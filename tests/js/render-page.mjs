/* Runs the real app.js once against the DOM stub and prints found problems.
 *
 * The Python test only spawns this file and compares stdout with []; every
 * assertion lives here so the fixtures and the checks that depend on them stay
 * in one language.
 *
 * Fuzzy boundary, unchanged from the string this replaced: the stub answers
 * exactly the requests the script makes on load. A new request without a route
 * returns {} — the assertion that fails is the one about the feature.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { installDom, MEDIA } from "./dom-stub.mjs";

const STATIC = fileURLToPath(new URL("../../src/chagent/web/static/", import.meta.url));

installDom();
globalThis.location = {hash: "", pathname: "/", search: ""};
globalThis.history = {replaceState() {}};

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
  ["/api/sessions/" + SESSION]: {id: SESSION, workspace: "/tmp", title: "冒烟", archived_at: null, latest_run: {id: RUN2, status: "completed"}},
  ["/api/sessions/" + SESSION + "/messages"]: {items: MESSAGES, next_before: null},
  ["/api/sessions/" + SESSION + "/files"]: {items: [{id:"file-1", name:"故事.md", size:42}]},
  ["/api/sessions/" + SESSION + "/sources"]: {runs: [{run_id: RUN2, memories: [{id: "mem1", key: "project.db",
    scope: "workspace", revision: 1, content: "使用 PostgreSQL", pinned: true, origin: "manual"}]}]},
  ["/api/sessions/" + SESSION + "/usage"]: {totals: {total_tokens: 0}},
  "/api/workspaces": {path: "/tmp", items: [{id: "w1", path: "/tmp", available: true, sessions: 1}]},
  "/api/sessions": {items: [], next_cursor: null, archived_count: 0},
  "/api/runs?active=true": {items: []},
  ["/api/runs/" + RUN2]: {id: RUN2, status: "completed", end_reason: "model_stop",
    file_diff: {complete: true, added: [{path: "故事.md"}], modified: [], deleted: [],
                counts: {added: 1, modified: 0, deleted: 0}},
    network_observation: {complete: true, sent_bytes: 2723, packets: 9,
                          targets: [{ip: "104.20.23.154", port: 443}]}},
};
const CALLS = [];
globalThis.fetch = async (path, options) => {
  CALLS.push({path, body: options && options.body ? JSON.parse(options.body) : null});
  const key = Object.keys(ROUTES).find(route => path === route || path.startsWith(route + "?"));
  const body = key ? ROUTES[key] : {};
  return {ok: true, status: 200, json: async () => body};
};

// markdown.js is a classic script (UMD-ish IIFE); this stays an eval so its
// loading matches the browser, where it is a plain <script> before the modules.
eval(readFileSync(STATIC + "markdown.js", "utf8"));
globalThis.window.ChagentMarkdown = globalThis.ChagentMarkdown;
localStorage.setItem("chagent.workspace", "/tmp");
localStorage.setItem("chagent.session", SESSION);
const app = await import(pathToFileURL(STATIC + "app.js").href);
const testOpenSession = app.openSession;
const testSetFollow = app.setFollowForTest;

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
  const summary = messages.querySelector(".run-summary");
  check(Boolean(summary), "the finished run's closing record is missing");
  if (summary) {
    // The stub's textContent only holds a node's own text, so serialize the
    // subtree — the same way the message assertions above do.
    const facts = JSON.stringify(summary, (key, value) => (key === "listeners" ? undefined : value));
    check(facts.includes("故事.md"), "the closing record lost the changed file: " + facts);
    check(facts.includes("104.20.23.154:443"), "the closing record lost the destination: " + facts);
  }
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
      check(document.getElementById("send").disabled, "send was enabled while attachments were uploading");
      const runsBefore = CALLS.filter(call => call.path.endsWith("/runs")).length;
      document.getElementById("input").value = "do not send incomplete attachments";
      document.getElementById("composer").fire("submit");
      check(CALLS.filter(call => call.path.endsWith("/runs")).length === runsBefore,
            "Enter/submit bypassed pending upload guard");
      setTimeout(async () => {
        check(CALLS.some(call => call.body && call.body.regenerate_from), "regenerate did not reach the API");
        check(CALLS.some(call => call.body && call.body.edit_message_id), "edit did not reach the API");
        const composer = document.getElementById("input").value;
        check(composer.startsWith("请阅读 uploads/报告.pdf"), "the composer lost its upload prompt: " + composer);
        check(JSON.stringify(document.getElementById("uploads").children).includes("已上传"),
              "the upload never completed");
        check(!document.getElementById("send").disabled, "send did not recover after upload completed");
        const input = document.getElementById("input");
        input.scrollHeight = 180; input.fire("input");
        check(input.style.height === "180px", "multiline composer did not grow");
        const sidebarMedia = MEDIA.get("(max-width: 960px)");
        const toolsMedia = MEDIA.get("(max-width: 1180px)");
        sidebarMedia.matches = toolsMedia.matches = true;
        sidebarMedia.changed();
        document.getElementById("toggle-sidebar").fire("click");
        check(document.body.classList.contains("drawer-open"), "sidebar did not show a scrim");
        document.getElementById("toggle-tools").fire("click");
        check(document.body.classList.contains("sidebar-collapsed"), "two mobile drawers stayed open together");
        check(!document.body.classList.contains("tools-collapsed"), "tools drawer did not open");
        document.getElementById("scrim").fire("click");
        check(!document.body.classList.contains("drawer-open"), "scrim did not close tools drawer");
        sidebarMedia.matches = toolsMedia.matches = false;
        sidebarMedia.changed();
        check(!document.body.classList.contains("sidebar-collapsed"), "mobile state replaced desktop preference");
        const view = document.getElementById("conversation");
        view.scrollHeight = 5000; view.clientHeight = 500; view.scrollTop = 250;
        testSetFollow(false);
        await testOpenSession(SESSION, {preserveScroll: true});
        check(view.scrollTop === 250, "completion refresh yanked an upward reader to the bottom");
        check(!document.getElementById("jump-latest").hidden, "upward reader lost return-to-latest button");
        testSetFollow(true);
        await testOpenSession(SESSION, {preserveScroll: true});
        check(view.scrollTop === 5000, "completion refresh stopped following the bottom");
        view.scrollTop = 250; testSetFollow(false);
        await testOpenSession(SESSION);
        check(view.scrollTop === 5000, "explicit session open did not show latest messages");
        process.stdout.write(JSON.stringify(problems));
      }, 80);
    }, 60);
  }, 60);
}, 250);
