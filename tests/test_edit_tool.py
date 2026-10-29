"""Multi-region edit contract: one call, original-relative, never a guess."""

import asyncio
import json
import os
from pathlib import Path
from time import perf_counter, sleep
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from chagent.context.builder import DEFAULT_INSTRUCTIONS, TOOLS_SECTION, BasicContextBuilder
from chagent.domain.context import ContextPolicy
from chagent.domain.messages import ToolCall, UserMessage
from chagent.domain.runs import StoredMessage
from chagent.models.base import Model
from chagent.tools import edit_diff
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.context import ToolContext
from chagent.tools.edit_diff import (Edit, EditError, _line_spans, _normalized_lines, apply_edits,
                                     fold_text, locate, near_match)
from chagent.tools.executor import ToolExecutor
from chagent.tools.filesystem import PATH_SCHEMA, FileTool, text_result
from chagent.tools.registry import ToolRegistry


def numbered_file(count: int) -> str:
    """A file whose lines are long and distinctive, like real code."""
    lines = []
    for index in range(count):
        step = index % 7
        if step == 0:
            lines.append(f"def handler_{index}(request, context):")
        elif step == 1:
            lines.append(f"    value = compute(request.payload, offset={index})")
        elif step == 2:
            lines.append("    if value is None:")
        elif step == 3:
            lines.append(f"        raise ValueError('missing {index}')")
        elif step == 4:
            lines.append("    return format_result(value, prefix='x')")
        elif step == 5:
            lines.append("")
        else:
            lines.append(f"    # step {index} of the pipeline")
    return "\n".join(lines) + "\n"


def drifted_block(original: str, start: int, count: int) -> str:
    """Copy a block the way a model does: a few lines altered, nothing exact left."""
    block = original.splitlines()[start:start + count]
    for offset in range(3, count, 7):
        block[offset] = block[offset] + "  # edited by hand"
    return "\n".join(block)


class SlowWriteTool(FileTool):
    """A write tool that blocks its thread; only its scheduling is under test."""

    def __init__(self):
        super().__init__("slow_write", "Test-only blocking write.", {"path": PATH_SCHEMA},
                         ["path"], read_only=False)

    def handle(self, filesystem, params):
        sleep(0.2)
        return text_result("done", {"path": params["path"]})



