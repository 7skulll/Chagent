"""The page script, executed once under a stub DOM (WK1–WK4).

WHY THIS EXISTS
    ``tests/test_web_ui.py`` reads the markup and the script as text: it proves a
    handler is attached, not that rendering works. The work batches added a
    Markdown renderer, folded answers, regenerate/edit actions, source cards and
    an upload queue — all of it built at runtime, none of it visible to a text
    check. This runs ``app.js`` for real (in Node, against a DOM stub) with one
    scripted session and asserts what ends up on screen.

WHAT IT IS NOT
    A browser test. There is no layout, no CSS, no pointer. It catches typos,
    wrong arguments and missing wiring; visual judgement stays manual.

FUZZY BOUNDARY
    The stub answers exactly the requests the script makes on load. A new request
    without a stub entry returns ``{}`` — the assertion that fails is the one
    about the feature, not a crash in the harness.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "chagent" / "web" / "static"
NODE = shutil.which("node")

HARNESS = r"""
const els = new Map();
function matches(node, selector) {
  return selector.split(",").some(part => {
    const sel = part.trim();
    if (!sel) return false;
    if (sel.startsWith(".")) return Boolean(node._classes && node._classes.has(sel.slice(1)));
    return node.tagName === sel.toUpperCase();
  });
}
function find(node, selector, first) {
  const out = [];
  const walk = current => {
    for (const child of current.children || []) {
      if (matches(child, selector)) { out.push(child); if (first) return true; }
      if (walk(child) && first) return true;
    }
    return false;
  };
  walk(node);
  return first ? (out[0] || null) : out;
}
function makeEl(tag) {
  const el = {
    nodeType: 1, tagName: String(tag || "div").toUpperCase(), children: [], dataset: {}, style: {},
    listeners: {}, _text: "", _classes: new Set(),
    get textContent() { return this._text; },
    set textContent(value) { this._text = String(value); this.children = []; },
    get className() { return [...this._classes].join(" "); },
    set className(value) { this._classes = new Set(String(value).split(/\s+/).filter(Boolean)); },
    set innerHTML(value) { this._html = String(value); },
    get innerHTML() { return this._html || ""; },
    append(...nodes) { for (const node of nodes) if (node) this.children.push(node); },
    prepend(...nodes) { this.children = [...nodes, ...this.children]; },
    replaceChildren(...nodes) { this.children = nodes.filter(Boolean); },
    appendChild(node) { this.children.push(node); return node; },
    insertBefore(node) { this.children.unshift(node); },
    remove() {}, replaceWith() {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    removeEventListener() {},
    fire(type, event = {}) { for (const fn of this.listeners[type] || []) fn({preventDefault() {}, stopPropagation() {}, ...event}); },
    querySelector(selector) { return find(this, selector, true); },
    querySelectorAll(selector) { return find(this, selector, false); },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    focus() {}, select() {}, setSelectionRange() {}, closest() { return null; },
    requestSubmit() {}, showModal() {}, close() {},
    click() { if (this.tagName === "A") globalThis.lastDownload = {href: this.href, name: this.download}; },
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    value: "", hidden: true, disabled: false, open: false, maxLength: 0, title: "", placeholder: "", type: "",
  };
  el.classList = {
    contains: name => el._classes.has(name),
    add: name => el._classes.add(name),
    remove: name => el._classes.delete(name),
    toggle: (name, force) => {
      const on = force === undefined ? !el._classes.has(name) : Boolean(force);
      if (on) el._classes.add(name); else el._classes.delete(name);
      return on;
    },
  };
  return el;
}
globalThis.document = {
  getElementById(id) { if (!els.has(id)) els.set(id, makeEl("div")); els.get(id).id = id; return els.get(id); },
  createElement: tag => makeEl(tag),
  createDocumentFragment: () => makeEl("fragment"),
  createTextNode: text => ({nodeType: 3, textContent: String(text), children: []}),
  querySelector: () => makeEl("div"), querySelectorAll: () => [],
  addEventListener() {}, body: makeEl("body"),
};
globalThis.window = {matchMedia: () => ({matches: false, addEventListener() {}})};
globalThis.matchMedia = globalThis.window.matchMedia;
globalThis.localStorage = {store: new Map(), getItem(k) { return this.store.has(k) ? this.store.get(k) : null; },
  setItem(k, v) { this.store.set(k, String(v)); }, removeItem(k) { this.store.delete(k); }};
globalThis.navigator = {clipboard: {writeText: async () => {}}};
globalThis.requestAnimationFrame = cb => setTimeout(cb, 0);
globalThis.cancelAnimationFrame = id => clearTimeout(id);
globalThis.EventSource = class { addEventListener() {} close() {} };
globalThis.XMLHttpRequest = class {
  upload = {addEventListener() {}}; listeners = {};
  open(method, url) { const m = /name=([^&]+)/.exec(url || ""); this.sentName = m ? decodeURIComponent(m[1]) : "upload"; }
  setRequestHeader() {} abort() {}
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  send() {
    setTimeout(() => {
      this.status = 201; this.responseText = JSON.stringify({path: "uploads/" + this.sentName});
      for (const fn of this.listeners.load || []) fn();
    }, 5);
  }
};
globalThis.URL = {createObjectURL: blob => { globalThis.downloadBlob = blob; return "blob:x"; }, revokeObjectURL() {}};
globalThis.FileReader = class { readAsDataURL() {} addEventListener() {} };

const SESSION = "11111111-1111-1111-1111-111111111111";
const RUN = "22222222-2222-2222-2222-222222222222";
const RUN2 = "33333333-3333-3333-3333-333333333333";
const MESSAGES = [
  {id: "m1", sequence: 1, run_id: RUN, superseded_by: null, message: {role: "user", content: "第一问"}},
  {id: "m2", sequence: 2, run_id: RUN, superseded_by: null, message: {role: "assistant", content: [
    {type: "text", text: "# 标题\n\n" + "长".repeat(2200)},
    {type: "toolCall", name: "web_search", arguments: {query: "x"}}]}},
  {id: "m3", sequence: 3, run_id: RUN, superseded_by: null, message: {role: "toolResult", tool_name: "web_search",
    is_error: false, content: [{type: "text", text: "结果正文"}],
    details: {query: "x", results: [{title: "示例站", url: "https://example.com/a", snippet: "摘要文字",
                                     site_name: "示例站点", date: "2026-03-01", authority: 3, score: 2.4}]}}},
  // WD 批: the fetch tool's details no longer carry a status code, so a card must
  // come from {"url", "title", "format"} alone.
  {id: "m6", sequence: 6, run_id: RUN, superseded_by: null, message: {role: "toolResult", tool_name: "web_fetch",
    artifact: {id: "artifact-1", preview: "摘录", char_count: 9000},
    is_error: false, content: [{type: "text", text: "页面正文"}],
    details: {url: "https://docs.example.org/page", title: "文档页", format: "markdown",
              chars: 900, truncated: false}}},
  {id: "m7", sequence: 7, run_id: RUN2, superseded_by: null, message: {role: "toolResult", tool_name: "create_file", is_error: false, content: [{type:"text", text:"Created"}], details: {generated_file: {id:"file-1", name:"故事.md", size:42}}}},
  {id: "m4", sequence: 4, run_id: RUN, superseded_by: RUN2, message: {role: "assistant", content: [{type: "text", text: "被替换的回答"}]}},
  {id: "m5", sequence: 5, run_id: RUN2, superseded_by: null, message: {role: "assistant", content: [{type: "text", text: "```js\nconst a = 1 < 2;\n```"}]}},
];
const ROUTES = {
  "/api/info": {provider: "demo", model: "fake", workspace: "/tmp", default_mode: "work", code_mode: false},
  ["/api/sessions/" + SESSION]: {id: SESSION, title: "冒烟", archived_at: null, latest_run: {id: RUN2, status: "completed"}},
  ["/api/sessions/" + SESSION + "/messages"]: {items: MESSAGES, next_before: null},
  ["/api/sessions/" + SESSION + "/files"]: {items: [{id:"file-1", name:"故事.md", size:42}]},
  ["/api/sessions/" + SESSION + "/sources"]: {runs: [{run_id: RUN2, memories: [{id: "mem1", key: "project.db",
    scope: "workspace", revision: 1, content: "使用 PostgreSQL", pinned: true, origin: "manual"}]}]},
  ["/api/sessions/" + SESSION + "/usage"]: {totals: {total_tokens: 0}},
  "/api/workspaces": {path: "/tmp"},
  "/api/sessions": {items: [], next_cursor: null, archived_count: 0},
  "/api/runs?active=true": {items: []},
};
const CALLS = [];
globalThis.fetch = async (path, options) => {
  CALLS.push({path, body: options && options.body ? JSON.parse(options.body) : null});
  const key = Object.keys(ROUTES).find(route => path === route || path.startsWith(route + "?"));
  const body = key ? ROUTES[key] : {};
  return {ok: true, status: 200, json: async () => body};
};

require(process.cwd() + "/markdown.js");
globalThis.window.ChagentMarkdown = globalThis.ChagentMarkdown;
localStorage.setItem("chagent.session", SESSION);
require(process.cwd() + "/app.js");

const problems = [];
setTimeout(() => {
  const messages = document.getElementById("messages");
  const text = JSON.stringify(messages, (key, value) => (key === "listeners" ? undefined : value));
  const article = count => `found ${count}`;
  const check = (condition, problem) => { if (!condition) problems.push(problem); };
  const articles = messages.children.filter(node => node.tagName === "ARTICLE");
  check(articles.length === 6, "expected six message articles, " + article(articles.length));
  check(text.includes("<h1>标题</h1>"), "markdown heading was not rendered");
  check(text.includes("clamped") && text.includes("read-more"), "a long answer was not folded");
  check(text.includes("superseded-fold"), "a replaced answer was not folded");
  check(text.includes("code-block") && text.includes("data-copy-code"), "fenced code lost its copy control");
  check(text.includes("依据"), "the sources block is missing");
  check(text.includes("message-file") && text.includes("故事.md"), "generated file card missing from conversation");
  check(text.includes("example.com/a"), "the search source card is missing");
  check(text.includes("示例站点"), "the provider's site label is not used on the card");
  check(text.includes("docs.example.org/page"), "a fetched page is not cited");
  check(text.includes("project.db"), "the injected memory is not cited");
  check(!text.includes("<script"), "model output produced a script tag");
  const artifactList = document.getElementById("artifacts-list");
  check(artifactList.children.length === 1, "sidebar should list only generated files");
  check(document.getElementById("artifacts-count").textContent === "1", "sidebar count is wrong");
  check(JSON.stringify(artifactList).includes("故事.md"), "generated file is missing");
  const actions = () => messages.querySelectorAll(".message-actions").flatMap(row => row.children);
  check(actions().some(node => node.textContent === "重新生成"), "no regenerate action");
  check(actions().some(node => node.textContent === "编辑"), "no edit action");
  const downloads = actions().filter(node => node.textContent === "下载");
  check(downloads.length === 3, "download should only appear on text answers");
  downloads[0].fire("click");
  check(globalThis.lastDownload && globalThis.lastDownload.name === "chagent-m2.md", "wrong download filename");
  globalThis.downloadBlob.text().then(value => check(value === "# 标题\n\n" + "长".repeat(2200), "download lost original answer text"));

  actions().find(node => node.textContent === "重新生成").fire("click");
  setTimeout(() => {
    actions().find(node => node.textContent === "编辑").fire("click");
    const form = messages.querySelector(".message-edit");
    check(Boolean(form), "the edit form did not open");
    if (form) {
      form.children.find(child => child.className === "message-edit-actions").children
        .find(child => child.textContent === "发送").fire("click");
    }
    setTimeout(() => {
      const fileInput = document.getElementById("file-input");
      fileInput.fire("change", {target: {files: [
        {name: "报告.pdf", size: 1024, type: "application/pdf"},
        {name: "超大.bin", size: 30 * 1024 * 1024, type: "application/octet-stream"},
      ], value: ""}});
      const queued = JSON.stringify(document.getElementById("uploads").children);
      check(queued.includes("超过 20 MiB"), "an oversized file was not refused with a reason");
      check(queued.includes("报告.pdf"), "a readable file did not get a row");
      check(queued.includes("upload-bar"), "an uploading file has no progress element");
      setTimeout(() => {
        check(CALLS.some(call => call.body && call.body.regenerate_from), "regenerate did not reach the API");
        check(CALLS.some(call => call.body && call.body.edit_message_id), "edit did not reach the API");
        const composer = document.getElementById("input").value;
        check(composer.startsWith("请阅读 uploads/报告.pdf"), "the composer lost its upload prompt: " + composer);
        check(JSON.stringify(document.getElementById("uploads").children).includes("已上传"),
              "the upload never completed");
        process.stdout.write(JSON.stringify(problems));
      }, 80);
    }, 60);
  }, 60);
}, 250);
"""


@unittest.skipUnless(NODE, "node is not installed")
class PageRenderTests(unittest.TestCase):
    def test_a_session_renders_and_its_actions_reach_the_api(self):
        result = subprocess.run(
            [NODE, "-"], input=HARNESS, capture_output=True, text=True, timeout=120,
            cwd=STATIC.parent / "static",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        problems = json.loads(result.stdout)
        self.assertEqual(problems, [], "页面渲染或交互失败：" + "；".join(problems))


if __name__ == "__main__":
    unittest.main()
