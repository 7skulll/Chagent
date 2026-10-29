"""Repeat detection must stop loops without blocking legitimate verification."""

import unittest

from chagent.domain.messages import TextContent, ToolCall
from chagent.tools.base import AgentToolResult
from chagent.tools.echo import EchoTool
from chagent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
from chagent.tools.progress import ProgressGuard

from uuid import uuid4

RUN = uuid4()


def call(name, arguments, identity="1"):
    return ToolCall(id=identity, name=name, arguments=arguments)


def ok(text="result"):
    return AgentToolResult(content=[TextContent(text)])


def failed():
    return AgentToolResult(content=[TextContent("error")], is_error=True)


class ProgressGuardTests(unittest.TestCase):
    def test_third_identical_call_is_refused_with_the_previous_result(self):
        guard, tool = ProgressGuard(), ReadFileTool()
        read = call("read_file", {"path": "a.txt"})
        self.assertTrue(guard.check(RUN, read, tool).execute)
        guard.completed(RUN, read, tool, ok("file contents"))
        self.assertTrue(guard.check(RUN, read, tool).execute)
        refused = guard.check(RUN, read, tool)
        self.assertFalse(refused.execute)
        self.assertFalse(refused.stop)
        self.assertTrue(refused.result.is_error)
        self.assertTrue(refused.result.details["repeated"])
        self.assertEqual(refused.result.details["count"], 3)
        text = refused.result.content[0].text
        self.assertIn("Repeated tool call", text)
        self.assertIn("file contents", text)          # summary, not the full result
        self.assertIn("change the arguments", text)   # actionable next step

    def test_the_second_refusal_ends_the_attempt(self):
        guard, tool = ProgressGuard(), ReadFileTool()
        read = call("read_file", {"path": "a.txt"})
        for _ in range(2):
            guard.check(RUN, read, tool)
            guard.completed(RUN, read, tool, ok())
        refused = guard.check(RUN, read, tool)
        self.assertFalse(refused.stop)
        self.assertTrue(guard.check(RUN, read, tool).stop)

    def test_different_arguments_are_never_repeats(self):
        guard, tool = ProgressGuard(), EditFileTool()
        first = call("edit_file", {"path": "a.py", "edits": [{"oldText": "a", "newText": "b"}]})
        second = call("edit_file", {"path": "a.py", "edits": [{"oldText": "c", "newText": "d"}]})
        # Two full rounds execute: if the two edits shared a signature, the
        # third check overall would already be refused.
        for _ in range(2):
            self.assertTrue(guard.check(RUN, first, tool).execute)
            self.assertTrue(guard.check(RUN, second, tool).execute)
        self.assertFalse(guard.check(RUN, first, tool).execute)

    def test_a_read_is_identified_by_path_offset_and_limit(self):
        guard, tool = ProgressGuard(), ReadFileTool()
        head = call("read_file", {"path": "a.txt"})
        tail = call("read_file", {"path": "a.txt", "offset": 100, "limit": 40})
        # Same file, different range: one counter each, not one shared counter.
        for _ in range(2):
            self.assertTrue(guard.check(RUN, head, tool).execute)
            self.assertTrue(guard.check(RUN, tail, tool).execute)
        self.assertFalse(guard.check(RUN, head, tool).execute)
        self.assertFalse(guard.check(RUN, tail, tool).execute)

    def test_a_successful_write_starts_a_new_attempt(self):
        guard = ProgressGuard()
        read, write = ReadFileTool(), WriteFileTool()
        repeat = call("read_file", {"path": "a.txt"})
        for _ in range(2):
            guard.check(RUN, repeat, read)
            guard.completed(RUN, repeat, read, ok())
        guard.completed(RUN, call("write_file", {"path": "a.txt", "content": "x"}), write, ok())
        # The same read after a change is verification, not a loop.
        decision = guard.check(RUN, repeat, read)
        self.assertTrue(decision.execute)
        self.assertEqual(decision.stop, False)

    def test_a_failed_write_keeps_counting(self):
        guard = ProgressGuard()
        read, write = ReadFileTool(), WriteFileTool()
        repeat = call("read_file", {"path": "a.txt"})
        for _ in range(2):
            guard.check(RUN, repeat, read)
        guard.completed(RUN, call("write_file", {"path": "a.txt", "content": "x"}), write, failed())
        self.assertFalse(guard.check(RUN, repeat, read).execute)

    def test_non_mutating_tools_keep_counting(self):
        guard, tool = ProgressGuard(), EchoTool()
        repeat = call("echo", {"text": "same"})
        for _ in range(2):
            guard.check(RUN, repeat, tool)
            guard.completed(RUN, repeat, tool, ok())
        self.assertFalse(guard.check(RUN, repeat, tool).execute)
        self.assertFalse(tool.mutates_workspace)

    def test_runs_do_not_share_counters(self):
        guard, tool = ProgressGuard(), ReadFileTool()
        repeat = call("read_file", {"path": "a.txt"})
        other = uuid4()
        for _ in range(2):
            guard.check(RUN, repeat, tool)
        self.assertTrue(guard.check(other, repeat, tool).execute)
        guard.release(other)
        self.assertTrue(guard.check(other, repeat, tool).execute)

    def test_zero_disables_the_guard(self):
        guard, tool = ProgressGuard(limit=0), ReadFileTool()
        repeat = call("read_file", {"path": "a.txt"})
        for _ in range(10):
            self.assertTrue(guard.check(RUN, repeat, tool).execute)


if __name__ == "__main__":
    unittest.main()
