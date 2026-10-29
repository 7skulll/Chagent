"""Budgeted orchestration, including checkpoints within a long-running task."""

import asyncio
from time import monotonic_ns
from uuid import UUID, uuid4

from chagent.agent.types import RunState
from chagent.context.builder import BasicContextBuilder, BuiltContext, ContextBudgetError
from chagent.context.compactor import ContextCompactor, CompactionError, PROMPT_VERSION
from chagent.context.projection import ProjectionCache
from chagent.context.segments import exchanges
from chagent.domain.messages import SystemMessage, UserMessage
from chagent.domain.summaries import CompactionPolicy, SessionSummary
from chagent.models.base import Model, ModelClient
from chagent.storage.repositories import ConversationRepository, RunRepository
from chagent.storage.summaries import SummaryRepository


class ContextManager:
    def __init__(self, builder: BasicContextBuilder, conversations: ConversationRepository,
                 runs: RunRepository, summaries: SummaryRepository, *, policy: CompactionPolicy | None = None):
        self.builder, self.conversations, self.runs, self.summaries = builder, conversations, runs, summaries
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
                      *, state: RunState | None = None) -> BuiltContext:
        history = state.history if state else await self.conversations.messages(session_id, current_run=run_id, project_artifacts=True)
        task_state = state.task_state if state else await self.conversations.task_state(session_id)
        previous = state.summary if state else await self.summaries.latest(session_id, current_run=run_id)
        recovery = state.recovery if state is not None else None
        # Run-static facts; absent only when a caller drives the builder without
        # a run state, in which case the prompt keeps its bare form.
        environment = state.environment if state is not None else None
        current_input = next(item for item in history if item.run_id == run_id and isinstance(item.message, UserMessage))

        async def build(summary):
            return await self.builder.build(history, model, tools, task_state=task_state,
                                            current_run_id=run_id, summary=summary, allow_over_budget=True,
                                            cache=self._cache(run_id), recovery=recovery,
                                            environment=environment)

        baseline = await build(previous)
        omissions = any(not item.selected and item.reason != "summarized" for item in baseline.snapshot.selections)
        if (not self.policy.enabled or (not omissions and baseline.snapshot.projected_token_estimate
                                        < baseline.snapshot.input_budget * self.policy.trigger_ratio)):
            return self._checked(baseline)
        boundary = previous.covered_through_sequence if previous else 0
        eligible = []
        # Old user spans stay whole; a prior active-run cut may leave an assistant suffix.
        historical = [item for item in history if item.sequence > boundary
                      and item.run_id != run_id and not isinstance(item.message, SystemMessage)]
        for item in historical:
            if isinstance(item.message, UserMessage) or not eligible:
                eligible.append([])
            eligible[-1].append(item)
        active = exchanges([item for item in history if item.run_id == run_id and item.sequence > boundary])
        # Keep the newest complete exchange verbatim, together with the exact current input.
        eligible.extend(active[:-1])
        if not eligible:
            return self._checked(baseline)
        progress = eligible[-1][-1].sequence
        last_progress, count = self._attempted.get(run_id, (0, 0))
        if progress <= last_progress or count >= self.policy.max_compactions_per_run:
            return self._checked(baseline)
        limit = min(self.policy.max_summary_tokens, baseline.snapshot.input_budget // 4)
        identity, started = uuid4(), monotonic_ns()
        revision = await self.summaries.next_revision(session_id)

        def checkpoint(length):
            prefix = eligible[:length]
            covered = prefix[-1][-1].sequence
            kept = next(item for item in history if item.sequence > covered and not isinstance(item.message, SystemMessage))
            active_source = any(item.run_id == run_id for group in prefix for item in group)
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

        # Do not pay for summarization when even the smallest legal suffix cannot fit.
        smallest = await build(checkpoint(len(eligible)))
        if smallest.snapshot.token_estimate + limit > smallest.snapshot.input_budget:
            return self._checked(baseline)
        self._attempted[run_id] = (progress, count + 1)
        await self.runs.event(run_id, "compaction_started", {
            "compaction_id": str(identity), "tokens_before": baseline.snapshot.projected_token_estimate,
            "previous_summary_id": str(previous.id) if previous else None,
        })
        try:
            left, right = 1, len(eligible)
            while left < right:
                middle = (left + right) // 2
                trial = await build(checkpoint(middle))
                if (trial.snapshot.projected_token_estimate + limit <= baseline.snapshot.input_budget * self.policy.keep_recent_ratio
                        and all(item.selected or item.reason == "summarized" for item in trial.snapshot.selections)):
                    right = middle
                else:
                    left = middle + 1
            prefix, candidate = eligible[:left], checkpoint(left)
            task_input = current_input.message.content if candidate.source_run_id else ""
            if not isinstance(task_input, str):
                task_input = "\n".join(getattr(block, "text", "") for block in task_input)
            candidate.content, candidate.usage = await self.compactor.compact(
                model=model, client=client, turns=prefix, previous=previous.content if previous else "",
                limit=limit, compaction_id=identity, run_id=run_id, task_input=task_input,
            )
            result = self._checked(await build(candidate))
            if result.snapshot.projected_token_estimate >= baseline.snapshot.projected_token_estimate:
                raise CompactionError("Summary does not reduce context size")
            if any(not item.selected and item.reason != "summarized" for item in result.snapshot.selections):
                raise CompactionError("Summary and remaining history do not fit the context budget")
            await self.summaries.save(candidate, run_id, completed_event={
                "compaction_id": str(identity), "summary_id": str(candidate.id), "revision": candidate.revision,
                "covered_through_sequence": candidate.covered_through_sequence,
                "source_run_id": str(candidate.source_run_id) if candidate.source_run_id else None,
                "tokens_before": baseline.snapshot.projected_token_estimate, "tokens_after": result.snapshot.token_estimate,
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
