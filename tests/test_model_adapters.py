import asyncio
from dataclasses import asdict
import json
import unittest

import httpx

from chagent.domain.messages import (
    AssistantMessage, ImageContent, SystemMessage, TextContent, ThinkingContent,
    ToolCall, ToolResultMessage, UserMessage,
)
from chagent.domain.tools import Tool, ToolReference
from chagent.models.base import ModelRequest
from chagent.models.events import DoneEvent, ErrorEvent, StartEvent
from chagent.models.factory import create_model_client
from chagent.models.settings import ModelSettings
from chagent.models.transcript import anthropic_payload, openai_payload


def chat_sse(chunks, *, done=True):
    body = "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
    return body + ("data: [DONE]\n\n" if done else "")


def claude_sse(events):
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def chat_choice(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def claude_events(block, delta=None, *, stop="end_turn"):
    events = [
        {"type": "message_start", "message": {"id": "msg-test", "model": "served-model", "usage": {"input_tokens": 10, "cache_read_input_tokens": 2}}},
        {"type": "content_block_start", "index": 0, "content_block": block},
    ]
    if delta:
        events.append({"type": "content_block_delta", "index": 0, "delta": delta})
    return events + [
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]


class FragmentedBody(httpx.AsyncByteStream):
    def __init__(self, text):
        self.body = text.encode("utf-8")

    async def __aiter__(self):
        for index in range(0, len(self.body), 7):
            yield self.body[index:index + 7]


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, provider="openai"):
        return ModelSettings.from_env({"CHAGENT_PROVIDER": provider, "CHAGENT_API_KEY": "test-provider-secret"})

    def request(self, settings):
        return ModelRequest(settings.model(), [
            SystemMessage("Use tools", 1, tools_added=[Tool("echo", "Echo", {"type": "object"})]),
            UserMessage("hello", 2),
        ])

    async def collect(self, settings, body, *, status=200, headers=None):
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(status, headers=headers or {"content-type": "text/event-stream"}, stream=FragmentedBody(body))

        async with create_model_client(settings, transport=httpx.MockTransport(handler)) as client:
            events = [event async for event in client.stream(self.request(settings))]
        return events, requests

    async def test_chat_text_usage_and_credential_routing(self):
        settings = self.settings()
        events, requests = await self.collect(settings, chat_sse([
            {"id": "response-1", "model": "actual-model", **chat_choice({"content": "你好"})},
            chat_choice(finish="stop"),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                                      "prompt_tokens_details": {"cached_tokens": 2}}},
        ]))
        self.assertIsInstance(events[0], StartEvent)
        self.assertIsInstance(events[-1], DoneEvent)
        final = events[-1].message
        self.assertEqual(final.content[0].text, "你好")
        self.assertEqual(final.response_id, "response-1")
        self.assertEqual(final.response_model, "actual-model")
        self.assertEqual((final.usage.input, final.usage.cache_read, final.usage.total_tokens), (8, 2, 14))
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-provider-secret")
        self.assertTrue(str(requests[0].url).endswith("/v1/chat/completions"))
        body = json.loads(requests[0].content)
        self.assertIn("max_completion_tokens", body)
        self.assertNotIn("test-provider-secret", json.dumps(body))
        self.assertNotIn("test-provider-secret", json.dumps(asdict(final)))

    async def test_interleaved_chat_tool_fragments_and_invalid_json(self):
        events, _ = await self.collect(self.settings("deepseek"), chat_sse([
            chat_choice({"tool_calls": [
                {"index": 0, "id": "a", "type": "function", "function": {"name": "echo", "arguments": '{"text":'}},
                {"index": 1, "id": "b", "type": "function", "function": {"name": "echo", "arguments": '{"text":'}},
            ]}),
            chat_choice({"tool_calls": [{"index": 1, "function": {"arguments": '"second"}'}},
                                         {"index": 0, "function": {"arguments": '"first"}'}}]}, finish="tool_calls"),
        ]))
        self.assertIsInstance(events[-1], DoneEvent)
        self.assertEqual([call.arguments["text"] for call in events[-1].message.content], ["first", "second"])
        malformed, _ = await self.collect(self.settings(), chat_sse([
            chat_choice({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "echo", "arguments": "[1]"}}]}, finish="tool_calls"),
        ]))
        self.assertTrue(malformed[-1].message.content[0].arguments_error)
        self.assertEqual(malformed[-1].message.content[0].arguments, {})

    async def test_missing_terminal_refusal_and_http_errors_are_safe(self):
        for body in (
            chat_sse([chat_choice({"content": "partial"})], done=False),
            chat_sse([chat_choice({"refusal": "no"}, finish="stop")]),
            chat_sse([chat_choice(finish="content_filter")]),
        ):
            with self.subTest(body=body[:30]):
                events, _ = await self.collect(self.settings(), body)
                self.assertIsInstance(events[-1], ErrorEvent)
                self.assertFalse(any(isinstance(event, DoneEvent) for event in events))
        for status in (401, 403, 404, 429, 503, 302):
            events, requests = await self.collect(self.settings(), '{"error":"test-provider-secret"}', status=status,
                                                  headers={"location": "https://other-host.test"})
            self.assertEqual(len(requests), 1)
            self.assertIsInstance(events[-1], ErrorEvent)
            self.assertIn(str(status), events[-1].error.error_message)
            self.assertNotIn("test-provider-secret", json.dumps(asdict(events[-1])))

    async def test_native_anthropic_tool_input_and_usage(self):
        settings = self.settings("anthropic")
        body = claude_sse(claude_events(
            {"type": "tool_use", "id": "tool-a", "name": "echo", "input": {}},
            {"type": "input_json_delta", "partial_json": '{"text":"hello"}'}, stop="tool_use",
        ))
        events, requests = await self.collect(settings, body)
        final = events[-1].message
        self.assertEqual(final.stop_reason, "toolUse")
        self.assertEqual(final.content[0].arguments, {"text": "hello"})
        self.assertEqual(final.usage.total_tokens, 16)
        self.assertEqual(requests[0].headers["x-api-key"], "test-provider-secret")
        self.assertNotIn("authorization", requests[0].headers)
        sent = json.loads(requests[0].content)
        self.assertEqual(sent["tools"][0]["input_schema"], {"type": "object"})
        self.assertTrue(str(requests[0].url).endswith("/v1/messages"))

    async def test_native_anthropic_signed_thinking_and_incomplete_stream(self):
        native = claude_events({"type": "thinking", "thinking": ""}, {"type": "thinking_delta", "thinking": "consider"})
        native.insert(3, {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "opaque"}})
        events, _ = await self.collect(self.settings("anthropic"), claude_sse(native))
        self.assertEqual(events[-1].message.content[0].thinking_signature, "opaque")
        events, _ = await self.collect(self.settings("anthropic"), claude_sse(native[:-1]))
        self.assertIsInstance(events[-1], ErrorEvent)
        self.assertFalse(any(isinstance(event, DoneEvent) for event in events))

    async def test_cancellation_closes_transport_without_error_event(self):
        started, closed = asyncio.Event(), asyncio.Event()

        class Blocking(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                closed.set()

        settings = self.settings()
        async with create_model_client(settings, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Blocking()),
        )) as client:
            async def consume():
                return [event async for event in client.stream(self.request(settings))]
            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(closed.is_set())

    async def test_network_failures_and_endpoint_mismatch_do_not_leak_key(self):
        settings = self.settings()
        calls = []

        async def broken(request):
            calls.append(request)
            raise httpx.ConnectError("diagnostic contains test-provider-secret", request=request)

        async with create_model_client(settings, transport=httpx.MockTransport(broken)) as client:
            events = [event async for event in client.stream(self.request(settings))]
            self.assertIsInstance(events[-1], ErrorEvent)
            self.assertNotIn("test-provider-secret", json.dumps(asdict(events[-1])))
            request = self.request(settings)
            request.model.endpoint = "https://wrong-host.test/v1"
            events = [event async for event in client.stream(request)]
            self.assertIsInstance(events[-1], ErrorEvent)
            self.assertEqual(len(calls), 1)


