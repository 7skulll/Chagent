"""Workspace evidence is content based and does not follow links."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chagent.workspace_manifest import ManifestCache, diff, snapshot


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


class ManifestCacheTests(unittest.TestCase):
    """An unchanged file is settled by a stat, not by reading it again."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def hashes(self):
        calls = []
        real = __import__("chagent.workspace_manifest", fromlist=["digest_file"]).digest_file

        def counted(path):
            calls.append(path)
            return real(path)

        return calls, patch("chagent.workspace_manifest.digest_file", counted)

    def test_unchanged_files_are_not_read_twice(self):
        for name in ("a.txt", "b.txt", "sub/c.txt"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        cache = ManifestCache()
        calls, counted = self.hashes()
        with counted:
            first = snapshot(self.root, cache)
            self.assertEqual(len(calls), 3)
            calls.clear()
            second = snapshot(self.root, cache)
        self.assertEqual(calls, [])
        self.assertEqual(first["files"], second["files"])
        self.assertEqual(len(cache), 3)

    def test_changed_file_is_rehashed_and_reported(self):
        path = self.root / "note.txt"
        path.write_text("one")
        cache = ManifestCache()
        before = snapshot(self.root, cache)
        path.write_text("two")           # same file, different bytes
        calls, counted = self.hashes()
        with counted:
            after = snapshot(self.root, cache)
        self.assertEqual([p.name for p in calls], ["note.txt"])
        self.assertEqual(diff(before, after)["counts"], {"added": 0, "modified": 1, "deleted": 0})

    def test_deleted_paths_are_forgotten(self):
        keep, drop = self.root / "keep.txt", self.root / "drop.txt"
        keep.write_text("keep")
        drop.write_text("drop")
        cache = ManifestCache()
        snapshot(self.root, cache)
        self.assertEqual(len(cache), 2)
        drop.unlink()
        snapshot(self.root, cache)
        self.assertEqual(len(cache), 1)
        # A path that comes back is hashed again rather than trusted.
        drop.write_text("drop")
        calls, counted = self.hashes()
        with counted:
            snapshot(self.root, cache)
        self.assertEqual([p.name for p in calls], ["drop.txt"])

    def test_partial_scan_keeps_what_it_could_not_see(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.root / name).write_text(name)
        cache = ManifestCache()
        snapshot(self.root, cache)
        with patch("chagent.workspace_manifest.MAX_FILES", 1):
            partial = snapshot(self.root, cache)
        self.assertFalse(partial["complete"])
        # Dropping entries here would make the next run re-hash files that
        # simply were not reached, and report them as new.
        self.assertEqual(len(cache), 3)

    def test_sliced_hashing_matches_a_serial_scan(self):
        for index in range(40):
            (self.root / f"{index:02d}.txt").write_text(str(index))
        serial = snapshot(self.root, ManifestCache())
        with patch("chagent.workspace_manifest.HASH_WORKERS", 4):
            sliced = snapshot(self.root, ManifestCache())
        self.assertEqual(serial["files"], sliced["files"])
        self.assertTrue(sliced["complete"])
