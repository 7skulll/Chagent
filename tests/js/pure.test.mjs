/* Pure functions, run by `node --test` with no DOM stub and no page.
 *
 * Everything here answers "which string / which state for these inputs", which
 * is the part of the page that used to be spread across handlers and could only
 * be checked by driving the whole document. The reducer is included: it is the
 * one place event order is interpreted, and the events it reads are the ones
 * tests/test_web_render.py cannot produce (they need a live EventSource).
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  approvalTitle, attachmentPrompt, configValue, diffSummary, formatRate, formatSize, formatTokens,
  hostOf, isTerminal, locatorLabel, networkSummary, plainText, readableName, runStatusText,
  uploadStatusLabel, workspaceName, workspaceParent,
} from "../../src/chagent/web/static/core/format.js";
import { initialRunState, reduce } from "../../src/chagent/web/static/run/run-reducer.js";

test("token counts shrink only past a thousand", () => {
  assert.equal(formatTokens(0), "0");
  assert.equal(formatTokens(999), "999");
  assert.equal(formatTokens(1000), "1.0k");
  assert.equal(formatTokens(1500), "1.5k");
  assert.equal(formatTokens(999999), "1000.0k");
  assert.equal(formatTokens(1e6), "1.0M");
});

test("a missing cache denominator is not zero percent", () => {
  assert.equal(formatRate(null), "—");
  assert.equal(formatRate(undefined), "—");
  assert.equal(formatRate(0), "0%");
  assert.equal(formatRate(0.456), "46%");
});

test("byte sizes use the unit a person reads", () => {
  assert.equal(formatSize(512), "512 B");
  assert.equal(formatSize(2048), "2.0 KB");
  assert.equal(formatSize(5 * 1024 * 1024), "5.0 MB");
});

test("a path is shown by its last segment with its parent dimmed", () => {
  assert.equal(workspaceName("/Users/me/project"), "project");
  assert.equal(workspaceName("project"), "project");
  assert.equal(workspaceName("/"), "/");
  assert.equal(workspaceParent("/Users/me/project"), "me/");
  assert.equal(workspaceParent("/single"), "");
});

test("a host is parsed only when the URL is one", () => {
  assert.equal(hostOf("https://docs.example.org/a?b=1"), "docs.example.org");
  assert.equal(hostOf("not a url"), "");
});

test("document locators prefer pages over sections", () => {
  assert.equal(locatorLabel([{page: 2}, {page: 3}, {page: 2}]), "第 2、3 页");
  assert.equal(locatorLabel([{section: "A > B"}, {section: "A > B"}, {section: "C"}]), "A > B · C");
  assert.equal(locatorLabel([]), "");
});

test("message text is the text blocks, or the artifact preview", () => {
  assert.equal(plainText({role: "user", content: "hello"}), "hello");
  assert.equal(plainText({role: "assistant", content: [{type: "text", text: "a"}, {type: "text", text: "b"}]}), "a\nb");
  assert.equal(plainText({role: "assistant", content: [{type: "toolCall", name: "x", arguments: {}}]}), "");
  assert.equal(
    plainText({role: "toolResult", artifact: {preview: "excerpt"}, content: [{type: "text", text: "full"}]}),
    "excerpt",
  );
});

test("the attachment prompt lists exactly the readable, finished files", () => {
  assert.equal(attachmentPrompt([]), "");
  assert.equal(attachmentPrompt(["uploads/a.pdf", "uploads/b.md"]), "请阅读 uploads/a.pdf、uploads/b.md");
});

test("a run is described by its end reason before its status", () => {
  assert.equal(runStatusText({status: "failed", end_reason: "turn_limit"}), "已达到模型轮数上限");
  assert.equal(runStatusText({status: "completed", end_reason: null}), "已完成");
  assert.equal(runStatusText({status: "interrupted"}), "运行已中断");
  assert.equal(runStatusText({status: "weird"}), "weird");
  assert.equal(runStatusText(null), "");
  assert.ok(isTerminal("interrupted"));
  assert.ok(!isTerminal("running"));
});

test("a file names itself readable without an extension", () => {
  assert.ok(readableName("README"));
  assert.ok(readableName("报告.pdf"));
  assert.ok(!readableName("archive.bin"));
});

test("an upload row says what is happening to it", () => {
  assert.equal(uploadStatusLabel({status: "queued"}), "等待上传");
  assert.equal(uploadStatusLabel({status: "uploading", progress: 0.42}), "上传中 42%");
  assert.equal(uploadStatusLabel({status: "done", size: 2048}), "已上传 2.0 KB");
  assert.equal(uploadStatusLabel({status: "failed", error: "boom"}), "boom");
  assert.equal(uploadStatusLabel({status: "failed", error: ""}), "上传失败");
});

test("a write approval names the action, not the tool", () => {
  assert.equal(approvalTitle("write_file"), "写入文件");
  assert.equal(approvalTitle("apply_patch"), "应用补丁");
  assert.equal(approvalTitle("exec_command"), "exec_command");
});

test("configuration reads as behaviour, not as booleans", () => {
  const config = {boundary: "container", capability: "full", gate: "approval", controller: "host-controller",
    approval: "untrusted", approval_timeout: 120, approval_grace: 30, tools: 12, run_timeout: 600,
    run_wall_timeout: 3600,
    workspace_writable: true, process_stdin: false, host_commands: true, web_access: "deepseek",
    web_key: "set", compaction: true, max_turns: 32, max_processes: 2, process_timeout: 300};
  assert.equal(configValue("gate", config), "仅宿主原生操作逐次审批");
  assert.equal(configValue("capability", config), "可执行命令");
  assert.equal(configValue("approval", config), "untrusted，等待 120 秒，无人查看 30 秒后放弃");
  assert.equal(configValue("run_wall_timeout", config), "3600 秒");
  assert.equal(configValue("web_access", config), "deepseek，密钥已配置");
  assert.equal(configValue("max_processes", config), "2 个，单个 300 秒");
});

/* ---------- the run reducer ---------- */