class TranscriptTests(unittest.TestCase):
    def request(self, provider="openai", api="openai-chat"):
        settings = ModelSettings(provider, "test-model", "https://example.test/v1", "test-secret", api=api)
        return ModelRequest(settings.model(), [
            SystemMessage("base", 1, sections={"rule": "old"}, tools_added=[Tool("old", "old", {})]),
            SystemMessage("extra", 2, sections={"rule": "new"}, tools_added=[Tool("echo", "Echo", {})], tools_removed=[ToolReference("old")]),
            UserMessage([TextContent("hello"), ImageContent("aGVsbG8=", "image/png")], 3),
            AssistantMessage([ToolCall("a", "echo", {"text": "x"}), ToolCall("b", "echo", {"text": "y"})], api, provider, "test-model", 4, "toolUse"),
            ToolResultMessage("a", "echo", [TextContent("one")], True, 5, details={"private": "not for model"}),
            ToolResultMessage("b", "echo", [TextContent("two")], False, 6),
        ])

    def test_chat_conversion_and_latest_system_resources(self):
        payload = openai_payload(self.request())
        self.assertEqual([tool["function"]["name"] for tool in payload["tools"]], ["echo"])
        self.assertEqual(payload["messages"][0]["content"], "base\n\nextra\n\nnew")
        self.assertTrue(payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(payload["messages"][-2]["tool_call_id"], "a")
        self.assertIn("Tool error", payload["messages"][-2]["content"])
        self.assertNotIn("private", json.dumps(payload))

    def test_anthropic_batches_tool_results_and_preserves_error_flag(self):
        payload = anthropic_payload(self.request("anthropic", "anthropic-messages"))
        results = payload["messages"][-1]["content"]
        self.assertEqual([item["tool_use_id"] for item in results], ["a", "b"])
        self.assertTrue(results[0]["is_error"])
        self.assertFalse(results[1]["is_error"])
        self.assertEqual(payload["messages"][0]["content"][1]["source"]["type"], "base64")
        self.assertNotIn("private", json.dumps(payload))

    def test_cross_provider_thinking_is_not_replayed(self):
        request = self.request("anthropic", "anthropic-messages")
        request.messages.insert(3, AssistantMessage([ThinkingContent("old private reasoning", "old signature"), TextContent("answer")],
                                                    "other-api", "other-provider", "other-model", 3, "stop"))
        self.assertNotIn("old signature", json.dumps(anthropic_payload(request)))
