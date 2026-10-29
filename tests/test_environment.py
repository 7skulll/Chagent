"""Run-static environment snapshot: bounded, deterministic, and honest about it."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from chagent.context import environment
from chagent.context.environment import AGENTS_MAX_BYTES, TREE_MAX_BYTES, collect

HAS_GIT = shutil.which("git") is not None


class EnvironmentSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, relative: str, content: str = "") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_reports_working_directory_and_two_levels_of_directories(self):
        self.write("app/pricing.py")
        self.write("app/invoice.py")
        self.write("app/nested/deep.py")
        self.write("app/nested/deeper/too_deep.py")
        self.write("tests/test_pricing.py")
        facts = collect(self.root)
        self.assertIn(f"Working directory: {self.root}", facts.text)
        self.assertIn("app/ (2 files: invoice.py, pricing.py)", facts.text)
        self.assertIn("nested/ (1 file: deep.py)", facts.text)
        self.assertIn("tests/ (1 file: test_pricing.py)", facts.text)
        self.assertNotIn("deeper", facts.text)
        self.assertIn("do not list those directories again", facts.text)

    def test_budgets_scale_with_the_model_window(self):
        """4 KB of facts inside an 8 K window would be a context rejection."""
        self.assertEqual(environment.budgets_for(None), (environment.TREE_MAX_BYTES, environment.AGENTS_MAX_BYTES))
        self.assertEqual(environment.budgets_for(1_000_000), (environment.TREE_MAX_BYTES, environment.AGENTS_MAX_BYTES))
        small_tree, small_agents = environment.budgets_for(8_192)
        self.assertLess(small_tree, environment.TREE_MAX_BYTES)
        self.assertLess(small_agents, environment.AGENTS_MAX_BYTES)
        self.assertGreaterEqual(small_tree, environment.TREE_MIN_BYTES)
        wide_tree, _ = environment.budgets_for(65_536)
        self.assertGreater(wide_tree, small_tree)

    def test_dotfiles_are_not_listed(self):
        """`.env` and `.DS_Store` are noise, and credential file names should not travel."""
        self.write(".env", "SECRET=1\n")
        self.write(".DS_Store", "junk")
        self.write("app/pricing.py")
        facts = collect(self.root)
        self.assertNotIn(".env", facts.text)
        self.assertNotIn(".DS_Store", facts.text)
        self.assertIn("pricing.py", facts.text)

    def test_vendored_and_hidden_directories_are_skipped(self):
        for directory in (".git", "node_modules", "__pycache__", ".venv", "build", "app"):
            (self.root / directory).mkdir()
        facts = collect(self.root)
        for hidden in (".git", "node_modules", "__pycache__", ".venv", "build"):
            self.assertNotIn(f"{hidden}/", facts.text)
        self.assertIn("app/", facts.text)

    def test_tree_is_bounded_by_bytes_and_reports_truncation(self):
        for index in range(400):
            (self.root / f"directory-{index:03d}-{'x' * 12}").mkdir()
        facts = collect(self.root)
        tree = facts.text.split("Directory tree", 1)[1]
        self.assertLessEqual(len(tree.encode("utf-8")), TREE_MAX_BYTES + 120)
        self.assertTrue(facts.tree_truncated)
        self.assertIn("truncated", tree)
        self.assertLessEqual(facts.tree_entries, environment.TREE_MAX_ENTRIES)

    def test_missing_workspace_degrades_without_raising(self):
        facts = collect(self.root / "does-not-exist")
        self.assertIn("Working directory", facts.text)
        self.assertNotIn("Directory tree", facts.text)
        self.assertEqual(facts.tree_entries, 0)

    def test_snapshot_is_deterministic(self):
        self.write("app/pricing.py")
        self.write("app/invoice.py")
        self.assertEqual(collect(self.root).text, collect(self.root).text)

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_git_branch_and_status_are_summarised(self):
        self.run_git("init", "-q")
        self.write("app/pricing.py", "value = 1\n")
        self.run_git("add", ".")
        self.run_git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
        self.write("app/pricing.py", "value = 2\n")
        self.write("app/new.py", "value = 3\n")
        facts = collect(self.root)
        self.assertTrue(facts.git_repo)
        self.assertIn("git: branch", facts.text)
        self.assertIn("1 changed", facts.text)
        self.assertIn("1 untracked", facts.text)
        self.assertIn("app/pricing.py", facts.text)

    def test_a_directory_without_a_repository_omits_the_git_line(self):
        self.write("app/pricing.py")
        facts = collect(self.root)
        self.assertFalse(facts.git_repo)
        self.assertNotIn("git:", facts.text)

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_agents_files_are_concatenated_deepest_first(self):
        self.run_git("init", "-q")
        self.write("AGENTS.md", "root rule\n")
        workspace = self.root / "packages" / "app"
        self.write("packages/AGENTS.md", "packages rule\n")
        self.write("packages/app/AGENTS.md", "app rule\n")
        facts = collect(workspace)
        self.assertEqual(facts.agents_files, ["packages/app/AGENTS.md", "packages/AGENTS.md", "AGENTS.md"])
        text = facts.text
        self.assertLess(text.index("app rule"), text.index("packages rule"))
        self.assertLess(text.index("packages rule"), text.index("root rule"))
        self.assertIn("deepest file takes precedence", text)

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_agents_content_is_bounded(self):
        self.write("AGENTS.md", "rule\n" * 2000)
        facts = collect(self.root)
        self.assertLessEqual(len(facts.text.split("Project instructions", 1)[1].encode("utf-8")), AGENTS_MAX_BYTES + 200)

    @unittest.skipUnless(HAS_GIT, "git is not installed")
    def test_a_failing_git_call_does_not_break_the_snapshot(self):
        self.run_git("init", "-q")
        self.write(".git/HEAD", "not a ref\n")
        facts = collect(self.root)
        self.assertIn("Working directory", facts.text)

    def run_git(self, *arguments: str) -> None:
        subprocess.run(["git", "-C", os.fspath(self.root), *arguments], check=True,
                       capture_output=True, text=True)
