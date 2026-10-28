"""Convert our transcript to provider payloads, excluding host-only fields."""

import json

from chagent.domain.messages import (
    AssistantMessage, ImageContent, Message, SystemMessage, TextContent,
    ThinkingContent, ToolCall, ToolResultMessage, UserMessage,
)
from chagent.domain.tools import Tool
from chagent.models.base import ModelRequest


def normalize(request: ModelRequest) -> tuple[str, list[Tool], list[Message]]:
    instructions = []
    sections: dict[str, str] = {}
    tools: dict[str, Tool] = {}
    messages = []
    for message in request.messages:
        if not isinstance(message, SystemMessage):
            messages.append(message)
            continue
        text = message.content if isinstance(message.content, str) else "\n".join(block.text for block in message.content)
        if text:
            instructions.append(text)
        for name, content in (message.sections or {}).items():
            if content is None:
                sections.pop(name, None)
            else:
                sections[name] = content
        for tool in message.tools_added or []:
            tools[tool.name] = tool
        for tool in message.tools_removed or []:
            tools.pop(tool.name, None)
    return "\n\n".join([*instructions, *sections.values()]), list(tools.values()), messages


def openai_payload(request: ModelRequest, *, reasoning_replay: bool = False) -> dict:
    system, tools, history = normalize(request)
    messages = [{"role": "system", "content": system}] if system else []
    for message in history:
        if isinstance(message, UserMessage):
            content = message.content
            if not isinstance(content, str):
                content = [
                    {"type": "text", "text": block.text} if isinstance(block, TextContent) else
                    {"type": "image_url", "image_url": {"url": f"data:{block.mime_type};base64,{block.data}"}}
                    for block in content
                ]
            messages.append({"role": "user", "content": content})
        elif isinstance(message, AssistantMessage):
            text = "".join(block.text for block in message.content if isinstance(block, TextContent))
            item = {"role": "assistant", "content": text or None}
            calls = [block for block in message.content if isinstance(block, ToolCall)]
            if calls:
                item["tool_calls"] = [{
                    "id": call.id, "type": "function", "function": {
                        "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False, allow_nan=False),
                    },
                } for call in calls]
            if reasoning_replay and message.provider == request.model.provider and message.model == request.model.id:
                reasoning = "".join(block.thinking for block in message.content if isinstance(block, ThinkingContent))
                if reasoning:
                    item["reasoning_content"] = reasoning
            messages.append(item)
        elif isinstance(message, ToolResultMessage):
            if any(isinstance(block, ImageContent) for block in message.content):
                raise ValueError("Image tool results are not supported by the chat-completions adapter")
            text = "\n".join(block.text for block in message.content)
            if message.is_error:
                text = "[Tool error]\n" + text
            messages.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": text})
    body = {"model": request.model.id, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters}} for tool in tools]
    if request.temperature is not None:
        body["temperature"] = request.temperature
    return body


def anthropic_payload(request: ModelRequest) -> dict:
    system, tools, history = normalize(request)
    messages = []
    for message in history:
        if isinstance(message, UserMessage):
            blocks = [TextContent(message.content)] if isinstance(message.content, str) else message.content
            item = {"role": "user", "content": [_anthropic_content(block) for block in blocks]}
        elif isinstance(message, AssistantMessage):
            content = []
            for block in message.content:
                if isinstance(block, TextContent):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolCall):
                    content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.arguments})
                elif message.provider == request.model.provider and message.model == request.model.id:
                    if block.redacted and block.thinking_signature:
                        content.append({"type": "redacted_thinking", "data": block.thinking_signature})
                    elif block.thinking_signature:
                        content.append({"type": "thinking", "thinking": block.thinking, "signature": block.thinking_signature})
            if not content:
                raise ValueError("Assistant history has no replayable content for this provider")
            item = {"role": "assistant", "content": content}
        else:
            item = {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": message.tool_call_id,
                "content": [_anthropic_content(block) for block in message.content], "is_error": message.is_error,
            }]}
        if messages and messages[-1]["role"] == item["role"]:
            messages[-1]["content"].extend(item["content"])
        else:
            messages.append(item)
    body = {"model": request.model.id, "messages": messages, "max_tokens": request.max_tokens or request.model.max_tokens, "stream": True}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": tool.name, "description": tool.description, "input_schema": tool.parameters} for tool in tools]
    if request.temperature is not None:
        body["temperature"] = request.temperature
    return body


def _anthropic_content(block: TextContent | ImageContent) -> dict:
    if isinstance(block, TextContent):
        return {"type": "text", "text": block.text}
    return {"type": "image", "source": {"type": "base64", "media_type": block.mime_type, "data": block.data}}
