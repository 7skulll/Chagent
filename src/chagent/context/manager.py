"""Budgeted orchestration, including checkpoints within a long-running task."""

import asyncio
from dataclasses import dataclass
from time import monotonic_ns
from uuid import UUID, uuid4

from chagent.agent.types import RunState
from chagent.context.builder import BasicContextBuilder, BuiltContext, ContextBudgetError
from chagent.context.compactor import ContextCompactor, CompactionError, PROMPT_VERSION
from chagent.context.projection import ProjectionCache
from chagent.context.segments import exchanges
from chagent.domain.messages import SystemMessage, UserMessage
from chagent.domain.recovery import RecoveryNote
from chagent.domain.runs import StoredMessage
from chagent.domain.sessions import TaskState
from chagent.domain.summaries import CompactionPolicy, SessionSummary
from chagent.models.base import Model, ModelClient
from chagent.storage.repositories import ConversationRepository, RunRepository
from chagent.storage.summaries import SummaryRepository


@dataclass(slots=True)
class _Inputs:
    """Everything a build needs from storage, or from an already-loaded run state."""

    history: list[StoredMessage]
    task_state: TaskState | None
    previous: SessionSummary | None
    recovery: RecoveryNote | None
    environment: str | None


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

    def release(self, run_id: UUID):
        self._attempted.pop(run_id, None)
        self._projections.pop(run_id, None)

    def _cache(self, run_id: UUID) -> ProjectionCache:
        return self._projections.setdefault(run_id, ProjectionCache())

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

        The method reads as a guard sequence: load, try the baseline, decide
        whether compaction is worth paying for, then hand the actual work to
        ``_compact``. Every early return is a case where keeping the previous
        context is the correct answer.
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

        async def build(summary):
            return await self.builder.build(inputs.history, model, tools, task_state=inputs.task_state,
                                            current_run_id=run_id, summary=summary, allow_over_budget=True,
                                            cache=self._cache(run_id), recovery=inputs.recovery,
                                            environment=inputs.environment, mode=prompt_mode,
                                            other_modes=other_modes)

        baseline = await build(inputs.previous)
        if not self._should_compact(baseline):
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
        smallest = await build(checkpoint(len(eligible)))
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

    async def _inputs(self, session_id: UUID, run_id: UUID, state: RunState | None) -> _Inputs:
        if state is not None:
            return _Inputs(state.history, state.task_state, state.summary, state.recovery, state.environment)
        return _Inputs(
            await self.conversations.messages(session_id, current_run=run_id, project_artifacts=True),
            await self.conversations.task_state(session_id),
            await self.summaries.latest(session_id, current_run=run_id),
            None, None,
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
        """Summarize, verify the result actually shrinks, then persist or fall back."""
        try:
            length = await self._fit_length(eligible, limit, baseline, build, checkpoint)
            prefix, candidate = eligible[:length], checkpoint(length)
            task_input = self._task_input(current_input, candidate)
            candidate.content, candidate.usage = await self.compactor.compact(
                model=model, client=client, turns=prefix, previous=previous.content if previous else "",
                limit=limit, compaction_id=identity, run_id=run_id, task_input=task_input,
            )
            result = self._checked(await build(candidate))
            self._verify_reduction(result, baseline)
            await self.summaries.save(candidate, run_id, completed_event={
                "compaction_id": str(identity), "summary_id": str(candidate.id), "revision": candidate.revision,
                "covered_through_sequence": candidate.covered_through_sequence,
                "source_run_id": str(candidate.source_run_id) if candidate.source_run_id else None,
                "tokens_before": baseline.snapshot.projected_token_estimate,
                "tokens_after": result.snapshot.token_estimate,
                "duration_ms": (monotonic_ns() - started) // 1_000_000,
            })
            if state is not None:
                state.summary = candidate
                state.context_revision += 1
            return result
        except asyncio.CancelledError:
            await self.runs.event(run_id, "compaction_cancelled", {"compaction_id": str(identity)})
            raise
        except Exception as error:
            reason = str(error) if isinstance(error, (CompactionError, ContextBudgetError)) else type(error).__name__
            await self.runs.event(run_id, "compaction_failed", {
                "compaction_id": str(identity), "reason": reason,
                "fallback": "previous_summary_and_recent_complete_turns",
            })
            return self._checked(baseline)

    async def _fit_length(self, eligible: list[list[StoredMessage]], limit: int, baseline: BuiltContext,
                          build, checkpoint) -> int:
        """Smallest prefix whose summary plus the remaining suffix still fits."""
        left, right = 1, len(eligible)
        while left < right:
            middle = (left + right) // 2
            trial = await build(checkpoint(middle))
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
