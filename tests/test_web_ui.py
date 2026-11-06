"""The web surface may only show controls that do something.

Z1 removed 24 buttons that were drawing, not working: a fake tab, a browser bar
and three tool cards. The reference-inspired rail added later contains only
shortcuts to existing, functional actions. A button must either carry an ``id``
that ``app.js`` looks up, or be a submit control inside a form whose ``submit``
the script handles.

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
APP = (STATIC.parent / "app.py").read_text(encoding="utf-8")
STYLES = (STATIC / "style.css").read_text(encoding="utf-8")

#: Every page module, not just the two entry scripts. A handler that moved into
#: views/ must still count as "attached", and an id that moved out of app.js must
#: still be checked against the markup — otherwise splitting a file would quietly
#: weaken the only test that links the markup to the script.
MODULES = {
    path.relative_to(STATIC).as_posix(): path.read_text(encoding="utf-8")
    for path in sorted(STATIC.rglob("*.js"))
}
SCRIPT = "\n".join(MODULES.values())

# Created by app.js at runtime (approval prompt), so it has no markup of its own.
RUNTIME_IDS = {"approval"}

# Relative imports between page modules: `import x from "./core/dom.js"`,
# `import "./app.js"`, `import("./x.js")`.
IMPORT = re.compile(r'(?:from|import)\s*\(?\s*["\'](\.{1,2}/[^"\']+)["\']')


def _module_graph(entry: str) -> set[str]:
    """Every static path reachable from one module through relative imports."""
    seen: set[str] = set()
    queue = [entry]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = STATIC / name
        if path.suffix != ".js" or not path.is_file():
            continue
        for spec in IMPORT.findall(path.read_text(encoding="utf-8")):
            resolved = (path.parent / spec).resolve()
            try:
                queue.append(resolved.relative_to(STATIC.resolve()).as_posix())
            except ValueError:
                continue
    return seen

# Classes and ids that belonged to placeholder controls Z1 deleted. A leftover
# class means either the element or half its CSS came back.
REMOVED_TOKENS = (
    "head-actions", "side-row", "fake-tab", "tab-x", "browser-bar", "url-pill",
    "tool-card", "tool-ico", "tool-extra",
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
        #: Forms whose submit one of the modules handles, in either idiom:
        #: `$("id").addEventListener("submit", ...)` or `on($("id"), "submit", ...)`.
        cls.submit_forms = {
            literal or wrapped
            for literal, wrapped in re.findall(
                r'(?:\$\(["\']([A-Za-z0-9_-]+)["\']\)\.addEventListener\(\s*["\']submit'
                r'|on\(\$\(["\']([A-Za-z0-9_-]+)["\']\)\s*,\s*["\']submit)',
                SCRIPT,
            )
        }

    def label(self, attrs):
        return attrs.get("title") or attrs.get("aria-label") or attrs.get("id") or "未命名按钮"

    def test_every_button_has_a_handler(self):
        orphans = []
        for attrs, form in self.page.buttons:
            if attrs.get("id") and attrs["id"] in self.lookups:
                continue
            if attrs.get("type") == "submit" and form and form in self.submit_forms:
                continue
            # Delegated handler: the group is looked up and the child is chosen by
            # its data attribute, which is how a two-state switch stays one handler.
            if attrs.get("data-mode") and "mode-switch" in self.lookups:
                continue
            orphans.append(self.label(attrs))
        self.assertEqual(orphans, [], f"这些按钮没有任何行为：{orphans}")

    def test_button_surface_is_pinned(self):
        # The site restart and explicit container rebuild controls are included.
        # Every button must also pass the handler check above.
        self.assertEqual(len(self.page.buttons), 62)

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

    def test_reference_rail_contains_only_real_shortcuts(self):
        self.assertIn('class="rail"', MARKUP)
        rail_ids = {"rail-home", "rail-workspace", "rail-files", "rail-memory", "rail-config", "rail-archive"}
        self.assertTrue(rail_ids.issubset(self.page.ids))
        self.assertTrue(rail_ids.issubset(self.lookups))

    def test_empty_state_offers_only_real_openers(self):
        # Starters are built in app.js, so the markup holds the container only:
        # every opener must stay a task the base tool surface can finish.
        starters = re.search(r"const STARTERS = \[(.*?)\];", SCRIPT, re.S)
        self.assertIsNotNone(starters)
        self.assertNotRegex(starters.group(1), r"测试|命令|联网|搜索网页")

    def test_no_placeholder_controls_are_left(self):
        self.assertNotIn("未开放", MARKUP)

    def test_no_window_globals_carry_calls_between_modules(self):
        """Cross-module calls are imports, not page globals.

        `window.ChagentFiles?.open(...)` cannot fail: if the file panel is not
        loaded, renamed, or thrown during evaluation, the button simply stops
        doing anything and nothing anywhere reports it. That was the worst
        failure mode on this page, so the ban is a test, not a convention.
        """
        offenders = {}
        for name, text in MODULES.items():
            found = set(re.findall(r"\bwindow\.Chagent\w*", text))
            if found:
                offenders[name] = sorted(found)
        self.assertEqual(offenders, {}, f"模块仍在通过 window 全局互相调用：{offenders}")

    def test_module_imports_respect_the_layering_rule(self):
        """Only downward imports, never sideways.

        One direction keeps the module graph acyclic and makes "which layer owns
        this decision" answerable from the path. `run/run-render.js` may import
        `render/` because it is the composition point of those cards; nothing
        imports `run/` except the entry point.
        """
        allowed = {
            "core": {"core"},
            "run": {"core", "render"},
            "render": {"core", "render"},
            "view": {"core", "render", "run"},
            # The entry point and the file panel it creates may import any layer:
            # they are the composition root, not a layer of their own.
            "shell": {"core", "render", "run", "view", "shell"},
        }

        def layer(name: str) -> str:
            if "/" not in name:
                # markdown.js is a leaf with no imports at all.
                return "core" if name == "markdown.js" else "shell"
            folder = name.split("/", 1)[0]
            return "view" if folder == "views" else folder

        violations = []
        for name, text in MODULES.items():
            source = layer(name)
            for spec in IMPORT.findall(text):
                resolved = (STATIC / name).parent / spec
                try:
                    target = resolved.resolve().relative_to(STATIC.resolve()).as_posix()
                except ValueError:
                    violations.append(f"{name} → {spec}（逃出 static/）")
                    continue
                if layer(target) not in allowed[source]:
                    violations.append(f"{name} → {target}")
        self.assertEqual(violations, [], f"跨层 import：{sorted(violations)}")

    def test_every_referenced_asset_is_served(self):
        """Every asset the page can load must be in the server allow-list.

        That includes the whole module graph: an `import "./core/dom.js"` the
        allow-list does not serve is a 404, and a failed module import leaves a
        blank page with no error in the DOM — invisible until someone opens
        devtools. Neither the JS tests nor the Python page test load over HTTP,
        so this text check is the only thing standing between the page and that
        failure.
        """
        referenced = set(re.findall(r'(?:src|href)="/static/([^"]+)"', MARKUP))
        served = set(re.findall(r'"([A-Za-z0-9_./-]+\.(?:js|css|html))"\s*:', APP))
        graph = set()
        for name in referenced:
            graph |= _module_graph(name)
        missing = sorted((referenced | graph) - served)
        self.assertEqual(missing, [], f"页面引用但服务端不提供的静态资源：{missing}")
        for name in sorted(referenced):
            with self.subTest(asset=name):
                self.assertTrue((STATIC / name).is_file(), f"页面引用的文件不存在：{name}")


if __name__ == "__main__":
    unittest.main()
