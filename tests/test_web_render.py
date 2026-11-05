"""The page script, executed once under a stub DOM (WK1–WK4).

WHY THIS EXISTS
    ``tests/test_web_ui.py`` reads the markup and the script as text: it proves a
    handler is attached, not that rendering works. The work batches added a
    Markdown renderer, folded answers, regenerate/edit actions, source cards and
    an upload queue — all of it built at runtime, none of it visible to a text
    check. ``tests/js/render-page.mjs`` runs ``app.js`` for real (in Node,
    against a DOM stub) with one scripted session and reports what ended up on
    screen.

WHAT IT IS NOT
    A browser test. There is no layout, no CSS, no pointer. It catches typos,
    wrong arguments and missing wiring; visual judgement stays manual.

WHY THE FIXTURES MOVED OUT OF THIS FILE
    They used to be a JavaScript string inside this module, and
    ``test_file_editor.py`` cut the shared prefix out of it with
    ``HARNESS.split("const SESSION =")[0]``. Any structural edit to the string
    silently changed what the second file received — and it still passed. The
    stub now lives in ``tests/js/dom-stub.mjs`` and each page test owns its
    fixtures; Python only spawns the script and reads its verdict.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
SCRIPT = Path(__file__).resolve().parent / "js" / "render-page.mjs"


@unittest.skipUnless(NODE, "node is not installed")
class PageRenderTests(unittest.TestCase):
    def test_a_session_renders_and_its_actions_reach_the_api(self):
        result = subprocess.run(
            [NODE, str(SCRIPT)], capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        problems = json.loads(result.stdout)
        self.assertEqual(problems, [], "页面渲染或交互失败：" + "；".join(problems))


if __name__ == "__main__":
    unittest.main()
