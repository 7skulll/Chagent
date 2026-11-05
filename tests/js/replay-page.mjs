/* Replay equivalence: the hard evidence for converging into one renderer.
 *
 * WHAT THIS PROVES
 *   1. Painting is a function of state, not of history: feeding the events one
 *      at a time with a paint after each, or reducing them all and painting
 *      once, ends in the same DOM.
 *   2. Painting the same state twice changes nothing (no duplicated rows), so a
 *      re-delivered event cannot draw a second tool row or a second card.
 *   3. The streamed paragraph and the stored paragraph are the same markup:
 *      the live text node's innerHTML equals what the message renderer produces
 *      for the same text. This is the "两份渲染路径" defect, checked directly.
 *   4. An approval card disappears when the decision event arrives.
 *
 * WHAT IT IS NOT
 *   A browser test. The stub has no layout and no CSS; a node is compared by its
 *   serialised shape, not by where it appears on screen.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { installDom } from "./dom-stub.mjs";

const STATIC = fileURLToPath(new URL("../../src/chagent/web/static/", import.meta.url));
installDom();
eval(readFileSync(STATIC + "markdown.js", "utf8"));

const { initialRunState, reduce } = await import(pathToFileURL(STATIC + "run/run-reducer.js").href);
const { createRunRenderer } = await import(pathToFileURL(STATIC + "run/run-render.js").href);
const { contentNode } = await import(pathToFileURL(STATIC + "render/message.js").href);

const event = (type, payload = {}) => ({type, payload});
const EVENTS = [
  event("model_started"),
  event("text_delta", {delta: "# 结果\n\n"}),
  event("text_delta", {delta: "先读文件，"}),
  event("text_delta", {delta: "再跑测试。"}),
  event("tool_queued", {tool_call_id: "c1", name: "read_file"}),
  event("tool_started", {tool_call_id: "c1", name: "read_file"}),
  event("tool_progress", {tool_call_id: "c1", text: "读取中 1/3"}),
  event("tool_completed", {tool_call_id: "c1", is_error: false}),
  event("compaction_completed", {revision: 2, tokens_before: 9000, tokens_after: 4100}),
  event("approval_requested", {}),
  event("approval_loaded", {request: {id: "a1", tool_name: "write_file", subject: "overwrite src/app.py",
                                      cwd: "/w", expires_at: "2026-11-05T10:00:00Z"}}),
  event("approval_decided", {}),
  event("tool_started", {tool_call_id: "c2", name: "run_tests"}),
  event("tool_interrupted", {tool_call_id: "c2"}),
  event("model_started"),
  event("text_delta", {delta: "结束。"}),
  event("memory_saved", {items: [{status: "active"}, {status: "candidate"}]}),
];

const problems = [];
const check = (condition, problem) => { if (!condition) problems.push(problem); };

function serialize(root) {
  return JSON.stringify(root.children, (key, value) => (key === "listeners" || key === "_parent" ? undefined : value));
}

function painted(events, {each} = {}) {
  const root = document.createElement("div");
  const status = document.createElement("span");
  const renderer = createRunRenderer({root, status, decide: () => {}});
  let state = initialRunState("run-1");
  for (const item of events) {
    state = reduce(state, item);
    if (each) renderer.flush(state);
  }
  renderer.flush(state);
  return {root, status, renderer, state, html: serialize(root)};
}

// 1. Same events, same final DOM, whether painted once or after every event.
const once = painted(EVENTS);
const stepwise = painted(EVENTS, {each: true});
check(once.html === stepwise.html, "painting after every event produced different DOM than painting once");

// 2. Determinism across a fresh renderer.
const again = painted(EVENTS);
check(once.html === again.html, "the same events produced different DOM in a second renderer");

// 3. Idempotence: a repeated state must not duplicate anything.
const before = once.root.children.length;
once.renderer.flush(once.state);
check(once.root.children.length === before && serialize(once.root) === once.html,
      "painting the same state twice changed the DOM");

// 4. The streamed paragraph and the stored paragraph are one function.
const texts = once.state.items.filter(item => item.kind === "text");
check(texts.length === 2, `expected two answer paragraphs, got ${texts.length}`);
for (const item of texts) {
  const expected = contentNode(item.text, true).innerHTML;
  const liveContent = once.root.children
    .filter(node => node.tagName === "ARTICLE")
    .map(node => node.children.find(child => child.className === "content md"))
    .find(node => node && node.innerHTML === expected);
  check(Boolean(liveContent),
    `the streamed paragraph for ${JSON.stringify(item.text)} does not match the message renderer's markup`);
}

// 5. The tool rows are one per call, in arrival order, with their own outcome.
const tools = once.state.items.filter(item => item.kind === "tool");
check(tools.length === 2, `expected two tool rows, got ${tools.length}`);
check(tools[0].state === "完成" && !tools[0].failed, "a completed tool was not marked complete");
check(tools[1].state === "结果未知" && tools[1].failed, "an interrupted tool was not marked unknown");

// 6. The approval card is gone once the decision arrives, and the status line
//    no longer says it is waiting.
check(!once.html.includes("需要确认"), "the approval card survived its decision");
check(once.status.textContent !== "等待确认操作…", "the status line still waits for approval");

// 7. No element was left behind by the removed approval card.
check(once.root.children.length === once.state.items.length,
      `live column has ${once.root.children.length} rows for ${once.state.items.length} items`);

process.stdout.write(JSON.stringify(problems));
