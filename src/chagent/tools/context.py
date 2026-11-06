"""Invocation-local capabilities supplied by the runner, never by model arguments."""

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID


@dataclass(slots=True)
class DeferredTools:
    """Tool names the model has asked to see during this run.

    Shared by value: ``RunState`` owns the instance and hands the same object to
    every ``ToolContext`` of the run, so a tool can record what it loaded without
    the runner having to parse its output afterwards.
    """

    names: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: Path
    session_id: UUID | None = None
    run_id: UUID | None = None
    workspace_identity: tuple[int, int] | None = None
    #: Present only when the run declares tools on demand.
    deferred: DeferredTools | None = None
