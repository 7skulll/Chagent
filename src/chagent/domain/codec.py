"""Restore persisted messages without losing typed content or provider metadata."""

from dataclasses import asdict

from chagent.domain.json_types import JsonObject
from chagent.domain.messages import (
    AssistantMessage, ImageContent, Message, SystemMessage, TextContent,
    ThinkingContent, ToolCall, ToolResultMessage, UserMessage,
)
from chagent.domain.tools import Tool, ToolReference
from chagent.domain.usage import Usage, UsageCost
from chagent.domain.artifacts import ArtifactReference


def message_to_dict(message: Message) -> JsonObject:
    return asdict(message)


def message_from_dict(payload: dict) -> Message:
    data = dict(payload)
    role = data.pop("role", None)
    classes = {
        "system": SystemMessage, "user": UserMessage,
        "assistant": AssistantMessage, "toolResult": ToolResultMessage,
    }
    if role not in classes:
        raise ValueError(f"Unknown message role: {role!r}")
    content = data.get("content")
    allowed = {
        "system": {"text"}, "user": {"text", "image"},
        "assistant": {"text", "thinking", "toolCall"},
        "toolResult": {"text", "image"},
    }[role]
    if isinstance(content, list):
        blocks = []
        block_classes = {
            "text": TextContent, "image": ImageContent,
            "thinking": ThinkingContent, "toolCall": ToolCall,
        }
        for raw in content:
            if not isinstance(raw, dict) or raw.get("type") not in allowed:
                raise ValueError(f"Invalid content block for {role}")
            block = dict(raw)
            kind = block.pop("type")
            blocks.append(block_classes[kind](**block))
        data["content"] = blocks
    elif role not in {"system", "user"} or not isinstance(content, str):
        raise ValueError(f"Invalid message content for {role}")
    if data.get("usage") is not None:
        usage = dict(data["usage"])
        if usage.get("cost") is not None:
            usage["cost"] = UsageCost(**usage["cost"])
        data["usage"] = Usage(**usage)
    if role == "toolResult" and data.get("artifact") is not None:
        data["artifact"] = ArtifactReference(**data["artifact"])
    if role == "system":
        if data.get("tools_added") is not None:
            data["tools_added"] = [Tool(**tool) for tool in data["tools_added"]]
        if data.get("tools_removed") is not None:
            data["tools_removed"] = [ToolReference(**tool) for tool in data["tools_removed"]]
    return classes[role](**data)
