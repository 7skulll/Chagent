"""Invocation-local capabilities supplied by the runner, never by model arguments."""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: Path
    session_id: UUID | None = None
    run_id: UUID | None = None
    workspace_identity: tuple[int, int] | None = None
