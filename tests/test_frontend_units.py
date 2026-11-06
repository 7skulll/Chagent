"""Pure front-end functions, run by ``node --test`` with no DOM at all.

WHY THIS IS SEPARATE FROM test_web_render.py
    That test drives the whole page against a DOM stub, which proves wiring but
    cannot reach the reducer: the events it reads come from an EventSource the
    stub never opens. Formatting, status vocabulary and event folding are pure
    functions of their arguments, so they are checked here — no stub, no
    document, no waiting on a paint.

WHY A PYTHON WRAPPER
    One regression command. A JavaScript test that only runs when someone
    remembers to run it is a test that stops running.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
SUITE = Path(__file__).resolve().parent / "js" / "pure.test.mjs"


@unittest.skipUnless(NODE, "node is not installed")
class PureFunctionTests(unittest.TestCase):
    def test_formatting_and_the_run_reducer(self):
        result = subprocess.run(
            [NODE, "--test", str(SUITE)], capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fail 0", result.stdout)


if __name__ == "__main__":
    unittest.main()
