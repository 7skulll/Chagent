"""Behavior tests for page-local drafts. Actual HTTP/version checks live in test_web.

The script itself is ``tests/js/editor-page.mjs``: it stubs the DOM, holds the
save and directory requests open to create races, and prints "checks passed".
This file only spawns it and reads the exit code.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
SCRIPT = Path(__file__).resolve().parent / "js" / "editor-page.mjs"


@unittest.skipUnless(NODE, "Node required")
class EditorTests(unittest.TestCase):
    def test_drafts_save_races_conflicts_and_workspace_isolation(self):
        result = subprocess.run(
            [NODE, str(SCRIPT)], text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checks passed", result.stdout)
