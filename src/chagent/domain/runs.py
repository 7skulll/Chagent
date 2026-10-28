"""Persisted run metadata and the result returned by the runner."""

from dataclasses import dataclass, field
from typing import Literal, TypeAlias
from uuid import UUID

from chagent.domain.messages import AssistantMessage, Message, ToolResultMessage


RunStatus: TypeAlias = Literal[
    "queued", "running", "completed", "failed", "cancelled", "interrupted"
]

RunEndReason: TypeAlias = Literal[
    "model_stop", "tool_requested_stop", "user_cancelled", "model_aborted",
    "turn_limit", "run_timeout", "context_budget_exhausted", "output_limit",
    "model_error", "model_protocol_error", "storage_error", "execution_error",
    "owner_lost", "legacy_terminal",
]


def terminal_reason(status: RunStatus, reason: RunEndReason | None = None) -> RunEndReason:
    allowed = {
        "completed": {"model_stop", "tool_requested_stop"},
        "cancelled": {"user_cancelled", "model_aborted"},
        "failed": {"turn_limit", "run_timeout", "context_budget_exhausted", "output_limit",
                   "model_error", "model_protocol_error", "storage_error", "execution_error"},
        "interrupted": {"owner_lost"},
    }
    defaults = {"completed": "model_stop", "cancelled": "user_cancelled",
                "failed": "execution_error", "interrupted": "owner_lost"}
    if status not in allowed:
        raise ValueError("Expected a terminal run status")
    reason = reason or defaults[status]
    if reason not in allowed[status]:
        raise ValueError("End reason does not match run status")
    return reason


@dataclass(slots=True)
class StoredMessage:
    id: UUID
    sequence: int
    message: Message
    run_id: UUID | None = None


@dataclass(slots=True)
class RunResult:
    run_id: UUID
    session_id: UUID
    message: AssistantMessage | None
    end_reason: RunEndReason = "model_stop"
    tool_results: list[ToolResultMessage] = field(default_factory=list)