const event = (type, payload = {}) => ({type, payload});

test("delta text accumulates into one paragraph per model turn", () => {
  let state = initialRunState("r1");
  state = reduce(state, event("model_started"));
  state = reduce(state, event("text_delta", {delta: "Hel"}));
  state = reduce(state, event("text_delta", {delta: "lo"}));
  assert.deepEqual(state.items.map(item => [item.kind, item.text]), [["text", "Hello"]]);

  state = reduce(state, event("model_started"));
  state = reduce(state, event("text_delta", {delta: "again"}));
  assert.deepEqual(state.items.map(item => item.text), ["Hello", "again"]);
});

test("a tool row is created once and updated in place", () => {
  let state = initialRunState("r1");
  state = reduce(state, event("tool_queued", {tool_call_id: "c1", name: "read_file"}));
  state = reduce(state, event("tool_started", {tool_call_id: "c1", name: "read_file"}));
  state = reduce(state, event("tool_progress", {tool_call_id: "c1", text: "读取中 3/9"}));
  state = reduce(state, event("tool_completed", {tool_call_id: "c1", is_error: false}));
  assert.equal(state.items.length, 1);
  assert.equal(state.items[0].name, "read_file");
  assert.equal(state.items[0].state, "完成");
  assert.equal(state.items[0].failed, false);
});

test("an unknown tool outcome is not a success", () => {
  let state = reduce(initialRunState("r1"), event("tool_completed", {tool_call_id: "c1", is_error: false, outcome_unknown: true}));
  assert.equal(state.items[0].state, "结果未知");
  assert.equal(state.items[0].failed, true);
});

