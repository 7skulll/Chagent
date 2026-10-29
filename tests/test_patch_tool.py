"""apply_patch: hunk matching, atomicity across files, create/delete, scope."""

import os
from pathlib import Path
import tempfile
import unittest

from chagent.domain.messages import ToolCall
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.context import ToolContext
from chagent.tools.executor import ToolExecutor


SOURCE = "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"


def patch(*lines: str) -> str:
    return "\n".join(lines) + "\n"


@unittest.skipUnless(os.name == "posix", "POSIX workspace tools")
class ApplyPatchToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "project"
        self.workspace.mkdir()
        (self.workspace / "app").mkdir()
        (self.workspace / "app" / "pricing.py").write_text(SOURCE, encoding="utf-8")
        (self.workspace / "app" / "invoice.py").write_text("from app.pricing import foo\n", encoding="utf-8")
        self.context = ToolContext(self.workspace)
        self.executor = ToolExecutor(create_builtin_registry())

    async def call(self, text: str):
        return await self.executor.execute(ToolCall("test-call", "apply_patch", {"patch": text}),
                                           context=self.context)

    async def test_modifies_multiple_files_in_one_call(self):
        result = await self.call(patch(
            "--- a/app/pricing.py", "+++ b/app/pricing.py", "@@ -1,3 +1,3 @@",
            " def foo():", "-    return 1", "+    return 100", " ",
            "--- a/app/invoice.py", "+++ b/app/invoice.py", "@@ -1 +1 @@",
            "-from app.pricing import foo", "+from app.pricing import bar",
        ))
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual((self.workspace / "app" / "pricing.py").read_text(),
                         "def foo():\n    return 100\n\n\ndef bar():\n    return 2\n")
        self.assertEqual((self.workspace / "app" / "invoice.py").read_text(),
                         "from app.pricing import bar\n")
        self.assertEqual([item["action"] for item in result.details["files"]], ["modified", "modified"])
        self.assertEqual([(item["added"], item["removed"]) for item in result.details["files"]],
                         [(1, 1), (1, 1)])
        self.assertIn("Patched 2 file(s): modified 2", result.content[0].text)

    async def test_one_bad_hunk_writes_nothing(self):
        result = await self.call(patch(
            "--- a/app/pricing.py", "+++ b/app/pricing.py", "@@ -1,3 +1,3 @@",
            " def foo():", "-    return 1", "+    return 100", " ",
            "--- a/app/invoice.py", "+++ b/app/invoice.py", "@@ -1,2 +1,2 @@",
            "-from app.pricing import missing", "+from app.pricing import other",
            " context-that-does-not-exist",
        ))
        self.assertTrue(result.is_error)
        self.assertIn("does not match the file", result.content[0].text)
        self.assertEqual((self.workspace / "app" / "pricing.py").read_text(), SOURCE)
        self.assertEqual((self.workspace / "app" / "invoice.py").read_text(),
                         "from app.pricing import foo\n")

    async def test_creates_and_deletes_files(self):
        created = await self.call(patch(
            "--- /dev/null", "+++ b/app/new_module.py", "@@ -0,0 +1,2 @@",
            "+def add(a, b):", "+    return a + b",
        ))
        self.assertFalse(created.is_error, created.content[0].text)
        self.assertEqual(created.details["files"][0]["action"], "created")
        self.assertEqual((self.workspace / "app" / "new_module.py").read_text(),
                         "def add(a, b):\n    return a + b\n")
        deleted = await self.call(patch(
            "--- a/app/new_module.py", "+++ /dev/null", "@@ -1,2 +0,0 @@",
            "-def add(a, b):", "-    return a + b",
        ))
        self.assertFalse(deleted.is_error, deleted.content[0].text)
        self.assertEqual(deleted.details["files"][0]["action"], "deleted")
        self.assertFalse((self.workspace / "app" / "new_module.py").exists())

    async def test_creating_an_existing_file_is_refused(self):
        result = await self.call(patch(
            "--- /dev/null", "+++ b/app/pricing.py", "@@ -0,0 +1 @@", "+replaced",
        ))
        self.assertTrue(result.is_error)
        self.assertIn("already exists", result.content[0].text)
        self.assertEqual((self.workspace / "app" / "pricing.py").read_text(), SOURCE)

    async def test_new_file_written_as_a_zero_length_hunk_is_accepted(self):
        result = await self.call(patch(
            "--- a/app/from_zero.py", "+++ b/app/from_zero.py", "@@ -0,0 +1,1 @@",
            "+VALUE = 1",
        ))
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(result.details["files"][0]["action"], "created")
        self.assertEqual((self.workspace / "app" / "from_zero.py").read_text(), "VALUE = 1\n")
        missing = await self.call(patch(
            "--- a/app/absent.py", "+++ b/app/absent.py", "@@ -4,1 +4,1 @@",
            "-old", "+new",
        ))
        self.assertTrue(missing.is_error)
        self.assertIn("does not exist", missing.content[0].text)

    async def test_ambiguous_context_is_refused(self):
        target = self.workspace / "app" / "twin.py"
        target.write_text("a\nx = 1\nb\nx = 1\nc\n", encoding="utf-8")
        result = await self.call(patch(
            "--- a/app/twin.py", "+++ b/app/twin.py", "@@ -3,1 +3,1 @@",
            "-x = 1", "+x = 2",
        ))
        self.assertTrue(result.is_error)
        self.assertEqual(target.read_text(), "a\nx = 1\nb\nx = 1\nc\n")

    async def test_header_counts_and_markers_are_tolerated(self):
        result = await self.call(patch(
            "*** Begin Patch",
            "--- a/app/pricing.py", "+++ b/app/pricing.py", "@@ -1,99 +1,99 @@",
            " def foo():", "-    return 1", "+    return 100",
            "*** End Patch",
        ))
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertIn("    return 100", (self.workspace / "app" / "pricing.py").read_text())

    async def test_bare_hunk_header_is_located_by_content(self):
        result = await self.call(patch(
            "--- a/app/invoice.py", "+++ b/app/invoice.py", "@@",
            "-from app.pricing import foo", "+from app.pricing import bar",
        ))
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual((self.workspace / "app" / "invoice.py").read_text(),
                         "from app.pricing import bar\n")

    async def test_paths_outside_the_workspace_are_rejected(self):
        outside = Path(self.temporary.name) / "outside.py"
        outside.write_text("secret\n", encoding="utf-8")
        for header in ("a/../outside.py", str(outside)):
            with self.subTest(header=header):
                result = await self.call(patch(
                    f"--- {header}", f"+++ {header}", "@@ -1 +1 @@", "-secret", "+stolen",
                ))
                self.assertTrue(result.is_error)
                self.assertEqual(outside.read_text(), "secret\n")

    async def test_symlinked_target_is_not_written_through(self):
        outside = Path(self.temporary.name) / "outside.py"
        outside.write_text("secret\n", encoding="utf-8")
        (self.workspace / "link.py").symlink_to(outside)
        result = await self.call(patch(
            "--- a/link.py", "+++ b/link.py", "@@ -1 +1 @@", "-secret", "+stolen",
        ))
        self.assertTrue(result.is_error)
        self.assertEqual(outside.read_text(), "secret\n")
