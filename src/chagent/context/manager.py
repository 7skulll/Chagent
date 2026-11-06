"""Budgeted orchestration, including checkpoints within a long-running task."""

import asyncio
from dataclasses import dataclass, field, replace
import logging
from time import monotonic_ns
from uuid import UUID, uuid4

from chagent.agent.types import RunState
from chagent.context.builder import BasicContextBuilder, BuiltContext, ContextBudgetError
from chagent.context.compactor import (
    COMPACTION_FRESH_WINDOW, COMPACTION_SUMMARY, ContextCompactor, CompactionError, MemoryCalls,
    PROMPT_VERSION, FRESH_WINDOW_MODEL, FRESH_WINDOW_VERSION, fresh_window_content,
)
from chagent.context.projection import ProjectionCache
from chagent.context.segments import exchanges
from chagent.domain.context import ObservedUsage
from chagent.domain.messages import SystemMessage, UserMessage
from chagent.domain.recovery import RecoveryNote
from chagent.domain.runs import StoredMessage
from chagent.domain.sessions import TaskState
from chagent.domain.summaries import (
    CompactionPolicy, FRESH_WINDOW_ALWAYS, FRESH_WINDOW_FALLBACK, SessionSummary,
)
from chagent.models.base import Model, ModelClient
from chagent.storage.repositories import ConversationRepository, RunRepository
from chagent.storage.summaries import SummaryRepository


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Inputs:
    """Everything a build needs from storage, or from an already-loaded run state."""

    history: list[StoredMessage]
    task_state: TaskState | None
    previous: SessionSummary | None
    recovery: RecoveryNote | None
    environment: str | None
    observed: ObservedUsage | None


@dataclass(slots=True)
class _Candidate:
    """A summary computed in the background and not yet published.

    Nothing here has touched the database: the checkpoint carries a throwaway
    revision and only receives a real one when the foreground adopts it.
    """

    summary: SessionSummary
    tokens_before: int


@dataclass(slots=True)
class _Speculation:
    """The in-flight background summary for one run, plus what would invalidate it."""

    identity: UUID
    model_id: str
    provider: str
    tool_revision: int
    previous_id: UUID | None
    previous_revision: int | None
    progress: int
    tokens_before: int
    recorder: MemoryCalls = field(default_factory=MemoryCalls)
    task: asyncio.Task | None = None

    def valid(self, *, model: Model, previous: SessionSummary | None, tool_revision: int) -> bool:
        """Whether the candidate still describes the request it would replace.

        History only grows within a run and the candidate covers a prefix of it,
        so a covered span cannot go stale; covering less than it could is the
        ordinary case and is allowed. What can change is the shape of the prompt
        around it: the tool declarations, the model, or the summary the candidate
        was built on top of.
        """
        return (self.model_id == model.id and self.provider == model.provider
                and self.tool_revision == tool_revision
                and self.previous_id == (previous.id if previous else None)
                and self.previous_revision == (previous.revision if previous else None))


