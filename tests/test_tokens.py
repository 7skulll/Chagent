"""Token estimation: one coarse heuristic over the wire body, never raw bytes."""

import json
import unittest

from chagent.context.tokens import (
    ASCII_BYTES_PER_TOKEN, HeuristicTokenCounter, MESSAGE_OVERHEAD, TOOL_DECLARATION_OVERHEAD,
    WIDE_CHAR_TOKENS, estimate_tokens, select_counter, wire_text,
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


class EstimateTests(unittest.TestCase):
    """The heuristic is coarse on purpose; what it must never be is bytes-as-tokens."""

    def test_ascii_text_costs_about_a_quarter_of_its_bytes(self):
        text = "The quick brown fox jumps over the lazy dog. " * 40
        self.assertEqual(estimate_tokens(text), -(-len(text) // ASCII_BYTES_PER_TOKEN))

    def test_cjk_is_charged_per_character_not_per_byte(self):
        """The old byte counter read Chinese as 3x the tokens it really needs."""
        text = "我们需要把上下文预算做得更稳一些" * 20
        tokens = estimate_tokens(text)
        self.assertEqual(tokens, len(text) * WIDE_CHAR_TOKENS)
        self.assertLess(tokens, len(text.encode("utf-8")))
        self.assertGreater(tokens, len(text.encode("utf-8")) // ASCII_BYTES_PER_TOKEN)

    def test_mixed_text_sums_both_rates(self):
        text = "abc" * 100 + "你好" * 100
        self.assertEqual(estimate_tokens(text), 300 // ASCII_BYTES_PER_TOKEN + 200 * WIDE_CHAR_TOKENS)

    def test_empty_text_costs_nothing(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_english_prose_is_not_double_counted(self):
        """A 900-byte English sample measured 201 cl100k tokens; stay in that band."""
        text = "The quick brown fox jumps over the lazy dog. " * 20
        self.assertLessEqual(estimate_tokens(text), 240)
        self.assertGreaterEqual(estimate_tokens(text), 190)


class CounterTests(unittest.TestCase):
    def test_is_additive_and_grows_with_content(self):
        counter = HeuristicTokenCounter()
        messages = history()
        self.assertEqual(counter.count(messages), sum(counter.count([message]) for message in messages))
        self.assertLess(counter.count([UserMessage("hi", 0)]), counter.count([UserMessage("hi" * 100, 0)]))

    def test_each_message_pays_a_fixed_overhead(self):
        counter = HeuristicTokenCounter()
        one = counter.count([UserMessage("", 0)])
        two = counter.count([UserMessage("", 0), UserMessage("", 0)])
        self.assertEqual(two - one, one, "the per-message cost is constant")
        self.assertGreaterEqual(one, MESSAGE_OVERHEAD, "role markers are not free")

    def test_tool_declarations_are_charged_per_tool(self):
        counter = HeuristicTokenCounter()

        def counted(tools):
            return counter.count([SystemMessage("", 0, sections={"tools": "x"}, tools_added=tools)])

        # Same section text, one more declaration: the schema plus its wrapper.
        self.assertGreater(counted([tool("a"), tool("b")]), counted([tool("a")]))
        self.assertGreaterEqual(counted([tool()]) - counted([]), TOOL_DECLARATION_OVERHEAD)


class SelectionTests(unittest.TestCase):
    def test_one_counter_for_every_model(self):
        """A missing encoder used to change when the same task compacted."""
        for model in (Model("fake", "local", "fake", 4096, 256),
                      Model("deepseek-flash", "deepseek", "openai-chat", 65536, 8192),
                      Model("gpt-5", "openai", "openai-chat", 400_000, 8192)):
            self.assertIsInstance(select_counter(model), HeuristicTokenCounter)
        self.assertEqual(select_counter(Model("gpt-5", "openai", "openai-chat", 4096, 256)).name,
                         HeuristicTokenCounter.name)
