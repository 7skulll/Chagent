"""Minimal model and streaming contracts, independent of provider SDKs."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol
from pathlib import Path
from uuid import UUID

from chagent.domain.messages import Message
from chagent.models.events import AssistantMessageEvent


@dataclass(slots=True)
class Model:
    """Model identity and limits supplied by configuration or a provider."""

    id: str
    provider: str
    api: str
    context_window: int
    max_tokens: int
    endpoint: str | None = None


@dataclass(slots=True)
class ModelRequest:
    """System instructions and tool declarations live in system messages.

    No separate system_prompt or tools field: the transcript is the single
    source of truth. Adapters replay section and tool updates as necessary.
    """

    model: Model
    messages: list[Message]
    max_tokens: int | None = None
    temperature: float | None = None
    purpose: Literal["task", "summary", "memory"] = "task"
    workspace: Path | None = None
    run_id: UUID | None = None


class ModelClient(Protocol):
    """Produce Pi-style start/updates/done or error events.

    Updates require a preceding start. Exactly one done or error terminates
    the stream; setup failures may emit error without start. Provider failures
    use error events; cancellation must not be swallowed by an adapter.
    Final messages are authoritative; partial tool arguments are not executable.
    """

    def stream(self, request: ModelRequest) -> AsyncIterator[AssistantMessageEvent]: ...
