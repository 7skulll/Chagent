import unittest

from chagent.models.http import finalize_arguments
from chagent.domain.codec import message_to_dict
from chagent.domain.messages import AssistantMessage, TextContent
from chagent.domain.json_types import postgres_json_safe


class ToolArgumentNulTests(unittest.TestCase):
    def test_nul_is_a_recoverable_tool_error(self):
        arguments, error = finalize_arguments('{"cmd":"tr \\u0000 x"}')
        self.assertEqual(arguments, {})
        self.assertIn("NUL", error)

    def test_nested_nul_is_rejected(self):
        arguments, error = finalize_arguments('{"items":[{"value":"\\u0000"}]}')
        self.assertEqual(arguments, {})
        self.assertIn("NUL", error)

    def test_model_text_nul_is_safe_for_postgres(self):
        message = AssistantMessage(content=[TextContent("tr '\\0' and " + "\x00")],
                                   api="test", provider="test", model="test", timestamp=0, stop_reason="stop")
        self.assertEqual(message_to_dict(message)["content"][0]["text"], "tr '\\0' and \\0")
        self.assertEqual(postgres_json_safe({"nested": ["x\x00y"]}), {"nested": ["x\\0y"]})

    def test_literal_unicode_escape_is_escaped(self):
        """A command that prints ``\\u0000`` used to kill the run that ran it.

        The text is six ASCII characters and valid JSON, but jsonb decodes it
        into the one byte it cannot store. A 24-turn Java build ended as
        ``storage_error`` on exactly this: ``tr '\\u0000'`` inside a /proc loop.
        """
        printed = "for p in /proc/[0-9]*; do tr '\\u0000' < $p; done"
        self.assertEqual(postgres_json_safe({"cmd": printed}),
                         {"cmd": "for p in /proc/[0-9]*; do tr '\\\\u0000' < $p; done"})

    def test_both_nul_shapes_in_one_string(self):
        self.assertEqual(postgres_json_safe("a\x00b\\u0000c"), "a\\0b\\\\u0000c")
