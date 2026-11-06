"""Workspace evidence is content based and does not follow links."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chagent.workspace_manifest import diff, snapshot


class WorkspaceManifestTests(unittest.TestCase):
    def test_added_modified_deleted_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (root / "modified.txt").write_text("old")
            (root / "deleted.txt").write_text("remove")
            (Path(outside) / "secret").write_text("outside")
            (root / "link").symlink_to(Path(outside) / "secret")
            before = snapshot(root)
            (root / "modified.txt").write_text("new")
            (root / "deleted.txt").unlink()
            (root / "added.txt").write_text("create")
            after = snapshot(root)
            changes = diff(before, after)
            self.assertTrue(changes["complete"])
            self.assertEqual(changes["counts"], {"added": 1, "modified": 1, "deleted": 1})
            self.assertEqual([item["path"] for item in changes["added"]], ["added.txt"])
            self.assertEqual([item["path"] for item in changes["modified"]], ["modified.txt"])
            self.assertEqual([item["path"] for item in changes["deleted"]], ["deleted.txt"])
            self.assertNotIn("link", [item["path"] for item in after["files"]])
            self.assertEqual(before["skipped_symlinks"], 1)

    def test_scan_limit_never_claims_complete_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").write_text("one")
            (root / "b").write_text("two")
            with patch("chagent.workspace_manifest.MAX_FILES", 1):
                partial = snapshot(root)
            self.assertFalse(partial["complete"])
            self.assertEqual(partial["issues"][0]["reason"], "scan_limit")
            self.assertFalse(diff(partial, snapshot(root))["complete"])
