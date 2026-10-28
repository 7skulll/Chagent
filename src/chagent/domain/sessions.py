"""Session metadata, history pages and explicitly maintained task state."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID

from chagent.domain.runs import StoredMessage


@dataclass(slots=True)
class Session:
    id: UUID
    workspace_id: UUID
    workspace: Path
    title: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


@dataclass(slots=True)
class SessionPage:
    items: list[Session]
    next_cursor: UUID | None = None


@dataclass(slots=True)
class MessagePage:
    items: list[StoredMessage]
    next_before: int | None = None


@dataclass(slots=True)
class TaskState:
    """User-maintained task facts; no automatic inference or long-term memory."""

    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    source_message_ids: list[str] = field(default_factory=list)
    revision: int = 0
