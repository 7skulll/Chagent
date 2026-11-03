"""OpenAI-compatible streaming chat completions with complete tool-call assembly."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from chagent.domain.messages import AssistantMessage, TextContent, ThinkingContent, ToolCall
from chagent.domain.usage import Usage
from chagent.models.base import ModelRequest
from chagent.models.events import (
    AssistantMessageEvent, ContentDeltaEvent, ContentEndEvent, ContentStartEvent,
    DoneEvent, ToolCallEndEvent,
)
from chagent.models.http import HTTPModelClient, ModelProtocolError, event_object, finalize_arguments, sse_events
from chagent.models.transcript import openai_payload


@dataclass(slots=True)
class ToolAssembly:
    call: ToolCall
    content_index: int
    arguments: str = ""


def chat_usage(value: dict) -> Usage:
    cached = value.get("prompt_cache_hit_tokens", (value.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    incoming, outgoing = value["prompt_tokens"], value["completion_tokens"]
    reasoning = (value.get("completion_tokens_details") or {}).get("reasoning_tokens")
    total = value.get("total_tokens", incoming + outgoing)
    if any(type(number) is not int or number < 0 for number in (incoming, outgoing, cached, total)) or cached > incoming:
        raise ModelProtocolError("Invalid token usage in provider response")
    if reasoning is not None and (type(reasoning) is not int or not 0 <= reasoning <= outgoing):
        raise ModelProtocolError("Invalid reasoning token usage")
    return Usage(input=incoming - cached, output=outgoing, cache_read=cached, cache_write=0,
                 total_tokens=total, reasoning=reasoning)


class OpenAIChatClient(HTTPModelClient):
    @property
    def endpoint(self) -> str:
        return self.settings.base_url + "/chat/completions"

    def headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.settings.api_key, "Accept": "text/event-stream"}

    def payload(self, request: ModelRequest) -> dict:
        body = openai_payload(request, reasoning_replay=self.settings.provider == "deepseek")
        reasoning_model = request.model.id.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))
        token_field = "max_completion_tokens" if self.settings.provider == "openai" and reasoning_model else "max_tokens"
        body[token_field] = request.max_tokens or request.model.max_tokens
        thinking = self.settings.thinking
        if self.settings.provider == "openai" and reasoning_model:
            body["reasoning_effort"] = "high" if thinking else "low"
        elif self.settings.provider == "deepseek":
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        elif self.settings.provider == "qwen":
            body["enable_thinking"] = thinking
        return body

    async def decode(self, response: httpx.Response, partial: AssistantMessage) -> AsyncIterator[AssistantMessageEvent]:
        calls: dict[int, ToolAssembly] = {}
        text_index: int | None = None
        thinking_index: int | None = None
        finish: str | None = None
        done = False
        async for event in sse_events(response):
            if done:
                raise ModelProtocolError("Data received after stream completion")
            if event.data == "[DONE]":
                done = True
                continue
            chunk = event_object(event)
            if chunk.get("id"):
                partial.response_id = chunk["id"]
            if chunk.get("model"):
                partial.response_model = chunk["model"]
            if chunk.get("usage"):
                partial.usage = chat_usage(chunk["usage"])
            choices = chunk.get("choices", [])
            if not choices:
                continue
            if len(choices) != 1 or choices[0].get("index", 0) != 0:
                raise ModelProtocolError("Only one completion choice is supported")
            choice = choices[0]
            delta = choice.get("delta") or {}
            if delta.get("refusal"):
                raise ModelProtocolError("Model refused the request")
            if finish is not None and any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls")):
                raise ModelProtocolError("Content received after a finish reason")
            for field, kind in (("reasoning_content", "thinking"), ("content", "text")):
                value = delta.get(field)
                if value is None or value == "":
                    continue
                if not isinstance(value, str):
                    raise ModelProtocolError("Expected streamed text")
                index = thinking_index if kind == "thinking" else text_index
                if index is None:
                    index = len(partial.content)
                    partial.content.append(ThinkingContent("") if kind == "thinking" else TextContent(""))
                    if kind == "thinking":
                        thinking_index = index
                    else:
                        text_index = index
                    yield ContentStartEvent(type=kind + "_start", content_index=index, partial=partial)
                block = partial.content[index]
                if kind == "thinking":
                    block.thinking += value
                else:
                    block.text += value
                yield ContentDeltaEvent(type=kind + "_delta", content_index=index, delta=value, partial=partial)
            for fragment in delta.get("tool_calls") or []:
                index = fragment.get("index")
                if not isinstance(index, int) or index < 0:
                    raise ModelProtocolError("Tool fragment requires a nonnegative index")
                if index not in calls:
                    assembly = ToolAssembly(ToolCall("", "", {}), len(partial.content))
                    calls[index] = assembly
                    partial.content.append(assembly.call)
                    yield ContentStartEvent(type="toolcall_start", content_index=assembly.content_index, partial=partial)
                assembly = calls[index]
                if fragment.get("type", "function") != "function":
                    raise ModelProtocolError("Unsupported tool-call type")
                assembly.call.id += fragment.get("id") or ""
                function = fragment.get("function") or {}
                assembly.call.name += function.get("name") or ""
                raw = function.get("arguments")
                if raw is not None:
                    if not isinstance(raw, str):
                        raise ModelProtocolError("Tool arguments must stream as JSON text")
                    assembly.arguments += raw
                    yield ContentDeltaEvent(type="toolcall_delta", content_index=assembly.content_index, delta=raw, partial=partial)
            reason = choice.get("finish_reason")
            if reason is not None:
                if finish is not None and finish != reason:
                    raise ModelProtocolError("Conflicting finish reasons")
                finish = reason
        if not done or finish not in {"stop", "tool_calls", "length"}:
            raise ModelProtocolError("Stream is incomplete, refused, or has an unsupported finish reason")
        if bool(calls) != (finish == "tool_calls") and finish != "length":
            raise ModelProtocolError("Finish reason does not match tool calls")
        for assembly in calls.values():
            if not assembly.call.id or not assembly.call.name:
                raise ModelProtocolError("Tool call is missing its ID or name")
            assembly.call.arguments, assembly.call.arguments_error = finalize_arguments(assembly.arguments)
            yield ToolCallEndEvent(assembly.content_index, assembly.call, partial)
        for index, kind in ((thinking_index, "thinking"), (text_index, "text")):
            if index is not None:
                block = partial.content[index]
                yield ContentEndEvent(type=kind + "_end", content_index=index,
                                      content=block.thinking if kind == "thinking" else block.text, partial=partial)
        partial.raw_stop_reason = finish
        partial.stop_reason = {"stop": "stop", "tool_calls": "toolUse", "length": "length"}[finish]
        yield DoneEvent(reason=partial.stop_reason, message=partial)
