from hashlib import sha256
from pathlib import Path
import os
import tempfile
import unittest

from chagent.domain.messages import ToolCall
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.context import ToolContext
from chagent.tools.executor import ToolExecutor


@unittest.skipUnless(os.name == "posix", "POSIX workspace tools")
class FilesystemToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.context = ToolContext(self.workspace)
        self.executor = ToolExecutor(create_builtin_registry(mode="code"))

    async def call(self, name, **arguments):
        return await self.executor.execute(ToolCall("test-call", name, arguments), context=self.context)

    async def test_write_creates_missing_parent_directories(self):
        """B6：写文件不该要求先单独调一次 create_directory。

        实测：模型写 `semis-ai-dashboard/server.py` 时目录还不存在，一次
        FileNotFoundError 报销了一个半小时里的一轮。
        """
        written = await self.call("write_file", path="pkg/src/app.py", content="print(1)\n")
        self.assertFalse(written.is_error, written.content[0].text)
        self.assertEqual((self.workspace / "pkg" / "src" / "app.py").read_text(), "print(1)\n")

    async def test_write_refuses_symlinked_parent(self):
        """自动建目录不能绕过 O_NOFOLLOW：符号链接组件仍然拒绝。

        工具把异常折成错误结果（不抛出），所以断言落在结果与磁盘上。
        """
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, self.workspace / "link")
        refused = await self.call("write_file", path="link/escaped.txt", content="x")
        self.assertTrue(refused.is_error)
        self.assertFalse((outside / "escaped.txt").exists())

    async def test_write_still_rejects_traversal(self):
        refused = await self.call("write_file", path="../escaped.txt", content="x")
        self.assertTrue(refused.is_error)
        self.assertFalse((self.root / "escaped.txt").exists())

    async def test_create_read_edit_and_sha_guard(self):
        original = "第一行\nsecond line\n"
        created = await self.call("write_file", path="notes.txt", content=original)
        self.assertFalse(created.is_error)
        read = await self.call("read_file", path="notes.txt", offset=2, limit=1)
        self.assertIn("L2: second line", read.content[0].text)
        self.assertEqual(read.details["sha256"], sha256(original.encode()).hexdigest())
        edited = await self.call("edit_file", path="notes.txt", old_text="second line", new_text="修改完成", expected_sha256=read.details["sha256"])
        self.assertFalse(edited.is_error)
        self.assertEqual((self.workspace / "notes.txt").read_text(), "第一行\n修改完成\n")
        stale = await self.call("edit_file", path="notes.txt", old_text="修改完成", new_text="bad", expected_sha256=read.details["sha256"])
        self.assertTrue(stale.is_error)
        self.assertNotIn("bad", (self.workspace / "notes.txt").read_text())
        self.assertFalse(list(self.workspace.glob(".chagent-*")))

    async def test_overwrite_is_explicit_and_preserves_mode(self):
        target = self.workspace / "script.sh"
        target.write_text("old")
        target.chmod(0o755)
        result = await self.call("write_file", path="script.sh", content="new")
        self.assertTrue(result.is_error)
        self.assertEqual(target.read_text(), "old")
        result = await self.call("write_file", path="script.sh", content="new", overwrite=True)
        self.assertFalse(result.is_error)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    async def test_ambiguous_edit_leaves_file_unchanged(self):
        target = self.workspace / "repeat.txt"
        target.write_text("same same")
        for old in ("same", "missing"):
            result = await self.call("edit_file", path="repeat.txt", old_text=old, new_text="changed")
            self.assertTrue(result.is_error)
            self.assertEqual(target.read_text(), "same same")

    async def test_traversal_absolute_outside_and_symlinks_are_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("outside")
        (self.workspace / "link").symlink_to(outside)
        (self.workspace / "directory-link").symlink_to(self.root, target_is_directory=True)
        for path in ("../outside.txt", str(outside), "link", "directory-link/outside.txt"):
            for name, extra in (("read_file", {}), ("write_file", {"content": "bad", "overwrite": True}), ("edit_file", {"old_text": "outside", "new_text": "bad"})):
                with self.subTest(path=path, tool=name):
                    self.assertTrue((await self.call(name, path=path, **extra)).is_error)
        self.assertEqual(outside.read_text(), "outside")

    async def test_absolute_inside_and_workspace_alias(self):
        target = self.workspace / "file.txt"
        target.write_text("inside")
        self.assertFalse((await self.call("read_file", path=str(target))).is_error)
        alias = self.root / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        result = await self.executor.execute(ToolCall("alias", "read_file", {"path": str(alias / "file.txt")}), context=ToolContext(alias))
        self.assertFalse(result.is_error)

    async def test_listing_is_paged_sorted_and_marks_symlinks(self):
        for name in ("a", "b", "c"):
            (self.workspace / name).write_text(name)
        (self.workspace / "d").mkdir()
        (self.workspace / "e").symlink_to(self.workspace / "a")
        first = await self.call("list_dir", limit=2)
        second = await self.call("list_dir", offset=first.details["next_offset"], limit=10)
        self.assertTrue(first.content[0].text.endswith("a\nb"))
        self.assertIn("Next offset: 2", first.content[0].text)
        self.assertTrue(second.content[0].text.endswith("c\nd/\ne@"))
        self.assertFalse(second.details["has_more"])

    async def test_input_and_output_limits_and_nonregular_files(self):
        (self.workspace / "huge.txt").write_bytes(b"a" * (1_048_576 + 1))
        self.assertTrue((await self.call("read_file", path="huge.txt")).is_error)
        (self.workspace / "long.txt").write_text("a" * 20_000)
        result = await self.call("read_file", path="long.txt")
        self.assertFalse(result.details["output_truncated"])
        self.assertIn("a" * 20_000, result.content[0].text)
        os.mkfifo(self.workspace / "pipe")
        self.assertTrue((await self.call("read_file", path="pipe")).is_error)
        self.assertTrue((await self.call("read_file", path=".")).is_error)
        self.assertTrue((await self.executor.execute(ToolCall("no-context", "read_file", {"path": "x"}))).is_error)

    async def test_context_is_not_shared_between_workspaces(self):
        other = self.root / "other"
        other.mkdir()
        (self.workspace / "same.txt").write_text("first")
        (other / "same.txt").write_text("second")
        first = await self.call("read_file", path="same.txt")
        second = await self.executor.execute(ToolCall("other", "read_file", {"path": "same.txt"}), context=ToolContext(other))
        self.assertIn("L1: first", first.content[0].text)
        self.assertIn("L1: second", second.content[0].text)
