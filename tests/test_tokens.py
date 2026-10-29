"""Token counting: a real encoder when available, bytes otherwise, never mixed."""

import io
import json
from contextlib import redirect_stderr
import unittest

from chagent.context.tokens import (
    BpeTokenCounter, ByteTokenCounter, TokenizerUnavailable, encoding_for, select_counter, wire_text,
)
from chagent.domain.messages import (
    AssistantMessage, SystemMessage, TextContent, ToolCall, ToolResultMessage, UserMessage,
)
from chagent.domain.tools import Tool
from chagent.models.base import Model, ModelRequest
from chagent.models.transcript import openai_payload


def tool(name="read_file"):
    return Tool(name, f"{name} description", {"type": "object", "properties": {"path": {"type": "string"}}})


def history():
    return [UserMessage("hello", 0),
            AssistantMessage([ToolCall("a", "read_file", {"path": "x"})], "fake", "p", "m", 0, "toolUse"),
            ToolResultMessage("a", "read_file", [TextContent("body")], False, 0)]


class WireShapeTests(unittest.TestCase):
    def test_the_estimate_measures_the_wire_body(self):
        """Host metadata and timestamps never reach a provider, so they cost nothing."""
        text = wire_text(UserMessage("hello", 12345))
        self.assertNotIn("timestamp", text)
        self.assertEqual(json.loads(text), {"role": "user", "content": "hello"})

    def test_system_message_serializes_like_the_adapter(self):
        message = SystemMessage("", 0, sections={"preamble": "do the task", "rules": "be honest"},
                                tools_added=[tool()])
        request = ModelRequest(model=Model("m", "p", "openai-chat", 4096, 256), messages=[message])
        payload = openai_payload(request)
        body = {"role": "system", "content": payload["messages"][0]["content"], "tools": payload["tools"]}
        self.assertEqual(json.loads(wire_text(message)), body)


class ByteCounterTests(unittest.TestCase):
    def test_is_additive_and_grows_with_content(self):
        counter = ByteTokenCounter()
        messages = history()
        self.assertEqual(counter.count(messages), sum(counter.count([message]) for message in messages))
        self.assertLess(counter.count([UserMessage("hi", 0)]), counter.count([UserMessage("hi" * 100, 0)]))


class SelectionTests(unittest.TestCase):
    def test_fake_and_disabled_models_use_bytes(self):
        self.assertIsInstance(select_counter(Model("fake", "local", "fake", 4096, 256)), ByteTokenCounter)
        self.assertIsInstance(select_counter(Model("gpt-5", "openai", "openai-chat", 4096, 256),
                                             {"CHAGENT_TOKENIZER": "bytes"}), ByteTokenCounter)

    def test_encoding_follows_the_model_family(self):
        self.assertEqual(encoding_for("gpt-5.4-mini"), "o200k_base")
        self.assertEqual(encoding_for("gpt-4o"), "o200k_base")
        self.assertEqual(encoding_for("deepseek-flash"), "cl100k_base")

    def test_an_unavailable_tokenizer_falls_back_and_says_so(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            counter = select_counter(Model("m", "p", "openai-chat", 4096, 256),
                                     {"CHAGENT_TOKENIZER": "definitely_not_an_encoding"})
        self.assertIsInstance(counter, ByteTokenCounter)
        self.assertIn("fallback", stderr.getvalue())


class BpeCounterTests(unittest.TestCase):
    def setUp(self):
        try:
            self.counter = BpeTokenCounter("cl100k_base")
        except TokenizerUnavailable as error:
            self.skipTest(str(error))

    def test_counting_is_additive(self):
        messages = history()
        self.assertEqual(self.counter.count(messages), sum(self.counter.count([m]) for m in messages))

    def test_tool_declarations_cost_more_than_their_text(self):
        plain = self.counter.count([SystemMessage("rules", 0)])
        declared = self.counter.count([SystemMessage("rules", 0, tools_added=[tool()])])
        self.assertGreater(declared - plain, self.counter.TOOL_DECLARATION_OVERHEAD)

    def test_english_bytes_are_the_documented_overestimate(self):
        """Bytes overcount English by ~3x; that is why the BPE counter exists."""
        message = UserMessage("Complete the user's task with the available tools, and report only "
                              "what a tool confirmed. Do not invent results.", 0)
        self.assertGreater(ByteTokenCounter().count([message]) / self.counter.count([message]), 2.5)

    def test_a_message_costs_its_text_plus_the_json_envelope(self):
        """The counter measures the wire JSON, so only the envelope is added on top."""
        text = "Complete the user's task with the available tools and report only what a tool confirmed."
        counted = self.counter.count([UserMessage(text, 0)]) - self.counter.MESSAGE_OVERHEAD
        encoded = len(self.counter._encoder.encode(text, disallowed_special=()))
        self.assertGreaterEqual(counted, encoded)
        self.assertLess(counted - encoded, 20)


if __name__ == "__main__":
    unittest.main()
