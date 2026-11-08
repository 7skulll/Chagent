"""One tool result may not decide a whole turn's survival."""

import unittest
from uuid import uuid4

from chagent.context.projection import ProjectionCache
from chagent.context.tokens import HeuristicTokenCounter, estimate_tokens
from chagent.context.truncate import MARKER, truncate_middle
from chagent.domain.artifacts import ArtifactReference
from chagent.domain.messages import ImageContent, TextContent, ToolResultMessage
from chagent.domain.runs import StoredMessage


def numbered_lines(count, width=60):
    return "\n".join(f"line {index} " + "x" * width for index in range(count))


class TruncateMiddleTests(unittest.TestCase):
    def test_text_that_fits_is_returned_unchanged(self):
        text = "short result\nsecond line"
        self.assertEqual(truncate_middle(text, 1000), (text, 0))

    def test_oversized_text_keeps_the_head_the_tail_and_a_marker(self):
        text = numbered_lines(400)
        kept, omitted = truncate_middle(text, 200)
        self.assertGreater(omitted, 0)
        self.assertEqual(len(kept), len(text) - omitted + len(MARKER.format(omitted=omitted)))
        self.assertTrue(kept.startswith("line 0 "))
        self.assertTrue(kept.endswith("x" * 60), "the tail must survive: exit codes and summaries read from the end")
        self.assertIn("characters omitted from the middle", kept)
        self.assertLessEqual(estimate_tokens(kept), 200)

    def test_the_marker_says_how_to_read_the_rest(self):
        """A truncated excerpt with no recovery path is a silent data loss."""
        kept, _ = truncate_middle(numbered_lines(400), 120)
        self.assertIn("stored result is unchanged", kept)
        self.assertIn("offset/limit", kept)

    def test_cuts_snap_to_line_boundaries(self):
        text = numbered_lines(200)
        kept, _ = truncate_middle(text, 150)
        head, rest = kept.split("\n\n[...", 1)
        tail = rest.split("...]\n\n", 1)[1]
        self.assertRegex(head.splitlines()[-1], r"^line \d+ x{60}$")
        self.assertRegex(tail.splitlines()[0], r"^line \d+ x{60}$")

    def test_cjk_is_capped_by_tokens_not_bytes(self):
        """A Chinese result is 3 bytes per character; counting bytes capped it late."""
        text = "这是一段工具输出。" * 1000
        kept, omitted = truncate_middle(text, 300)
        self.assertGreater(omitted, 0)
        self.assertLessEqual(estimate_tokens(kept), 300)

    def test_a_limit_below_the_marker_still_returns_something(self):
        kept, omitted = truncate_middle(numbered_lines(50), 1)
        self.assertGreater(omitted, 0)
        self.assertIn("omitted from the middle", kept)

    def test_invalid_limit_is_refused(self):
        with self.assertRaises(ValueError):
            truncate_middle("x", 0)


class ProjectionTruncationTests(unittest.TestCase):
    def setUp(self):
        self.counter = HeuristicTokenCounter()

    def project(self, message, **limits):
        cache = ProjectionCache(**limits)
        item = StoredMessage(uuid4(), 1, message, uuid4())
        return cache.project(item, self.counter)

    def test_an_oversized_inline_result_is_capped_and_counted(self):
        text = "结果" * 3000
        projection = self.project(
            ToolResultMessage("a", "exec_command", [TextContent(text)], False, 0),
            max_tool_result_tokens=400)
        self.assertGreater(projection.omitted_chars, 0)
        self.assertLessEqual(estimate_tokens(projection.message.content[0].text), 400)
        self.assertEqual(len(projection.message.content), 1)

    def test_a_result_within_the_cap_is_left_alone(self):
        projection = self.project(
            ToolResultMessage("a", "exec_command", [TextContent("ok")], False, 0),
            max_tool_result_tokens=400)
        self.assertEqual(projection.omitted_chars, 0)
        self.assertEqual(projection.message.content[0].text, "ok")

    def test_an_artifact_backed_result_is_bounded_by_its_reference(self):
        """Storage already replaced the body with a reference plus excerpt."""
        reference = ArtifactReference("11111111-1111-1111-1111-111111111111", 90_000, 90_000,
                                      "a" * 64, "preview text", False)
        projection = self.project(
            ToolResultMessage("a", "exec_command", [TextContent("y" * 90_000)], False, 0, artifact=reference),
            max_tool_result_tokens=400)
        self.assertEqual(projection.omitted_chars, 0, "the reference is already capped")
        self.assertIn("Tool output saved as artifact", projection.message.content[0].text)
        self.assertIsNone(projection.message.artifact)

    def test_images_survive_a_text_cap(self):
        image = ImageContent(data="aGk=", mime_type="image/png")
        text = "结果" * 3000
        projection = self.project(
            ToolResultMessage("a", "read_file", [TextContent(text), image], False, 0),
            max_tool_result_tokens=200)
        self.assertEqual([block.type for block in projection.message.content], ["text", "image"])

    def test_several_text_blocks_are_capped_as_one_result(self):
        blocks = [TextContent("块" * 1000) for _ in range(3)]
        projection = self.project(
            ToolResultMessage("a", "exec_command", blocks, False, 0),
            max_tool_result_tokens=300)
        self.assertEqual(len(projection.message.content), 1)
        self.assertLessEqual(estimate_tokens(projection.message.content[0].text), 300)
