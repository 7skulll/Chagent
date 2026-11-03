"""The web surface may only show controls that do something.

Z1 removed 24 buttons that were drawing, not working: the home rail, a fake tab,
a browser bar and three tool cards. This module is why they stay gone. A button
must either carry an ``id`` that ``app.js`` looks up, or be a submit control
inside a form whose ``submit`` the script handles.

The reverse direction is checked too: every ``$("id")`` in the script must still
exist in the markup, so deleting an element without its handler fails here
rather than at the first click.

This is a text check, not a browser: it cannot prove a handler does the right
thing, only that one is attached.
"""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "chagent" / "web" / "static"
MARKUP = (STATIC / "index.html").read_text(encoding="utf-8")
SCRIPT = (STATIC / "app.js").read_text(encoding="utf-8") + (STATIC / "file-editor.js").read_text(encoding="utf-8")
STYLES = (STATIC / "style.css").read_text(encoding="utf-8")

# Created by app.js at runtime (approval prompt), so it has no markup of its own.
RUNTIME_IDS = {"approval"}

# Classes and ids that belonged to controls Z1 deleted. A leftover class means
# either the element or half its CSS came back.
REMOVED_TOKENS = (
    "rail-top", "rail-bottom", "head-actions", "side-row", "fake-tab", "tab-x",
    "browser-bar", "url-pill", "tool-card", "tool-ico", "tool-extra",
    # Z6: the rail itself. One workspace button is not a navigation column, and
    # #workspace-button in the sidebar foot already does the same job.
    "rail", "rail-icon", "rail-avatar",
)
REMOVED_IDS = ("brand", "open-context")


class _Markup(HTMLParser):
    """Collects buttons with their enclosing form, plus every id and class."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buttons = []
        self.ids = set()
        self.classes = set()
        self._forms = []

    def handle_starttag(self, tag, attrs):
        values = {name: value or "" for name, value in attrs}
        if values.get("id"):
            self.ids.add(values["id"])
        self.classes.update(values.get("class", "").split())
        if tag == "form":
            self._forms.append(values.get("id", ""))
        if tag == "button":
            self.buttons.append((values, self._forms[-1] if self._forms else None))

    def handle_endtag(self, tag):
        if tag == "form" and self._forms:
            self._forms.pop()


class WebSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = _Markup()
        cls.page.feed(MARKUP)
        cls.lookups = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', SCRIPT))

    def label(self, attrs):
        return attrs.get("title") or attrs.get("aria-label") or attrs.get("id") or "未命名按钮"

    def test_every_button_has_a_handler(self):
        orphans = []
        for attrs, form in self.page.buttons:
            if attrs.get("id") and attrs["id"] in self.lookups:
                continue
            if attrs.get("type") == "submit" and form and f'$("{form}").addEventListener("submit"' in SCRIPT:
                continue
            orphans.append(self.label(attrs))
        self.assertEqual(orphans, [], f"这些按钮没有任何行为：{orphans}")

    def test_button_surface_is_pinned(self):
        # 44 buttons minus the 24 Z1 deleted, plus Z2/Z3's session search toggle
        # and usage pill, and the usage dialog's close button; Z6 removed the
        # rail's workspace button; WK1 added the "回到最新" scroll button, which
        # only exists so a reader who scrolled up can rejoin the stream.
        # A visible close control also dismisses the narrow tools drawer.
        self.assertEqual(len(self.page.buttons), 42)

    def test_script_lookups_exist_in_markup(self):
        missing = sorted(self.lookups - self.page.ids - RUNTIME_IDS)
        self.assertEqual(missing, [], f"app.js 仍在查找这些已不存在的元素：{missing}")

    def test_removed_controls_stay_removed(self):
        for token in REMOVED_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, self.page.classes)
                self.assertNotIn(f".{token}", STYLES)
        for ident in REMOVED_IDS:
            with self.subTest(id=ident):
                self.assertNotIn(ident, self.page.ids)

    def test_composer_chips_are_only_real_controls(self):
        chips = [attrs for attrs, _ in self.page.buttons if "tool-chip" in attrs.get("class", "").split()]
        self.assertEqual([attrs.get("id") for attrs in chips], ["open-config"])

    def test_the_shell_is_one_column(self):
        # A single 60px icon strip with one button duplicated a control that is
        # already labelled in the sidebar foot; it also carried an avatar for a
        # user this single-user app does not have.
        self.assertNotIn('class="rail"', MARKUP)
        self.assertNotIn(".rail", STYLES)

    def test_empty_state_offers_only_real_openers(self):
        # Starters are built in app.js, so the markup holds the container only:
        # every opener must stay a task the base tool surface can finish.
        starters = re.search(r"const STARTERS = \[(.*?)\];", SCRIPT, re.S)
        self.assertIsNotNone(starters)
        self.assertNotRegex(starters.group(1), r"测试|命令|联网|搜索网页")

    def test_no_placeholder_controls_are_left(self):
        self.assertNotIn("未开放", MARKUP)


if __name__ == "__main__":
    unittest.main()