class ContextManager:
    def __init__(self, builder: BasicContextBuilder, conversations: ConversationRepository,
                 runs: RunRepository, summaries: SummaryRepository, *, policy: CompactionPolicy | None = None,
                 modes: tuple[str, ...] = ("work",)):
        self.builder, self.conversations, self.runs, self.summaries = builder, conversations, runs, summaries
        # Which faces this build can hand a session. The prompt names the other
        # mode only when it is really reachable.
        self.modes = tuple(modes)
        self.policy = policy or CompactionPolicy.from_env()
        self.compactor = ContextCompactor(builder, summaries, self.policy)
        self._attempted: dict[UUID, tuple[int, int]] = {}
        # One projection cache per run: appended messages are projected and
        # counted once, not once per model turn.
        self._projections: dict[UUID, ProjectionCache] = {}
        # Speculative summaries in flight, keyed by run.
        self._speculations: dict[UUID, _Speculation] = {}

    async def release(self, run_id: UUID):
        speculation = self._speculations.pop(run_id, None)
        self._attempted.pop(run_id, None)
        self._projections.pop(run_id, None)
        if speculation is not None and speculation.task is not None:
            # The run is already terminal, so this work can only be discarded. It
            # owns no database state by construction, which is what makes a plain
            # cancel safe; the bounded wait keeps a wedged socket from delaying
            # teardown.
            speculation.task.cancel()
            _, pending = await asyncio.wait({speculation.task},
                                            timeout=self.policy.speculative_shutdown_timeout)
            if pending:
                logger.warning("speculative compaction %s did not unwind after run %s",
                               speculation.identity, run_id)

    def _cache(self, run_id: UUID) -> ProjectionCache:
        return self._projections.setdefault(
            run_id, ProjectionCache(max_tool_result_tokens=self.builder.policy.max_tool_result_tokens))

    @staticmethod
    def _checked(built):
        if built.snapshot.token_estimate > built.snapshot.input_budget:
            raise ContextBudgetError(
                "Required context exceeds budget after retaining input and the latest complete tool batch",
                required_tokens=built.snapshot.token_estimate, input_budget=built.snapshot.input_budget,
            )
        return built

    async def prepare(self, session_id: UUID, run_id: UUID, model: Model, client: ModelClient, tools,
                      *, state: RunState | None = None, prompt_mode: str | None = None) -> BuiltContext:
        """Build the request, compacting first when the budget demands it.

        The method reads as a guard sequence: load, try the baseline, adopt a
        summary that is already paid for, decide whether compaction is worth
        paying for, then hand the actual work to ``_compact``. Every early return
        is a case where keeping the previous context is the correct answer.

        Speculation sits beside the sequence rather than inside it: a summary that
        is not needed yet is started in the background so the model calls overlap
        with the tool calls that follow, and it is adopted by a later turn.
        """
        inputs = await self._inputs(session_id, run_id, state)
        # Run-static facts; absent only when a caller drives the builder without
        # a run state, in which case the prompt keeps its bare form. The mode is
        # stated by the runner, which is the only object that knows which face it
        # actually resolved — a single-face harness runs code tools under a work
        # session row, and the prompt must not claim otherwise.
        other_modes = tuple(name for name in self.modes if name != prompt_mode)
        current_input = next(item for item in reversed(inputs.history) if isinstance(item.message, UserMessage))
        owned = any(item.run_id == run_id for item in inputs.history)
        # One correction factor for the whole call: a candidate checkpoint has a
        # different shape but the same content, so it is measured the same way.
        scale = inputs.observed.scale if inputs.observed is not None else 1.0

        build = self._build_for(history=inputs.history, model=model, tools=tools, inputs=inputs,
                                run_id=run_id, prompt_mode=prompt_mode, other_modes=other_modes, scale=scale)

        baseline = await build(inputs.previous, inputs.observed)
        forced = self._should_compact(baseline)

        # A summary computed in the background is already paid for. It is adopted
        # exactly when the synchronous path would have compacted, so speculation
        # changes *when* the work happens, never *whether* it happens. Otherwise
        # every turn above the speculative trigger would compact again.
        speculation = self._speculations.get(run_id)
        if forced and speculation is not None:
            adopted = await self._adopt(
                speculation, session_id=session_id, run_id=run_id, state=state, inputs=inputs,
                model=model, baseline=baseline, build=build,
            )
            if adopted is not None:
                return self._checked(adopted)

        if not forced:
            await self._speculate(session_id=session_id, run_id=run_id, model=model, client=client,
                                  tools=tools, state=state, inputs=inputs, baseline=baseline,
                                  owned=owned, current_input=current_input, prompt_mode=prompt_mode,
                                  other_modes=other_modes, scale=scale)
            return self._checked(baseline)
        eligible = self._eligible(inputs.history, run_id, inputs.previous, owned, current_input)
        if not eligible:
            return self._checked(baseline)
        progress = eligible[-1][-1].sequence
        last_progress, count = self._attempted.get(run_id, (0, 0))
        if progress <= last_progress or count >= self.policy.max_compactions_per_run:
            return self._checked(baseline)
        limit = min(self.policy.max_summary_tokens, baseline.snapshot.input_budget // 4)
        identity, started = uuid4(), monotonic_ns()
        revision = await self.summaries.next_revision(session_id)
        checkpoint = self._checkpoint_maker(
            session_id=session_id, run_id=run_id, model=model, previous=inputs.previous, revision=revision,
            identity=identity, history=inputs.history, owned=owned, current_input=current_input,
            eligible=eligible,
        )
        # Do not pay for summarization when even the smallest legal suffix cannot fit.
        # A candidate checkpoint changes the prompt's structure, so the previous
        # provider anchor no longer describes it: count it from scratch.
        smallest = await build(checkpoint(len(eligible)), None)
        if smallest.snapshot.token_estimate + limit > smallest.snapshot.input_budget:
            return self._checked(baseline)
        self._attempted[run_id] = (progress, count + 1)
        await self.runs.event(run_id, "compaction_started", {
            "compaction_id": str(identity), "tokens_before": baseline.snapshot.projected_token_estimate,
            "previous_summary_id": str(inputs.previous.id) if inputs.previous else None,
        })
        return await self._compact(
            run_id=run_id, model=model, client=client, state=state, identity=identity, started=started,
            previous=inputs.previous, eligible=eligible, limit=limit, baseline=baseline, build=build,
            checkpoint=checkpoint, current_input=current_input,
        )

    def _build_for(self, *, history, model: Model, tools, inputs: _Inputs, run_id: UUID,
                   prompt_mode: str | None, other_modes: tuple[str, ...], scale: float):
        """Bind one build to one history.

        Background work gets its own list: the run keeps appending to
        ``state.history`` while a speculative summary is in flight, and a build
        that silently observed those appends would price a request nobody makes.
        """

        async def build(summary, observed):
            return await self.builder.build(history, model, tools, task_state=inputs.task_state,
                                            current_run_id=run_id, summary=summary, allow_over_budget=True,
                                            cache=self._cache(run_id), recovery=inputs.recovery,
                                            environment=inputs.environment, mode=prompt_mode,
                                            other_modes=other_modes, observed=observed, scale=scale)

        return build

    async def _speculate(self, *, session_id: UUID, run_id: UUID, model: Model, client: ModelClient,
                         tools, state: RunState | None, inputs: _Inputs, baseline: BuiltContext,
                         owned: bool, current_input: StoredMessage, prompt_mode: str | None,
                         other_modes: tuple[str, ...], scale: float) -> None:
        """Start a summary before the budget forces it, when it is worth the risk.

        The calls overlap with the tool work that follows this request, which is
        where the pause used to land. A candidate that is never adopted has cost
        only those calls; nothing is written until it is adopted, so an abandoned
        candidate leaves no audit row and no checkpoint behind.
        """
        if not (self.policy.enabled and self.policy.speculative and state is not None):
            return
        if self.policy.fresh_window == FRESH_WINDOW_ALWAYS:
            # Nothing to hide: a fresh window costs a build, not a model call.
            return
        if run_id in self._speculations:
            return
        if baseline.snapshot.projected_token_estimate < (
                baseline.snapshot.input_budget * self.policy.speculative_trigger):
            return
        last_progress, count = self._attempted.get(run_id, (0, 0))
        if count >= self.policy.max_compactions_per_run:
            return
        eligible = self._eligible(inputs.history, run_id, inputs.previous, owned, current_input)
        if not eligible:
            return
        progress = eligible[-1][-1].sequence
        if progress <= last_progress:
            return
        limit = min(self.policy.max_summary_tokens, baseline.snapshot.input_budget // 4)
        identity = uuid4()
        # The task reads this list and its build is bound to it; nothing it looks
        # at may change after this point.
        frozen = list(inputs.history)
        build = self._build_for(history=frozen, model=model, tools=tools, inputs=inputs, run_id=run_id,
                                prompt_mode=prompt_mode, other_modes=other_modes, scale=scale)
        # Revision 0 is a placeholder: the real one can only come from the
        # database, and it is assigned at adoption.
        checkpoint = self._checkpoint_maker(
            session_id=session_id, run_id=run_id, model=model, previous=inputs.previous, revision=0,
            identity=identity, history=frozen, owned=owned, current_input=current_input,
            eligible=eligible,
        )
        speculation = _Speculation(
            identity=identity, model_id=model.id, provider=model.provider,
            tool_revision=state.tool_revision,
            previous_id=inputs.previous.id if inputs.previous else None,
            previous_revision=inputs.previous.revision if inputs.previous else None,
            progress=progress, tokens_before=baseline.snapshot.projected_token_estimate,
        )
        self._speculations[run_id] = speculation
        # Claim the attempt now. A discarded candidate has already spent its
        # calls, and re-running the same range on the next turn is what the
        # synchronous path explicitly refuses to do.
        self._attempted[run_id] = (progress, count + 1)
        speculation.task = asyncio.create_task(
            self._compute(identity=identity, run_id=run_id, model=model, client=client,
                          previous=inputs.previous, eligible=eligible, limit=limit, baseline=baseline,
                          checkpoint=checkpoint, build=build, recorder=speculation.recorder,
                          current_input=current_input),
            name=f"speculative-compaction-{run_id}",
        )
        await self.runs.event(run_id, "compaction_started", {
            "compaction_id": str(identity), "tokens_before": speculation.tokens_before,
            "previous_summary_id": str(inputs.previous.id) if inputs.previous else None,
            "speculative": True,
        })

    async def _compute(self, *, identity: UUID, run_id: UUID, model: Model, client: ModelClient,
                       previous: SessionSummary | None, eligible: list[list[StoredMessage]], limit: int,
                       baseline: BuiltContext, checkpoint, build, recorder: MemoryCalls,
                       current_input: StoredMessage) -> _Candidate:
        """The same work as ``_compact`` with the database writes left out."""
        length = await self._fit_length(eligible, limit, baseline, build, checkpoint)
        candidate = checkpoint(length)
        task_input = self._task_input(current_input, candidate)
        candidate.content, candidate.usage = await self.compactor.compact(
            model=model, client=client, turns=eligible[:length],
            previous=previous.content if previous else "", limit=limit,
            compaction_id=identity, run_id=run_id, task_input=task_input, recorder=recorder,
        )
        return _Candidate(candidate, baseline.snapshot.projected_token_estimate)

    async def _adopt(self, speculation: _Speculation, *, session_id: UUID, run_id: UUID,
                     state: RunState | None, inputs: _Inputs, model: Model,
                     baseline: BuiltContext, build) -> BuiltContext | None:
        """Publish a finished candidate, or throw it away with a reason.

        Called only when the budget has run out, which is what makes waiting the
        right choice: the calls are already paid for, and recomputing them would
        buy the same summary twice.
        """
        if state is None or not speculation.valid(model=model, previous=inputs.previous,
                                                  tool_revision=state.tool_revision):
            await self._discard(speculation, run_id, "invalidated")
            return None
        self._speculations.pop(run_id, None)
        try:
            # The wait is bounded by the summarizer's own budget: a wedged request
            # must not consume the run's time, and falling back to the synchronous
            # path costs one more timeout at worst.
            async with asyncio.timeout(self.policy.timeout):
                candidate = await speculation.task
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if speculation.task is not None and not speculation.task.done():
                speculation.task.cancel()
                await asyncio.gather(speculation.task, return_exceptions=True)
            await self._discard(speculation, run_id, "timeout", finished=True)
            return None
        except Exception as error:
            reason = str(error) if isinstance(error, CompactionError) else type(error).__name__
            await self._discard(speculation, run_id, reason, finished=True)
            return None
        # The summary was verified against the history at the time it was
        # computed; the suffix has grown since, so it is verified again against
        # the request that would actually be sent now.
        revision = await self.summaries.next_revision(session_id)
        summary = replace(candidate.summary, id=uuid4(), revision=revision)
        try:
            result = self._checked(await build(summary, None))
            self._verify_reduction(result, baseline)
        except Exception as error:
            reason = str(error) if isinstance(error, (CompactionError, ContextBudgetError)) else type(error).__name__
            await self._discard(speculation, run_id, reason, finished=True)
            return None
        await self.summaries.record_calls(run_id, speculation.recorder.calls)
        await self.summaries.save(summary, run_id, completed_event={
            "compaction_id": str(speculation.identity), "summary_id": str(summary.id),
            "revision": summary.revision,
            "covered_through_sequence": summary.covered_through_sequence,
            "source_run_id": str(summary.source_run_id) if summary.source_run_id else None,
            "tokens_before": candidate.tokens_before,
            "tokens_after": result.snapshot.token_estimate,
            "duration_ms": None, "speculative": True,
        })
        state.summary = summary
        state.context_revision += 1
        # The prompt's second message just changed shape, so the provider anchor
        # describes a request this run will never send again.
        state.observed = None
        return result

    async def _discard(self, speculation: _Speculation, run_id: UUID, reason: str,
                       *, finished: bool = False) -> None:
        """Drop a candidate and record what it cost, while the run can still say so."""
        self._speculations.pop(run_id, None)
        if not finished and speculation.task is not None:
            speculation.task.cancel()
            await asyncio.gather(speculation.task, return_exceptions=True)
        spent = sum((call.response.usage.total_tokens if call.response is not None and call.response.usage
                     else 0) for call in speculation.recorder.calls)
        await self.runs.event(run_id, "compaction_discarded", {
            "compaction_id": str(speculation.identity), "reason": reason,
            "summarized_through_sequence": speculation.progress,
            "speculative_calls": len(speculation.recorder.calls), "speculative_tokens": spent,
        })

    async def _inputs(self, session_id: UUID, run_id: UUID, state: RunState | None) -> _Inputs:
        if state is not None:
            return _Inputs(state.history, state.task_state, state.summary, state.recovery,
                           state.environment, state.observed)
        return _Inputs(
            await self.conversations.messages(session_id, current_run=run_id, project_artifacts=True),
            await self.conversations.task_state(session_id),
            await self.summaries.latest(session_id, current_run=run_id),
            None, None, None,
        )

    def _should_compact(self, baseline: BuiltContext) -> bool:
        """Compaction is paid for when something was dropped, or the budget is tight."""
        if not self.policy.enabled:
            return False
        omissions = any(not item.selected and item.reason != "summarized" for item in baseline.snapshot.selections)
        if omissions:
            return True
        return baseline.snapshot.projected_token_estimate >= (
            baseline.snapshot.input_budget * self.policy.trigger_ratio)

    def _eligible(self, history: list[StoredMessage], run_id: UUID, previous: SessionSummary | None,
                  owned: bool, current_input: StoredMessage) -> list[list[StoredMessage]]:
        """Whole turns that may be summarized, oldest first, newest turn excluded."""
        boundary = previous.covered_through_sequence if previous else 0
        eligible: list[list[StoredMessage]] = []
        # Old user spans stay whole; a prior active-run cut may leave an assistant suffix.
        historical = [item for item in history if item.sequence > boundary
                      and item.run_id != run_id and not isinstance(item.message, SystemMessage)]
        for item in historical:
            if isinstance(item.message, UserMessage) or not eligible:
                eligible.append([])
            eligible[-1].append(item)
        active = exchanges([item for item in history if item.run_id == run_id and item.sequence > boundary])
        if active:
            # Keep the newest complete exchange verbatim, together with the exact current input.
            eligible.extend(active[:-1])
        elif not owned and eligible:
            # A regeneration owns no message of its own: the turn it answers is
            # the tail of the history, so it is held out of the summarizable span
            # exactly like an in-flight exchange. Otherwise the question could be
            # left only inside a summary, which the builder refuses.
            eligible.pop()
        return eligible

    def _checkpoint_maker(self, *, session_id: UUID, run_id: UUID, model: Model,
                          previous: SessionSummary | None, revision: int, identity: UUID,
                          history: list[StoredMessage], owned: bool, current_input: StoredMessage,
                          eligible: list[list[StoredMessage]]):
        """Build the candidate summary header for the first ``length`` turns."""

        def checkpoint(length: int) -> SessionSummary:
            prefix = eligible[:length]
            covered = prefix[-1][-1].sequence
            kept = next(item for item in history
                        if item.sequence > covered and not isinstance(item.message, SystemMessage))
            active_source = any(item.run_id == run_id for group in prefix for item in group)
            if not owned:
                # A regeneration owns no message of its own, so the turn being
                # answered is the last one; it still must not be summarized away.
                active_source = active_source or any(
                    any(item.id == current_input.id for item in group) for group in prefix)
            active_source = active_source or (previous is not None and previous.source_run_id == run_id)
            return SessionSummary(
                id=uuid4(), session_id=session_id, revision=revision,
                previous_summary_id=previous.id if previous else None,
                covered_through_sequence=covered, first_kept_message_id=kept.id,
                content="", model=model.id, provider=model.provider, prompt_version=PROMPT_VERSION,
                compaction_id=identity, source_message_ids=[str(item.id) for group in prefix for item in group],
                source_run_id=run_id if active_source else None,
                protected_input_id=current_input.id if active_source else None,
            )

        return checkpoint

    async def _compact(self, *, run_id: UUID, model: Model, client: ModelClient, state: RunState | None,
                       identity: UUID, started: int, previous: SessionSummary | None,
                       eligible: list[list[StoredMessage]], limit: int, baseline: BuiltContext,
                       build, checkpoint, current_input: StoredMessage) -> BuiltContext:
        """Compact, verify the result actually shrinks, then persist or fall back.

        Two implementations share this lifecycle, the way Codex keeps its three:
        ``summary`` pays a model to compress the span, ``fresh_window`` drops the
        span and restates what it was. The verification, the save, the event and
        the state update are the same for both, so a reader of the events cannot
        tell which one ran except by the ``implementation`` field.
        """
        strategy = COMPACTION_FRESH_WINDOW if self.policy.fresh_window == FRESH_WINDOW_ALWAYS else COMPACTION_SUMMARY
        try:
            return await self._install(
                strategy, run_id=run_id, model=model, client=client, state=state, identity=identity,
                started=started, previous=previous, eligible=eligible, limit=limit, baseline=baseline,
                build=build, checkpoint=checkpoint, current_input=current_input,
            )
        except asyncio.CancelledError:
            await self.runs.event(run_id, "compaction_cancelled", {"compaction_id": str(identity)})
            raise
        except Exception as error:
            reason = self._reason(error)
            if strategy == COMPACTION_SUMMARY and self.policy.fresh_window == FRESH_WINDOW_FALLBACK:
                # The span is unsummarizable, not the run: dropping it and saying so
                # keeps the task alive, where failing the run would not.
                try:
                    return await self._install(
                        COMPACTION_FRESH_WINDOW, run_id=run_id, model=model, client=client, state=state,
                        identity=identity, started=started, previous=previous, eligible=eligible,
                        limit=limit, baseline=baseline, build=build, checkpoint=checkpoint,
                        current_input=current_input, fallback_reason=reason,
                    )
                except asyncio.CancelledError:
                    await self.runs.event(run_id, "compaction_cancelled", {"compaction_id": str(identity)})
                    raise
                except Exception as fallback:
                    reason = f"{reason}; fresh window: {self._reason(fallback)}"
            await self.runs.event(run_id, "compaction_failed", {
                "compaction_id": str(identity), "reason": reason,
                "implementation": strategy,
                "fallback": "previous_summary_and_recent_complete_turns",
            })
            return self._checked(baseline)

    async def _install(self, strategy: str, *, run_id: UUID, model: Model, client: ModelClient,
                       state: RunState | None, identity: UUID, started: int,
                       previous: SessionSummary | None, eligible: list[list[StoredMessage]], limit: int,
                       baseline: BuiltContext, build, checkpoint, current_input: StoredMessage,
                       fallback_reason: str | None = None) -> BuiltContext:
        """Produce, verify and publish one checkpoint; raises when it does not hold up."""
        if strategy == COMPACTION_SUMMARY:
            length = await self._fit_length(eligible, limit, baseline, build, checkpoint)
        else:
            # Covering more costs nothing here: the restatement is host-authored and
            # bounded, while leaving an old turn out would only keep the window full.
            length = len(eligible)
        prefix, candidate = eligible[:length], checkpoint(length)
        if strategy == COMPACTION_SUMMARY:
            task_input = self._task_input(current_input, candidate)
            candidate.content, candidate.usage = await self.compactor.compact(
                model=model, client=client, turns=prefix, previous=previous.content if previous else "",
                limit=limit, compaction_id=identity, run_id=run_id, task_input=task_input,
            )
        else:
            candidate.content = fresh_window_content(prefix, limit, self.builder.counter,
                                                     retained=self.policy.retained_tool_results)
            # No model wrote this checkpoint and saying otherwise would make the
            # audit lie; ``prompt_version`` names the restatement instead.
            candidate.model, candidate.provider = FRESH_WINDOW_MODEL, FRESH_WINDOW_MODEL
            candidate.prompt_version, candidate.usage = FRESH_WINDOW_VERSION, None
        result = self._checked(await build(candidate, None))
        self._verify_reduction(result, baseline)
        await self.summaries.save(candidate, run_id, completed_event={
            "compaction_id": str(identity), "summary_id": str(candidate.id), "revision": candidate.revision,
            "covered_through_sequence": candidate.covered_through_sequence,
            "source_run_id": str(candidate.source_run_id) if candidate.source_run_id else None,
            "tokens_before": baseline.snapshot.projected_token_estimate,
            "tokens_after": result.snapshot.token_estimate,
            "duration_ms": (monotonic_ns() - started) // 1_000_000,
            "implementation": strategy,
            "fallback_reason": fallback_reason,
        })
        if state is not None:
            state.summary = candidate
            state.context_revision += 1
            # The prompt's second message just changed shape, so the provider
            # anchor describes a request this run will never send again.
            state.observed = None
        return result

    @staticmethod
    def _reason(error: BaseException) -> str:
        return str(error) if isinstance(error, (CompactionError, ContextBudgetError)) else type(error).__name__

    async def _fit_length(self, eligible: list[list[StoredMessage]], limit: int, baseline: BuiltContext,
                          build, checkpoint) -> int:
        """Smallest prefix whose summary plus the remaining suffix still fits."""
        left, right = 1, len(eligible)
        while left < right:
            middle = (left + right) // 2
            trial = await build(checkpoint(middle), None)
            if self._fits(trial, baseline, limit):
                right = middle
            else:
                left = middle + 1
        return left

    def _fits(self, trial: BuiltContext, baseline: BuiltContext, limit: int) -> bool:
        return (trial.snapshot.projected_token_estimate + limit
                <= baseline.snapshot.input_budget * self.policy.keep_recent_ratio
                and all(item.selected or item.reason == "summarized" for item in trial.snapshot.selections))

    @staticmethod
    def _verify_reduction(result: BuiltContext, baseline: BuiltContext) -> None:
        if result.snapshot.projected_token_estimate >= baseline.snapshot.projected_token_estimate:
            raise CompactionError("Summary does not reduce context size")
        if any(not item.selected and item.reason != "summarized" for item in result.snapshot.selections):
            raise CompactionError("Summary and remaining history do not fit the context budget")

    @staticmethod
    def _task_input(current_input: StoredMessage, candidate: SessionSummary) -> str:
        task_input = current_input.message.content if candidate.source_run_id else ""
        if not isinstance(task_input, str):
            task_input = "\n".join(getattr(block, "text", "") for block in task_input)
        return task_input
