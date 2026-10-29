"""Boundary tests for grep and glob: scope, symlinks, binary files, limits."""

import os
from pathlib import Path
import tempfile
import unittest

from chagent.domain.messages import ToolCall
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.context import ToolContext
from chagent.tools.executor import ToolExecutor


@unittest.skipUnless(os.name == "posix", "POSIX workspace tools")
class SearchToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        (self.workspace / "app").mkdir()
        (self.workspace / "app" / "pricing.py").write_text(
            "TIERS = ((100, 0.05), (1000, 0.15))\n\n\ndef discount_rate(total):\n    return 0.05\n", encoding="utf-8")
        (self.workspace / "app" / "invoice.py").write_text(
            "from app.pricing import discount_rate\n\n\ndef render(items):\n    return discount_rate(0)\n", encoding="utf-8")
        (self.workspace / "notes.md").write_text("discount policy: none\n", encoding="utf-8")
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "config").write_text("discount=ignored\n", encoding="utf-8")
        (self.workspace / "blob.bin").write_bytes(b"discount\x00\x01\x02")
        self.context = ToolContext(self.workspace)
        self.executor = ToolExecutor(create_builtin_registry())

    async def call(self, name, **arguments):
        return await self.executor.execute(ToolCall("test-call", name, arguments), context=self.context)

    async def test_grep_reports_locations_and_skips_binary_and_git(self):
        result = await self.call("grep", pattern="discount_rate")
        self.assertFalse(result.is_error)
        text = result.content[0].text
        self.assertIn("app/pricing.py:4: def discount_rate(total):", text)
        self.assertIn("app/invoice.py:1: from app.pricing import discount_rate", text)
        self.assertNotIn("blob.bin", text)
        self.assertNotIn(".git", text)
        self.assertEqual(result.details["file_count"], 2)
        self.assertFalse(result.details["truncated"])

    async def test_grep_include_case_and_regex_are_honoured(self):
        only_python = await self.call("grep", pattern="discount", include="*.py")
        self.assertNotIn("notes.md", only_python.content[0].text)
        case_sensitive = await self.call("grep", pattern="DISCOUNT")
        self.assertIn("[No match]", case_sensitive.content[0].text)
        insensitive = await self.call("grep", pattern="DISCOUNT", case_sensitive=False)
        self.assertIn("notes.md", insensitive.content[0].text)
        invalid = await self.call("grep", pattern="(unclosed")
        self.assertTrue(invalid.is_error)
        self.assertIn("Invalid regular expression", invalid.content[0].text)

    async def test_grep_limit_marks_truncation(self):
        exact = await self.call("grep", pattern="discount_rate")
        total = exact.details["match_count"]
        self.assertFalse(exact.details["truncated"], "an exact fit is not truncation")
        result = await self.call("grep", pattern="discount_rate", max_results=total - 1)
        self.assertTrue(result.details["truncated"])
        self.assertEqual(result.details["match_count"], total - 1)
        self.assertIn("[Truncated", result.content[0].text)

    async def test_grep_single_file_and_scope_violations(self):
        single = await self.call("grep", pattern="TIERS", path="app/pricing.py")
        self.assertIn("app/pricing.py:1:", single.content[0].text)
        outside = self.root / "outside.txt"
        outside.write_text("discount outside\n", encoding="utf-8")
        for path in ("../outside.txt", str(outside), "link"):
            (self.workspace / "link").unlink(missing_ok=True)
            (self.workspace / "link").symlink_to(outside)
            with self.subTest(path=path):
                result = await self.call("grep", pattern="outside", path=path)
                self.assertTrue(result.is_error)

    async def test_grep_does_not_follow_symlinked_files(self):
        outside = self.root / "secret.txt"
        outside.write_text("discount secret\n", encoding="utf-8")
        (self.workspace / "linked.txt").symlink_to(outside)
        result = await self.call("grep", pattern="secret")
        self.assertNotIn("linked.txt", result.content[0].text)

    async def test_glob_lists_sorted_relative_paths(self):
        result = await self.call("glob", pattern="**/*.py")
        self.assertFalse(result.is_error)
        self.assertEqual(result.details["paths"], ["app/invoice.py", "app/pricing.py"])
        self.assertEqual(result.content[0].text.splitlines()[1], "Paths: 2")

    async def test_glob_excludes_symlinks_and_rejects_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        listed = await self.call("glob", pattern="**/*")
        self.assertNotIn("escape", listed.content[0].text)
        self.assertTrue((await self.call("glob", pattern="../**/*.txt")).is_error)
        self.assertTrue((await self.call("glob", pattern=str(outside / "*"))).is_error)

    async def test_glob_limit_and_directories(self):
        limited = await self.call("glob", pattern="**/*", max_results=1)
        self.assertEqual(limited.details["count"], 1)
        self.assertTrue(limited.details["truncated"])
        self.assertNotIn("app", limited.details["paths"])
        with_dirs = await self.call("glob", pattern="*", include_dirs=True)
        self.assertIn("app", with_dirs.details["paths"])