test("items stay in arrival order across kinds", () => {
  let state = initialRunState("r1");
  state = reduce(state, event("model_started"));
  state = reduce(state, event("text_delta", {delta: "a"}));
  state = reduce(state, event("tool_started", {tool_call_id: "c1", name: "exec_command"}));
  state = reduce(state, event("compaction_completed", {revision: 2, tokens_before: 900, tokens_after: 400}));
  state = reduce(state, event("text_delta", {delta: "b"}));
  assert.deepEqual(state.items.map(item => item.kind), ["text", "tool", "notice"]);
  assert.equal(state.items[0].text, "ab");
  assert.match(state.items[2].text, /摘要第 2 版，900 → 400/);
});

test("an approval is added, filled in, then removed", () => {
  let state = reduce(initialRunState("r1"), event("approval_requested", {}));
  assert.equal(state.items[0].state, "loading");
  assert.equal(state.status, "等待确认操作…");

  state = reduce(state, event("approval_loaded", {request: {id: "a1", tool_name: "write_file"}}));
  assert.equal(state.items[0].state, "pending");
  assert.equal(state.items[0].request.id, "a1");

  state = reduce(state, event("approval_decided", {}));
  assert.deepEqual(state.items, []);
  assert.equal(state.approvalId, null);
});

test("an approval that could not be read says so instead of looking refused", () => {
  let state = reduce(initialRunState("r1"), event("approval_requested", {}));
  state = reduce(state, event("approval_loaded", {error: "503"}));
  assert.equal(state.items[0].state, "failed");
  assert.equal(state.items[0].error, "503");
});

test("an unknown event does not repaint", () => {
  const state = initialRunState("r1");
  assert.equal(reduce(state, event("something_new", {})), state);
});

test("the final file notice uses persisted diff counts and paths", () => {
  const state = reduce(initialRunState("r1"), event("workspace_manifest_after", {
    diff: {complete: true, counts: {added: 1, modified: 0, deleted: 0},
      added: [{path: "index.html"}], modified: [], deleted: []},
  }));
  assert.match(state.items[0].text, /新增 1，修改 0，删除 0：index.html/);
});

test("network notice reads observed destinations and bytes", () => {
  const state = reduce(initialRunState("r1"), event("network_observed", {
    complete: true, sent_bytes: 2723, targets: [{ip: "104.20.23.154", port: 443}],
  }));
  assert.match(state.items[0].text, /2723 字节；目标 104\.20\.23\.154:443/);
});

test("the closing record names the changed files", () => {
  const diff = {complete: true, added: [{path: "a.txt"}], modified: [{path: "b.txt"}], deleted: [],
                counts: {added: 1, modified: 1, deleted: 0}};
  assert.equal(diffSummary(diff), "工作区改动：新增 1，修改 1，删除 0：a.txt、b.txt");
  assert.equal(diffSummary({...diff, complete: false}).endsWith("；扫描不完整"), true);
  assert.equal(diffSummary(null), "");
});

test("the closing record names the destinations and bytes", () => {
  assert.equal(networkSummary({complete: true, sent_bytes: 2723,
    targets: [{ip: "104.20.23.154", port: 443}, {ip: "192.168.65.7", port: 53}]}),
    "容器出网：2723 字节；目标 104.20.23.154:443、192.168.65.7:53");
  assert.equal(networkSummary({complete: true, sent_bytes: 0, targets: []}), "容器出网：0 字节；目标 无");
  assert.match(networkSummary({complete: false}), /不完整/);
  assert.equal(networkSummary(null), "");
});

test("the same events always produce the same state", () => {
  const events = [
    event("model_started"),
    event("text_delta", {delta: "x"}),
    event("tool_started", {tool_call_id: "c1", name: "run_tests"}),
    event("tool_completed", {tool_call_id: "c1", is_error: true}),
    event("memory_saved", {items: [{status: "active"}, {status: "candidate"}]}),
  ];
  const once = events.reduce(reduce, initialRunState("r1"));
  const twice = events.reduce(reduce, initialRunState("r1"));
  assert.deepEqual(once, twice);
  assert.equal(once.items[2].text, "记忆已处理，1 条已生效，1 条待审核");
});
