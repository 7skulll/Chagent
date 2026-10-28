"""Stateless deterministic provider that observes the actual tool result."""

from collections.abc import AsyncIterator
from copy import deepcopy
from time import time_ns
from uuid import uuid4

from chagent.domain.messages import AssistantMessage, JsonObject, TextContent, ToolCall, ToolResultMessage, UserMessage
from chagent.models.base import ModelRequest
from chagent.models.events import (
    AssistantMessageEvent, ContentDeltaEvent, ContentEndEvent, ContentStartEvent,
    DoneEvent, StartEvent, ToolCallEndEvent,
)


class FakeModelClient:
    def __init__(self, tool_name: str = "echo", arguments: JsonObject | None = None):
        self.tool_name = tool_name
        self.arguments = deepcopy(arguments)

    async def stream(self, request: ModelRequest) -> AsyncIterator[AssistantMessageEvent]:
        partial = AssistantMessage(
            content=[], api=request.model.api, provider=request.model.provider,
            model=request.model.id, timestamp=time_ns() // 1_000_000,
            stop_reason="pending",
        )
        yield StartEvent(partial=partial)
        if request.purpose in {"summary", "memory"}:
            # Explicit offline fixture, not a semantic summarizer.
            final = deepcopy(partial)
            final.content = [TextContent('{"memories":[]}' if request.purpose == "memory" else "演示摘要：历史轮次已完成。请查阅原始记录获取细节。")]
            final.stop_reason = "stop"
            yield DoneEvent(reason="stop", message=final)
            return
        last = request.messages[-1]
        if isinstance(last, UserMessage):
            text = last.content if isinstance(last.content, str) else "\n".join(
                block.text for block in last.content if isinstance(block, TextContent)
            )
            call = ToolCall(id="call_" + uuid4().hex, name=self.tool_name,
                            arguments=deepcopy(self.arguments) if self.arguments is not None else {"text": text})
            partial.content.append(call)
            yield ContentStartEvent(type="toolcall_start", content_index=0, partial=partial)
            yield ToolCallEndEvent(content_index=0, tool_call=call, partial=partial)
            final = deepcopy(partial)
            final.stop_reason = "toolUse"
            yield DoneEvent(reason="toolUse", message=final)
        elif isinstance(last, ToolResultMessage):
            text = "\n".join(block.text for block in last.content if isinstance(block, TextContent))
            prefix = "工具返回错误：" if last.is_error else "工具执行结果："
            partial.content.append(TextContent(text=""))
            yield ContentStartEvent(type="text_start", content_index=0, partial=partial)
            partial.content[0].text = prefix + text
            yield ContentDeltaEvent(type="text_delta", content_index=0, delta=prefix + text, partial=partial)
            yield ContentEndEvent(type="text_end", content_index=0, content=prefix + text, partial=partial)
            final = deepcopy(partial)
            final.stop_reason = "stop"
            yield DoneEvent(reason="stop", message=final)
        else:
            raise ValueError("FakeModelClient expects a user message or tool result")
