"""The Markdown renderer, exercised as the browser runs it (WK1).

WHY THIS IS A NODE TEST
    ``markdown.js`` is the only place model output becomes markup. A Python
    re-implementation of the same rules would prove nothing about the file the
    page actually loads, so this runs the real file under ``node`` and inspects
    the string it returns. Node is present on this machine and in CI here; where
    it is not, the module skips rather than pretending to pass.

WHAT IT PINS
    1. The escaping promise: no input produces a tag the file did not write, so
       a ``<script>`` in an answer is text. This is the security property.
    2. Links are http(s) only — ``javascript:`` and ``data:`` stay literal.
    3. The subset that answers actually use renders: headings, lists, quotes,
       tables, fenced code, inline code, links.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "chagent" / "web" / "static"
RENDERER = STATIC / "markdown.js"
NODE = shutil.which("node")


def render(cases: list[str]) -> list[str]:
    """Render in one node process; each case is a separate call."""
    script = (
        "const {renderMarkdown} = require(" + json.dumps(str(RENDERER)) + ");"
        "const input = JSON.parse(process.argv[1]);"
        "process.stdout.write(JSON.stringify(input.map(text => renderMarkdown(text))));"
    )
    result = subprocess.run(
        [NODE, "-e", script, json.dumps(cases)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(result.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class MarkdownTests(unittest.TestCase):
    def test_no_input_produces_a_tag_the_renderer_did_not_write(self):
        payloads = [
            "<script>alert(1)</script>",
            "![x](x)\n\n<img src=x onerror=alert(1)>",
            "<div onclick=\"alert(1)\">hi</div>",
            "text with < and > and & and \"quotes\"",
            "```\n<script>alert(1)</script>\n```",
            "| <b>x</b> | y |\n| --- | --- |\n| <i>1</i> | 2 |",
        ]
        for output in render(payloads):
            with self.subTest(output=output):
                self.assertNotIn("<script", output)
                self.assertNotIn("<img", output)
                self.assertNotIn("<div onclick", output)
                self.assertNotIn("<b>", output)
                self.assertNotIn("<i>", output)
                # Every tag that survived is one of ours.
                for tag in _tags(output):
                    self.assertIn(tag, _ALLOWED_TAGS)
        # And the payload is still there, as text: escaping, not stripping.
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", render(payloads)[1])

    def test_links_are_limited_to_http_and_https(self):
        output = render([
            "[ok](https://example.com/a?x=1&y=2)",
            "[js](javascript:alert(1))",
            "[data](data:text/html;base64,PHNjcmlwdD4=)",
            "[ftp](ftp://example.com/x)",
        ])
        self.assertIn('href="https://example.com/a?x=1&amp;y=2"', output[0])
        self.assertNotIn("<a", output[1])
        self.assertIn("javascript:alert(1)", output[1])
        self.assertNotIn("<a", output[2])
        self.assertNotIn("<a", output[3])
        # A link that survives cannot navigate the page it was opened from.
        self.assertIn('rel="noopener noreferrer"', output[0])
        self.assertIn('target="_blank"', output[0])

    def test_headings_lists_quotes_and_tables(self):
        output = render([
            "# 一级\n\n## 二级",
            "- 一\n- 二\n  - 二点一\n- 三",
            "1. 甲\n2. 乙",
            "> 引用一行\n> 引用两行",
            "| 名称 | 值 |\n| --- | --- |\n| a | 1 |\n| b | 2 |",
        ])
        self.assertEqual(output[0], "<h1>一级</h1><h2>二级</h2>")
        self.assertIn("<ul><li>一</li><li>二<ul><li>二点一</li></ul></li><li>三</li></ul>", output[1])
        self.assertIn("<ol><li>甲</li><li>乙</li></ol>", output[2])
        self.assertIn("<blockquote><p>引用一行", output[3])
        self.assertIn("<th>名称</th>", output[4])
        self.assertIn("<td>2</td>", output[4])
        # A table never widens the transcript: it scrolls inside its own box.
        self.assertIn('class="table-wrap"', output[4])

    def test_code_blocks_keep_their_text_and_gain_a_copy_control(self):
        output = render(["```python\nx = 1 < 2\n```"])[0]
        self.assertIn('<span class="code-lang">python</span>', output)
        self.assertIn("data-copy-code", output)
        self.assertIn("x = 1 &lt; 2", output)
        # Fenced code is not inline-formatted: markdown inside it stays literal.
        self.assertIn("**not bold**", render(["```\n**not bold**\n```"])[0])

    def test_inline_code_cannot_be_turned_into_html(self):
        output = render(["`<b>raw</b>` 以及 **粗** 和 *斜*"])[0]
        self.assertIn("<code>&lt;b&gt;raw&lt;/b&gt;</code>", output)
        self.assertIn("<strong>粗</strong>", output)
        self.assertIn("<em>斜</em>", output)

    def test_plain_prose_is_one_paragraph(self):
        self.assertEqual(render(["一\n二"])[0], "<p>一\n二</p>")
        self.assertEqual(render([""])[0], "")


_ALLOWED_TAGS = frozenset({
    "p", "h1", "h2", "h3", "h4", "ul", "ol", "li", "blockquote", "hr", "strong", "em", "code",
    "a", "table", "thead", "tbody", "tr", "th", "td", "pre", "div", "span", "button",
})


def _tags(html: str) -> set[str]:
    import re
    return {match.lower() for match in re.findall(r"<\s*/?\s*([A-Za-z][A-Za-z0-9]*)", html)}


if __name__ == "__main__":
    unittest.main()