@unittest.skipUnless(os.name == "posix", "POSIX workspace tools")
class EditToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "project"
        self.workspace.mkdir()
        self.context = ToolContext(self.workspace)
        self.executor = ToolExecutor(create_builtin_registry())

    def write(self, name: str, text: str) -> None:
        (self.workspace / name).write_text(text, encoding="utf-8")

    def read(self, name: str) -> str:
        return (self.workspace / name).read_text(encoding="utf-8")

    async def call(self, **arguments):
        return await self.executor.execute(ToolCall("test-call", "edit_file", arguments), context=self.context)

    async def test_one_call_applies_every_edit_against_the_original(self):
        self.write("text_utils.py", 'LABEL = "draft"\nMODE = "safe"\n\n\ndef normalize(value):\n    return value.lower()\n')
        result = await self.call(path="text_utils.py", edits=[
            {"old_text": 'LABEL = "draft"', "new_text": 'LABEL = "published"'},
            {"old_text": "return value.lower()", "new_text": "return value.casefold()"},
            {"old_text": 'MODE = "safe"', "new_text": 'MODE = "strict"'},
        ])
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(
            self.read("text_utils.py"),
            'LABEL = "published"\nMODE = "strict"\n\n\ndef normalize(value):\n    return value.casefold()\n',
        )
        self.assertEqual(result.details["edit_count"], 3)
        self.assertEqual(result.details["strategies"], ["exact", "exact", "exact"])

    async def test_edits_target_the_original_not_each_other(self):
        self.write("chain.py", "value = old\n")
        result = await self.call(path="chain.py", edits=[
            {"old_text": "value = old", "new_text": "value = middle"},
            {"old_text": "value = middle", "new_text": "value = final"},
        ])
        self.assertTrue(result.is_error)
        self.assertIn("edits[1]", result.content[0].text)
        self.assertEqual(self.read("chain.py"), "value = old\n")

    async def test_duplicate_match_is_rejected_with_line_numbers(self):
        self.write("repeat.py", "same\nkeep\nsame\n")
        result = await self.call(path="repeat.py", edits=[{"old_text": "same", "new_text": "changed"}])
        self.assertTrue(result.is_error)
        message = result.content[0].text
        self.assertIn("matches 2 places", message)
        self.assertIn("L1, L3", message)
        self.assertEqual(self.read("repeat.py"), "same\nkeep\nsame\n")

    async def test_trailing_whitespace_drift_still_matches(self):
        self.write("padded.py", "def normalize(value):\n    return value.lower()   \n")
        result = await self.call(path="padded.py", edits=[
            {"old_text": "def normalize(value):\n    return value.lower()\n",
             "new_text": "def normalize(value):\n    return value.casefold()\n"},
        ])
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(result.details["strategies"], ["lines"])
        self.assertEqual(self.read("padded.py"), "def normalize(value):\n    return value.casefold()\n")

    async def test_folded_punctuation_matches_without_rewriting_the_file(self):
        self.write("dash.py", 'suffix = "\u2014"\nother = 1\n')
        result = await self.call(path="dash.py", edits=[{"old_text": 'suffix = "-"', "new_text": 'suffix = "!"'}])
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(result.details["strategies"], ["folded"])
        self.assertEqual(self.read("dash.py"), 'suffix = "!"\nother = 1\n')

    async def test_overlapping_edits_are_rejected_before_writing(self):
        self.write("overlap.py", "alpha beta gamma\n")
        result = await self.call(path="overlap.py", edits=[
            {"old_text": "alpha beta", "new_text": "X"},
            {"old_text": "beta gamma", "new_text": "Y"},
        ])
        self.assertTrue(result.is_error)
        self.assertIn("overlaps", result.content[0].text)
        self.assertEqual(self.read("overlap.py"), "alpha beta gamma\n")

    async def test_malformed_argument_shapes_are_repaired(self):
        self.write("shapes.py", "one\ntwo\nthree\n")
        cases = [
            {"edits": json.dumps([{"old_text": "one", "newText": "1"}])},
            {"edits": {"old_text": "one", "new_text": "1"}},
            {"old_text": "one", "new_text": "1"},
            {"oldText": "one", "newText": "1"},
        ]
        for index, case in enumerate(cases):
            with self.subTest(case=index):
                self.write("shapes.py", "one\ntwo\nthree\n")
                result = await self.call(path="shapes.py", **case)
                self.assertFalse(result.is_error, result.content[0].text)
                self.assertEqual(self.read("shapes.py"), "1\ntwo\nthree\n")

    async def test_legacy_pair_and_edits_together_are_rejected_when_they_disagree(self):
        self.write("both.py", "one\n")
        result = await self.call(path="both.py", old_text="one", new_text="1",
                                 edits=[{"old_text": "one", "new_text": "1"}])
        self.assertFalse(result.is_error)
        result = await self.call(path="both.py", old_text="nope", new_text="2",
                                 edits=[{"old_text": "1", "new_text": "3"}])
        self.assertTrue(result.is_error)
        self.assertIn("not both", result.content[0].text)

    async def test_missing_edits_names_the_expected_shape(self):
        self.write("none.py", "one\n")
        result = await self.call(path="none.py")
        self.assertTrue(result.is_error)
        self.assertIn("requires edits", result.content[0].text)

    async def test_failure_shows_the_closest_lines_and_writes_nothing(self):
        self.write("near.py", "def render(value):\n    suffix = \"...\"\n    return value + suffix\n")
        result = await self.call(path="near.py", edits=[
            {"old_text": "def render(value):\n    suffix = \"!\"\n    return value + suffix\n",
             "new_text": "def render(value):\n    suffix = \"?\"\n    return value + suffix\n"},
        ])
        self.assertTrue(result.is_error)
        message = result.content[0].text
        self.assertIn("edits[0]", message)
        self.assertIn("Closest region", message)
        self.assertIn('suffix = "..."', message)
        self.assertIn("Nothing was written", message)
        self.assertEqual(self.read("near.py"), "def render(value):\n    suffix = \"...\"\n    return value + suffix\n")

    async def test_stale_digest_still_blocks_the_edit(self):
        self.write("stale.py", "one\n")
        result = await self.call(path="stale.py", expected_sha256="0" * 64,
                                 edits=[{"old_text": "one", "new_text": "1"}])
        self.assertTrue(result.is_error)
        self.assertIn("File changed", result.content[0].text)
        self.assertEqual(self.read("stale.py"), "one\n")

    async def test_write_tools_do_not_hold_the_event_loop(self):
        """A blocking handler must not stall the loop: heartbeat, SSE and the
        tool timeout all run there, and none of them can preempt a sync call."""
        registry = ToolRegistry()
        registry.register(SlowWriteTool())
        executor = ToolExecutor(registry)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        try:
            result = await executor.execute(ToolCall("slow", "slow_write", {"path": "x.py"}),
                                           context=self.context)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, "done")
        # 0.2s of blocking work at a 10ms tick is ~20 ticks when the tool runs
        # off the loop and exactly 0 when it runs on it.
        self.assertGreaterEqual(ticks, 5)


