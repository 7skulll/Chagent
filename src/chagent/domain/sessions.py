"""Session metadata, history pages and explicitly maintained task state."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID

from chagent.domain.runs import StoredMessage

#: The legacy values a session row may still hold. New sessions get one tool
#: face regardless; see ``execution`` for what the process may actually do.
SESSION_MODES = ("work", "code")


def validate_mode(mode: str) -> str:
    """Reject an unknown mode instead of falling back to the safe one.

    A silent downgrade would let a caller believe it got the write face when it
    did not, which is the same class of mistake as hiding a tool in the UI.
    """
    if mode not in SESSION_MODES:
        raise ValueError(f"Session mode must be one of {SESSION_MODES}")
    return mode


@dataclass(slots=True)
class Session:
    id: UUID
    workspace_id: UUID
    workspace: Path
    title: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    #: Chosen once at creation and immutable per run; the run inherits it.
    mode: str = "work"


@dataclass(slots=True)
class SessionPage:
    items: list[Session]
    next_cursor: UUID | None = None
    #: How many sessions of this workspace are archived, so the sidebar can
    #: label its toggle. ``None`` means the question did not apply (the page
    #: already shows only archived sessions).
    archived_count: int | None = None


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
