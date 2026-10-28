"""Native Anthropic Messages streaming, including tool input and signed thinking."""

from collections.abc import AsyncIterator
import json

import httpx

from chagent.domain.messages import AssistantMessage, TextContent, ThinkingContent, ToolCall
from chagent.domain.usage import Usage
from chagent.models.base import ModelRequest
from chagent.models.events import AssistantMessageEvent, ContentDeltaEvent, ContentEndEvent, ContentStartEvent, DoneEvent, ToolCallEndEvent
from chagent.models.http import HTTPModelClient, ModelProtocolError, event_object, finalize_arguments, sse_events
from chagent.models.transcript import anthropic_payload


class AnthropicClient(HTTPModelClient):
    @property
    def endpoint(self) -> str:
        return self.settings.base_url + "/messages"

    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.settings.api_key, "anthropic-version": "2023-06-01", "Accept": "text/event-stream"}

    def payload(self, request: ModelRequest) -> dict:
        return anthropic_payload(request)

    async def decode(self, response: httpx.Response, partial: AssistantMessage) -> AsyncIterator[AssistantMessageEvent]:
        indices: dict[int, int] = {}
        open_blocks: set[int] = set()
        raw_arguments: dict[int, str] = {}
        streamed_arguments: set[int] = set()
        usage: dict = {}
        started = stopped = False
        finish = None
        async for event in sse_events(response):
            value = event_object(event)
            kind = value.get("type", event.event)
            if kind == "ping":
                continue
            if stopped:
                raise ModelProtocolError("Event received after message_stop")
            if kind == "message_start":
                if started:
                    raise ModelProtocolError("Duplicate message_start")
                started = True
                message = value["message"]
                partial.response_id = message.get("id")
                partial.response_model = message.get("model")
                usage.update(message.get("usage") or {})
                continue
            if not started:
                raise ModelProtocolError("Content received before message_start")
            if kind == "content_block_start":
                provider_index = value["index"]
                if provider_index in indices:
                    raise ModelProtocolError("Duplicate content block")
                block = value["content_block"]
                block_kind = block["type"]
                index = len(partial.content)
                indices[provider_index] = index
                open_blocks.add(provider_index)
                if block_kind == "text":
                    partial.content.append(TextContent(block.get("text", "")))
                elif block_kind in {"thinking", "redacted_thinking"}:
                    partial.content.append(ThinkingContent(
                        block.get("thinking", ""), block.get("data") if block_kind == "redacted_thinking" else block.get("signature"),
                        redacted=block_kind == "redacted_thinking",
                    ))
                elif block_kind == "tool_use":
                    partial.content.append(ToolCall(block["id"], block["name"], {}))
                    raw_arguments[provider_index] = json.dumps(block.get("input", {}))
                else:
                    raise ModelProtocolError("Unsupported Anthropic content block")
                event_kind = "toolcall" if block_kind == "tool_use" else "thinking" if "thinking" in block_kind else "text"
                yield ContentStartEvent(type=event_kind + "_start", content_index=index, partial=partial)
            elif kind == "content_block_delta":
                provider_index = value["index"]
                if provider_index not in open_blocks:
                    raise ModelProtocolError("Delta for a missing or closed content block")
                index = indices[provider_index]
                block, delta = partial.content[index], value["delta"]
                delta_kind = delta["type"]
                if delta_kind == "text_delta" and isinstance(block, TextContent):
                    text = delta["text"]
                    block.text += text
                    event_kind = "text_delta"
                elif delta_kind == "thinking_delta" and isinstance(block, ThinkingContent):
                    text = delta["thinking"]
                    block.thinking += text
                    event_kind = "thinking_delta"
                elif delta_kind == "signature_delta" and isinstance(block, ThinkingContent):
                    block.thinking_signature = (block.thinking_signature or "") + delta["signature"]
                    continue
                elif delta_kind == "input_json_delta" and isinstance(block, ToolCall):
                    if provider_index not in streamed_arguments:
                        raw_arguments[provider_index] = ""
                        streamed_arguments.add(provider_index)
                    text = delta["partial_json"]
                    raw_arguments[provider_index] += text
                    event_kind = "toolcall_delta"
                else:
                    raise ModelProtocolError("Mismatched Anthropic content delta")
                yield ContentDeltaEvent(type=event_kind, content_index=index, delta=text, partial=partial)
            elif kind == "content_block_stop":
                provider_index = value["index"]
                if provider_index not in open_blocks:
                    raise ModelProtocolError("Duplicate or missing content block stop")
                open_blocks.remove(provider_index)
                index = indices[provider_index]
                block = partial.content[index]
                if isinstance(block, ToolCall):
                    block.arguments, block.arguments_error = finalize_arguments(raw_arguments[provider_index])
                    yield ToolCallEndEvent(index, block, partial)
                else:
                    yield ContentEndEvent(type="text_end" if isinstance(block, TextContent) else "thinking_end",
                                          content_index=index, content=block.text if isinstance(block, TextContent) else block.thinking,
                                          partial=partial)
            elif kind == "message_delta":
                candidate = (value.get("delta") or {}).get("stop_reason")
                if candidate is not None:
                    if finish is not None and finish != candidate:
                        raise ModelProtocolError("Conflicting Anthropic stop reasons")
                    finish = candidate
                usage.update(value.get("usage") or {})
            elif kind == "message_stop":
                if open_blocks:
                    raise ModelProtocolError("Message stopped with incomplete content blocks")
                stopped = True
            # Ignore unrecognized top-level extension events, never unknown content blocks.
        if not stopped or finish not in {"end_turn", "stop_sequence", "tool_use", "max_tokens"}:
            raise ModelProtocolError("Anthropic stream is incomplete or refused")
        has_calls = any(isinstance(block, ToolCall) for block in partial.content)
        if has_calls != (finish == "tool_use") and finish != "max_tokens":
            raise ModelProtocolError("Anthropic stop reason does not match tool calls")
        if usage:
            incoming, outgoing = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            read, write = usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0)
            if any(not isinstance(value, int) or value < 0 for value in (incoming, outgoing, read, write)):
                raise ModelProtocolError("Invalid Anthropic token usage")
            partial.usage = Usage(incoming, outgoing, read, write, incoming + outgoing + read + write)
        partial.raw_stop_reason = finish
        partial.stop_reason = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "toolUse", "max_tokens": "length"}[finish]
        yield DoneEvent(reason=partial.stop_reason, message=partial)
