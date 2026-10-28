"""Typed stream events based on Pi's AssistantMessageEvent union.

Partial messages are live response objects, not immutable event snapshots.
Consumers retaining intermediate states must copy them.
"""

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from chagent.domain.messages import AssistantMessage, ToolCall


@dataclass(slots=True)
class StartEvent:
    partial: AssistantMessage
    type: Literal["start"] = field(default="start", init=False)


@dataclass(slots=True)
class ContentStartEvent:
    type: Literal["text_start", "thinking_start", "toolcall_start"]
    content_index: int
    partial: AssistantMessage


@dataclass(slots=True)
class ContentDeltaEvent:
    type: Literal["text_delta", "thinking_delta", "toolcall_delta"]
    content_index: int
    delta: str
    partial: AssistantMessage


@dataclass(slots=True)
class ContentEndEvent:
    type: Literal["text_end", "thinking_end"]
    content_index: int
    content: str
    partial: AssistantMessage


@dataclass(slots=True)
class ToolCallEndEvent:
    content_index: int
    tool_call: ToolCall
    partial: AssistantMessage
    type: Literal["toolcall_end"] = field(default="toolcall_end", init=False)


@dataclass(slots=True)
class DoneEvent:
    reason: Literal["stop", "length", "toolUse"]
    message: AssistantMessage
    type: Literal["done"] = field(default="done", init=False)


@dataclass(slots=True)
class ErrorEvent:
    reason: Literal["error", "aborted"]
    error: AssistantMessage
    type: Literal["error"] = field(default="error", init=False)


AssistantMessageEvent: TypeAlias = (
    StartEvent
    | ContentStartEvent
    | ContentDeltaEvent
    | ContentEndEvent
    | ToolCallEndEvent
    | DoneEvent
    | ErrorEvent
)
