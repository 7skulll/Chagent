"""Message data types used by the agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from chagent.domain.json_types import JsonObject, JsonValue
from chagent.domain.tools import Tool, ToolReference
from chagent.domain.usage import Usage
from chagent.domain.artifacts import ArtifactReference

StopReason: TypeAlias = Literal[
    "pending", "stop", "length", "toolUse", "error", "aborted"
]


@dataclass(slots=True)
class TextContent:
    text: str
    text_signature: str | None = None
    type: Literal["text"] = field(default="text", init=False)


@dataclass(slots=True)
class ThinkingContent:
    """Opaque signatures must survive replay, including redacted thinking."""

    thinking: str
    thinking_signature: str | None = None
    redacted: bool = False
    type: Literal["thinking"] = field(default="thinking", init=False)


@dataclass(slots=True)
class ImageContent:
    """Base64 image data, without a data-URL prefix."""

    data: str
    mime_type: str
    type: Literal["image"] = field(default="image", init=False)


@dataclass(slots=True)
class ToolCall:
    """A model-issued tool call, following Pi's ToolCall content block.

    ``thought_signature`` preserves opaque provider reasoning metadata.
    ``namespace`` identifies a namespaced tool, when supplied by the provider.
    If the complete argument text is not a JSON object, the provider adapter
    sets ``arguments`` to an empty dictionary and records ``arguments_error``.
    This class stores the data; validation belongs to the execution boundary.
    """

    id: str
    name: str
    arguments: JsonObject
    thought_signature: str | None = None
    namespace: str | None = None
    arguments_error: str | None = None
    type: Literal["toolCall"] = field(default="toolCall", init=False)


@dataclass(slots=True)
class SystemMessage:
    """Instructions and tool changes at a point in the transcript.

    Timestamps throughout this module are Unix milliseconds. Later system
    messages can update named sections or add/remove tool declarations.
    Provider adapters are responsible for replaying these changes.
    """

    content: str | list[TextContent]
    timestamp: int
    sections: dict[str, str | None] | None = None
    tools_added: list[Tool] | None = None
    tools_removed: list[ToolReference] | None = None

    def text(self) -> str:
        """The instructions as the provider sees them: content, then named sections.

        Used by prompts, tests and the cache audit. ``normalize`` merges several
        system messages by section name, so this is the per-message view only.
        """
        content = (self.content if isinstance(self.content, str)
                   else "\n".join(block.text for block in self.content))
        parts = [content, *(self.sections or {}).values()]
        return "\n\n".join(part for part in parts if part)
    role: Literal["system"] = field(default="system", init=False)


@dataclass(slots=True)
class UserMessage:
    content: str | list[TextContent | ImageContent]
    timestamp: int
    role: Literal["user"] = field(default="user", init=False)


@dataclass(slots=True)
class AssistantMessage:
    content: list[TextContent | ThinkingContent | ToolCall]
    api: str
    provider: str
    model: str
    timestamp: int
    stop_reason: StopReason
    usage: Usage | None = None
    response_model: str | None = None
    response_id: str | None = None
    error_message: str | None = None
    raw_stop_reason: str | None = None
    duration_ms: int | None = None
    role: Literal["assistant"] = field(default="assistant", init=False)


@dataclass(slots=True)
class ToolResultMessage:
    """Transcript result paired with ToolCall.id via tool_call_id.

    ``content`` is model-visible. ``details`` is application metadata and
    must not be injected into model context automatically.
    """

    tool_call_id: str
    tool_name: str
    content: list[TextContent | ImageContent]
    is_error: bool
    timestamp: int
    details: JsonValue = None
    usage: Usage | None = None
    duration_ms: int | None = None
    artifact: ArtifactReference | None = None
    role: Literal["toolResult"] = field(default="toolResult", init=False)


Message: TypeAlias = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage
