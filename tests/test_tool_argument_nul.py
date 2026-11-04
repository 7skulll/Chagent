import unittest

from chagent.models.http import finalize_arguments


class ToolArgumentNulTests(unittest.TestCase):
    def test_nul_is_a_recoverable_tool_error(self):
        arguments, error = finalize_arguments('{"cmd":"tr \\u0000 x"}')
        self.assertEqual(arguments, {})
        self.assertIn("NUL", error)

    def test_nested_nul_is_rejected(self):
        arguments, error = finalize_arguments('{"items":[{"value":"\\u0000"}]}')
        self.assertEqual(arguments, {})
        self.assertIn("NUL", error)
