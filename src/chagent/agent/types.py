"""Invocation-owned state; transcript entries are appended only after commit."""

from dataclasses import dataclass, field, replace
from typing import Literal
from uuid import UUID

from chagent.domain.messages import ImageContent, ToolResultMessage, UserMessage
from chagent.domain.recovery import RecoveryNote
from chagent.domain.runs import RunEndReason, StoredMessage
from chagent.domain.sessions import Session, TaskState
from chagent.domain.summaries import SessionSummary
from chagent.domain.tools import Tool


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
    tool_revision: int = -1
    # Facts from earlier runs that ended without a normal stop; loaded once per
    # run so every turn sees the same note and no turn re-queries it.
    recovery: RecoveryNote | None = None

    @property
    def current_input(self) -> StoredMessage:
        return next(item for item in self.history
                    if item.run_id == self.run_id and isinstance(item.message, UserMessage))

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
