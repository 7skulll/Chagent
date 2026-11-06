"""Invocation-owned state; transcript entries are appended only after commit."""

from dataclasses import dataclass, field, replace
from typing import Literal
from uuid import UUID

from chagent.domain.context import ObservedUsage
from chagent.domain.messages import ImageContent, ToolResultMessage, UserMessage
from chagent.domain.recovery import RecoveryNote
from chagent.domain.runs import RunEndReason, StoredMessage
from chagent.domain.sessions import Session, TaskState
from chagent.domain.summaries import SessionSummary
from chagent.domain.tools import Tool
from chagent.domain.usage import Usage
from chagent.tools.context import DeferredTools


@dataclass(frozen=True, slots=True)
class FinishDecision:
    action: Literal["continue", "complete"]
    reason: RunEndReason | Literal["tool_results_ready"]


@dataclass(slots=True)
class RunState:
    run_id: UUID
    session: Session
    history: list[StoredMessage]
    task_state: TaskState
    summary: SessionSummary | None = None
    iteration: int = 0
    active_model_call_id: UUID | None = None
    context_revision: int = 0
    tools: list[Tool] = field(default_factory=list)
    #: Changes whenever the *declared* set changes, which includes a tool loaded
    #: through ``tool_search``: the prompt prefix changes with it.
    tool_revision: int = -1
    #: Cheap identity of the declarations last handed to the builder: the registry
    #: revision plus how many deferred tools have been loaded. Names only ever get
    #: added, so the count is enough — and rebuilding every schema on every turn to
    #: compare them would defeat the point of declaring them once per run.
    declared_key: tuple[int, int] = (-1, -1)
    #: Which deferrable tools the model has loaded. Shared with every
    #: ``ToolContext`` of the run, so ``tool_search`` records into the run state.
    deferred: DeferredTools = field(default_factory=DeferredTools)
    # Facts from earlier runs that ended without a normal stop; loaded once per
    # run so every turn sees the same note and no turn re-queries it.
    recovery: RecoveryNote | None = None
    # Workspace snapshot (cwd, tree, branch, AGENTS.md) rendered once at run
    # start. It is part of the prompt prefix, so it must not change mid-run.
    environment: str | None = None
    workspace_identity: tuple[int, int] | None = None
    # The provider's own count for the last prompt of this run, if it reported
    # one. It is dropped whenever a structural part of the prompt changes (tool
    # declarations, an installed summary), because then it no longer describes
    # what the next request will contain.
    observed: ObservedUsage | None = None

    @property
    def current_input(self) -> StoredMessage:
        """The question this run answers.

        Normally the user message this run wrote. A regeneration writes none —
        it re-asks a question already in the transcript — so the fallback is the
        most recent user message, which is the one being answered again. That
        keeps the input protected from compaction and keeps memory recall and
        task-state handling reading the same text a first answer read.
        """
        return next(item for item in reversed(self.history) if isinstance(item.message, UserMessage))

    def append_committed(self, record: StoredMessage) -> None:
        if record.run_id != self.run_id or (self.history and record.sequence <= self.history[-1].sequence):
            raise ValueError("Committed message does not advance this run's history")
        # Match the database's artifact projection; large text stays in storage.
        message = record.message
        if isinstance(message, ToolResultMessage) and message.artifact is not None:
            record = replace(record, message=replace(message, details=None,
                             content=[block for block in message.content if isinstance(block, ImageContent)]))
        self.history.append(record)
        self.context_revision += 1

    def record_usage(self, usage: Usage | None, *, after_sequence: int, memory_tokens: int = 0,
                     coarse_tokens: int = 0) -> None:
        """Anchor the next turn's estimate on what the provider actually charged.

        ``memory_tokens`` is removed because the recalled reference is rebuilt on
        every turn and charged again by ``MemoryService.enrich``; keeping it in
        the anchor would count the same block twice in every later turn.
        ``coarse_tokens`` is the heuristic's count of the same request, so the
        pair records how far the heuristic reads off on this content.
        """
        if usage is None or usage.total_tokens <= 0:
            self.observed = None
            return
        memory = max(0, memory_tokens)
        # ``total_tokens`` is prompt plus completion: the completion becomes part
        # of the next prompt, so both halves belong to the anchor. ``input`` is
        # only the prompt half, which is what the heuristic is compared against.
        input_tokens = usage.input + usage.cache_read + usage.cache_write
        self.observed = ObservedUsage(
            prompt_tokens=max(1, usage.total_tokens - memory),
            input_tokens=max(0, input_tokens - memory),
            after_sequence=after_sequence,
            coarse_prompt_tokens=max(0, coarse_tokens - memory),
        )