class EditDiffUnitTests(unittest.TestCase):
    def test_folding_preserves_offsets(self):
        text = "\u201cquoted\u201d \u2014 spaced\u00a0out"
        self.assertEqual(len(fold_text(text)), len(text))
        self.assertEqual(fold_text(text), '"quoted" - spaced out')

    def test_repeated_region_is_reported_as_ambiguous(self):
        original = "a\nb\nX\nY\na\nb\n"
        with self.assertRaises(EditError) as caught:
            apply_edits(original, [Edit("a\nb", "Q")], "f.py")
        self.assertIn("matches 2 places", str(caught.exception))

    def test_similar_window_is_not_used_below_the_floor(self):
        with self.assertRaises(EditError):
            apply_edits("alpha\nbeta\n", [Edit("alpha\ngamma", "x")], "f.py")

    def test_a_miss_on_a_large_file_stays_bounded(self):
        """The diagnostic path is a cost centre: scoring every window here was
        ~19s of blocked time before the search was narrowed."""
        original = numbered_file(400)
        old = drifted_block(original, 100, 40)
        started = perf_counter()
        with self.assertRaises(EditError) as caught:
            locate(original, Edit(old, ""), 0, "f.py")
        elapsed = perf_counter() - started
        self.assertLess(elapsed, 1.0)
        self.assertIn("Closest region", str(caught.exception))
        self.assertIn("Nothing was written", str(caught.exception))

    def test_anchored_search_agrees_with_the_exhaustive_one(self):
        original = numbered_file(60)
        old = drifted_block(original, 20, 8)
        lines, _ = _line_spans(original)
        normalized, target = _normalized_lines(lines), _normalized_lines(old.splitlines())
        exhaustive = near_match(normalized, target)
        self.assertIsNotNone(exhaustive)
        with patch.object(edit_diff, "SIMILARITY_WORK_LIMIT", 0):
            anchored = near_match(normalized, target)
        self.assertEqual(exhaustive, anchored)

    def test_unrelated_text_is_reported_without_a_similarity_claim(self):
        original = numbered_file(400)
        with self.assertRaises(EditError) as caught:
            locate(original, Edit("nothing_alike_here\n" * 20, ""), 0, "f.py")
        self.assertIn("No region is close enough", str(caught.exception))
        self.assertIn("Nothing was written", str(caught.exception))


class DefaultInstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_instructions_name_the_edit_tool(self):
        """Prompt text is load-bearing; a silent revert once cost two turns per edit."""
        self.assertIs(BasicContextBuilder("unchanged").instructions, "unchanged")
        self.assertIn("edit_file", TOOLS_SECTION)
        builder = BasicContextBuilder(policy=ContextPolicy(safety_tokens=0))
        self.assertEqual(builder.instructions, DEFAULT_INSTRUCTIONS)
        history = [StoredMessage(uuid4(), 1, UserMessage("hi", 1))]
        built = await builder.build(history, Model("test", "test", "fake", 4096, 256), [])
        message = built.request.messages[0]
        self.assertEqual(message.content, "")
        self.assertEqual(message.sections["preamble"], DEFAULT_INSTRUCTIONS)
        self.assertIn("edit_file", message.text())
