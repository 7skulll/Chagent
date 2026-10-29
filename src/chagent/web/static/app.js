"use strict";

const $ = id => document.getElementById(id);
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const state = {session: null, run: null, source: null, version: 0, before: null, sessionCursor: null, busy: false, loading: false};
let liveText = null;
const toolRows = new Map();

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `请求失败 (${response.status})`);
  return value;
}
const post = (path, value) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
function showError(error) { $("error").textContent = error.message || String(error); $("error").hidden = false; }
function clearError() { $("error").hidden = true; }
function textNode(tag, text, className) {
  const node = document.createElement(tag); node.textContent = text;
  if (className) node.className = className;
  return node;
}
function scrollBottom() { const view = $("conversation"); view.scrollTop = view.scrollHeight; }
function setBusy(busy) {
  state.busy = busy;
  $("send").disabled = busy || state.loading;
  $("stop").hidden = !busy;
  $("status").textContent = busy ? "正在生成…" : "就绪";
}
function displayMessage(message, target = $("messages")) {
  if (message.role === "system") return;
  const node = document.createElement("article");
  node.className = `message ${message.role === "user" ? "user" : "assistant"}`;
  node.append(textNode("div", message.role === "user" ? "你" : message.role === "toolResult" ? "工具结果" : "Chagent", "role"));
  const content = message.role === "toolResult" && message.artifact
    ? [{type: "text", text: message.artifact.preview}] : message.content;
  if (typeof content === "string") node.append(textNode("div", content, "content"));
  else for (const block of content || []) {
    if (block.type === "text") node.append(textNode("div", block.text, "content"));
    if (block.type === "toolCall") {
      const details = document.createElement("details");
      details.append(textNode("summary", `调用工具 · ${block.name}`), textNode("pre", JSON.stringify(block.arguments, null, 2)));
      node.append(details);
    }
  }
  if (message.role === "toolResult") {
    const details = document.createElement("details");
    const artifactLabel = message.artifact ? ` · 产物 ${message.artifact.char_count} 字符` : "";
    details.append(textNode("summary", `${message.tool_name} · ${message.is_error ? "失败" : "完成"}${artifactLabel}`));
    for (const child of Array.from(node.children).slice(1)) details.append(child);
    if (message.artifact) {
      let offset = 0;
      const button = textNode("button", "读取产物全文（分页）", "quiet");
      const output = textNode("pre", "");
      const session = state.session;
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const page = await api(`/api/sessions/${session}/artifacts/${message.artifact.id}?offset=${offset}`);
          output.append(document.createTextNode(page.text));
          offset = page.next_offset;
          button.hidden = offset === null;
          button.textContent = `继续读取 · 从字符 ${offset}`;
        } catch (error) { showError(error); }
        finally { button.disabled = false; }
      });
      details.insertBefore(button, details.children[1] || null);
      if (message.artifact.source_truncated) details.append(textNode("p", "来源输出已被上游截断，产物保留全部已捕获内容。"));
      details.append(output);
    }
    node.append(details);
  }
  if (message.error_message) node.append(textNode("div", message.error_message, "tool-status failed"));
  // Thinking is internal; only response text and tool records are displayed.
  if (node.children.length > 1) target.append(node);
  return node;
}
function resetStream() {
  if (state.source) state.source.close();
  state.source = null; state.run = null; liveText = null; toolRows.clear(); $("live").replaceChildren();
}
async function listSessions(append = false) {
  const page = await api(`/api/sessions${append && state.sessionCursor ? `?before=${state.sessionCursor}` : ""}`);
  if (!append) $("sessions").replaceChildren();
  for (const session of page.items) {
    const button = textNode("button", session.title || "未命名会话");
    button.dataset.id = session.id; button.title = session.title || "未命名会话";
    button.classList.toggle("active", session.id === state.session);
    button.addEventListener("click", () => openSession(session.id).catch(showError));
    $("sessions").append(button);
  }
  state.sessionCursor = page.next_cursor; $("more-sessions").hidden = !page.next_cursor;
}
async function openSession(id) {
  const version = ++state.version;
  resetStream(); clearError(); state.session = id; state.loading = true; setBusy(false);
  $("messages").replaceChildren(); $("empty").hidden = true;
  localStorage.setItem("chagent.session", id);
  for (const button of $("sessions").children) button.classList.toggle("active", button.dataset.id === id);
  try {
    const [session, page] = await Promise.all([api(`/api/sessions/${id}`), api(`/api/sessions/${id}/messages`)]);
    if (version !== state.version) return;
    $("session-title").textContent = session.title || "未命名会话";
    const active = session.latest_run && !terminal.has(session.latest_run.status) ? session.latest_run : null;
    for (const item of page.items) {
      // SSE replays the entire active run, so render only its user input from history.
      if (active && item.run_id === active.id && item.message.role !== "user") continue;
      displayMessage(item.message);
    }
    state.before = page.next_before; $("older").hidden = !state.before;
    $("empty").hidden = page.items.length > 0;
    if (active) connectRun(active.id, version);
    else if (session.latest_run) {
      showRunStatus(session.latest_run);
      watchMemoryJob(session.latest_run.id, version);
    }
    if ($("context-panel").open) loadContext().catch(showError);
    scrollBottom();
  } finally {
    if (version === state.version) { state.loading = false; $("send").disabled = state.busy; }
  }
}
async function watchMemoryJob(runId, version) {
  let row = null;
  const started = Date.now();
  const refresh = async () => {
    if (version !== state.version) return;
    try {
      const {job} = await api(`/api/runs/${runId}/memory-job`);
      if (version !== state.version || !job) return;
      if (!row) { row = textNode("div", "", "tool-status"); $("live").append(row); }
      const labels = {queued: "等待后台整理记忆", running: "正在后台整理记忆", completed: "后台记忆整理完成", failed: "后台记忆整理未完成，回答已保留"};
      row.textContent = labels[job.status]; row.classList.toggle("failed", job.status === "failed");
      if (["queued", "running"].includes(job.status) && Date.now() - started < 120000) setTimeout(refresh, 1000);
    } catch {
      if (version === state.version && row) row.textContent = "后台记忆状态暂时不可用";
    }
  };
  await refresh();
}
function showRunStatus(run) {
  const labels = {completed: "已完成", failed: "运行失败", cancelled: "已停止", interrupted: "运行已中断"};
  const reasons = {tool_requested_stop: "工具已请求正常停止", explicit_memory: "已写入长期记忆",
    turn_limit: "已达到模型轮数上限", run_timeout: "已达到运行时间上限",
    context_budget_exhausted: "上下文预算不足",
    output_limit: "模型输出达到上限", model_aborted: "模型已停止生成"};
  $("status").textContent = reasons[run.end_reason] || labels[run.status] || run.status;
  if (run.error && run.status !== "cancelled") showError(new Error(run.error));
}
function connectRun(runId, version) {
  state.run = runId; setBusy(true); $("empty").hidden = true;
  const source = new EventSource(`/api/runs/${runId}/events`); state.source = source;
  source.addEventListener("update", event => {
    if (version !== state.version) return;
    const nearBottom = $("conversation").scrollHeight - $("conversation").scrollTop - $("conversation").clientHeight < 100;
    const {type, payload} = JSON.parse(event.data);
    if (type === "memory_extraction_started") $("status").textContent = "正在整理记忆候选…";
    if (type === "memory_saved") {
      const active = payload.items.filter(item => item.status === "active").length;
      const pending = payload.items.filter(item => item.status === "candidate").length;
      $("live").append(textNode("div", `记忆已处理 · ${active} 条已生效，${pending} 条待审核`, "tool-status"));
    }
    if (["memory_extraction_failed", "memory_write_failed"].includes(type)) {
      $("live").append(textNode("div", "本次记忆整理未完成，回答已保留。", "tool-status failed"));
    }
    if (type === "compaction_started") $("status").textContent = "正在压缩历史上下文…";
    if (type === "compaction_completed") {
      $("live").append(textNode("div", `历史已压缩 · 摘要第 ${payload.revision} 版 · ${payload.tokens_before} → ${payload.tokens_after} 估算 token`, "tool-status"));
    }
    if (type === "compaction_failed") {
      $("live").append(textNode("div", "压缩未完成，继续使用原有摘要和近期完整轮次。", "tool-status failed"));
    }
    if (type === "model_started") { liveText = null; $("status").textContent = "正在思考…"; }
    if (type === "text_delta") {
      if (!liveText) { const row = displayMessage({role: "assistant", content: ""}, $("live")); liveText = row.querySelector(".content"); }
      liveText.append(document.createTextNode(payload.delta)); $("status").textContent = "正在生成…";
    }
    if (type === "tool_queued" || type === "tool_started") {
      let tool = toolRows.get(payload.tool_call_id);
      if (!tool) {
        const row = textNode("div", "", "tool-status");
        tool = {row, name: payload.name}; toolRows.set(payload.tool_call_id, tool); $("live").append(row);
      }
      tool.row.textContent = `${payload.name} · ${type === "tool_queued" ? "等待执行" : "正在执行"}`;
      if (type === "tool_started") $("status").textContent = `正在执行 ${payload.name}…`;
    }
    if (type === "tool_completed") {
      const tool = toolRows.get(payload.tool_call_id);
      if (tool) { tool.row.textContent = `${tool.name} · ${payload.outcome_unknown ? "结果未知" : payload.is_error ? "失败" : "完成"}`; tool.row.classList.toggle("failed", payload.is_error || payload.outcome_unknown); }
    }
    if (type === "tool_progress") {
      const tool = toolRows.get(payload.tool_call_id);
      if (tool) tool.row.textContent = `${tool.name} · ${payload.text}`;
    }
    if (type === "tool_interrupted" || type === "tool_cancelled") {
      const tool = toolRows.get(payload.tool_call_id);
      if (tool) { tool.row.textContent = `${tool.name} · ${type === "tool_cancelled" ? "未执行，已取消" : "结果未知"}`; tool.row.classList.add("failed"); }
    }
    if (nearBottom) scrollBottom();
  });
  source.addEventListener("end", () => {
    source.close();
    if (version === state.version) openSession(state.session).catch(showError);
  });
  source.onerror = async () => {
    if (version !== state.version) return;
    $("status").textContent = "连接中断，正在重连…";
    // EventSource handles transient reconnects and Last-Event-ID automatically.
    try {
      await api(`/api/runs/${runId}`);
    } catch (error) { source.close(); setBusy(false); showError(error); }
  };
}
$("composer").addEventListener("submit", async event => {
  event.preventDefault();
  const text = $("input").value;
  if (!text.trim() || state.busy || state.loading) return;
  clearError(); state.loading = true; $("send").disabled = true;
  try {
    if (!state.session) {
      const session = await post("/api/sessions", {title: text.trim().slice(0, 30)}); state.session = session.id;
      await listSessions();
    }
    const id = state.session;
    await post(`/api/sessions/${id}/runs`, {text});
    $("input").value = "";
    if (id === state.session) await openSession(id);
  } catch (error) { showError(error); }
  finally { state.loading = false; $("send").disabled = state.busy; }
});
$("input").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("composer").requestSubmit(); }
});
$("new-session").addEventListener("click", async () => {
  try {
    const session = await post("/api/sessions", {title: "新会话"});
    await listSessions(); await openSession(session.id); $("input").focus();
  } catch (error) { showError(error); }
});
$("stop").addEventListener("click", async () => {
  if (!state.run) return;
  try { await post(`/api/runs/${state.run}/cancel`, {}); $("status").textContent = "正在停止…"; }
  catch (error) { showError(error); }
});
$("older").addEventListener("click", async () => {
  const id = state.session, before = state.before, version = state.version;
  if (!id || !before) return;
  $("older").disabled = true;
  try {
    const page = await api(`/api/sessions/${id}/messages?before=${before}`);
    if (version !== state.version) return;
    const fragment = document.createDocumentFragment();
    for (const item of page.items) displayMessage(item.message, fragment);
    $("messages").prepend(fragment); state.before = page.next_before; $("older").hidden = !state.before;
  } catch (error) { showError(error); }
  finally { $("older").disabled = false; }
});
$("more-sessions").addEventListener("click", () => listSessions(true).catch(showError));
async function loadContext() {
  const version = state.version, id = state.session;
  if (!id) { $("context-info").textContent = "选择会话后可查看。"; return; }
  const value = await api(`/api/sessions/${id}/context`);
  if (version !== state.version) return;
  const target = $("context-info"); target.replaceChildren();
  const latest = value.snapshots[0]?.snapshot;
  if (latest) {
    const selected = latest.selections.filter(item => item.selected).length;
    const summarized = latest.selections.filter(item => item.reason === "summarized").length;
    const excluded = latest.selections.length - selected - summarized;
    target.append(textNode("p", `最近一次任务模型请求：${latest.token_estimate} / ${latest.input_budget} 估算 token；保留 ${selected} 条消息，摘要覆盖 ${summarized} 条，另有 ${excluded} 条未纳入。`));
    target.append(textNode("p", `计数方式：${latest.token_counter}。使用摘要版本：${latest.summary_revision ?? "无"}。`));
    const memories = latest.memory_references || [];
    target.append(textNode("p", memories.length ? `长期记忆：${memories.map(item => `${item.key} v${item.revision}`).join("、")}；${latest.memory_token_estimate} 估算 token。` : "本轮未注入长期记忆。"));
    if (latest.memory_omitted) target.append(textNode("p", `另有 ${latest.memory_omitted} 条检索结果未纳入记忆预算。`));
  } else target.append(textNode("p", "尚无模型请求快照。"));
  if (!value.summaries.length) target.append(textNode("p", "尚未生成会话摘要；达到预算阈值后会自动压缩旧历史或运行内已完成的轮次。"));
  for (const summary of value.summaries) {
    const row = document.createElement("details");
    const scope = summary.source_run_id ? ` · 运行内（${summary.replayable ? summary.source_run_status === "running" ? "仅当前运行可用" : "可续接" : "已失效，保留审计"}）` : "";
    row.append(textNode("summary", `摘要第 ${summary.revision} 版 · 覆盖至消息序号 ${summary.covered_through_sequence}${scope}`));
    row.append(textNode("pre", summary.content)); target.append(row);
  }
}
$("context-panel").addEventListener("toggle", () => { if ($("context-panel").open) loadContext().catch(showError); });
async function init() {
  const info = await api("/api/info");
  $("model").textContent = `${info.provider} / ${info.model}`;
  $("workspace").textContent = info.workspace; $("workspace").title = info.workspace;
  await listSessions();
  const previous = localStorage.getItem("chagent.session");
  if (previous) {
    try { await openSession(previous); }
    catch (error) { localStorage.removeItem("chagent.session"); state.session = null; showError(error); }
  }
}
init().catch(showError);

// New UI wiring: rail button opens the context panel inside the tools sidebar.
$("open-context").addEventListener("click", () => {
  const panel = $("context-panel");
  panel.open = true;
  loadContext().catch(showError);
  panel.scrollIntoView({behavior: "smooth", block: "nearest"});
});

// Collapsible sidebars: state persists across reloads via localStorage.
function applyCollapsed(name, collapsed) {
  document.body.classList.toggle(`${name}-collapsed`, collapsed);
  localStorage.setItem(`chagent.${name}.collapsed`, collapsed ? "1" : "0");
}
for (const name of ["sidebar", "tools"]) {
  const collapsed = localStorage.getItem(`chagent.${name}.collapsed`) === "1";
  document.body.classList.toggle(`${name}-collapsed`, collapsed);
}
$("toggle-sidebar").addEventListener("click", () =>
  applyCollapsed("sidebar", !document.body.classList.contains("sidebar-collapsed")));
$("toggle-tools").addEventListener("click", () =>
  applyCollapsed("tools", !document.body.classList.contains("tools-collapsed")));

// Memory management is explicit user input; generated candidates never auto-activate.
const memoryState = {cursor: null, editing: null, version: 0};
function memoryNotice(message, failed = false) {
  $("memory-notice").textContent = message;
  $("memory-notice").classList.toggle("failed", failed);
}
async function memoryRequest(path, method, value) {
  return api(path, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
}
function editMemory(memory = null) {
  memoryState.editing = memory;
  $("memory-editor").hidden = false;
  $("memory-scope").value = memory?.scope || "workspace";
  $("memory-key").value = memory?.key || "";
  $("memory-content").value = memory?.content || "";
  $("memory-pinned").checked = memory?.pinned || false;
  $("memory-scope").disabled = $("memory-key").disabled = !!memory;
  $("memory-content").focus();
}
async function loadMemories(append = false) {
  const version = ++memoryState.version;
  const status = $("memory-filter").value;
  const cursor = append && memoryState.cursor ? `&before=${memoryState.cursor}` : "";
  const page = await api(`/api/memories?status=${status}${cursor}`);
  if (version !== memoryState.version) return;
  if (!append) $("memory-list").replaceChildren();
  if (!page.items.length && !append) $("memory-list").append(textNode("p", "暂无记录。", "muted"));
  for (const memory of page.items) {
    const card = document.createElement("article"); card.className = "memory-card";
    card.append(textNode("strong", memory.key), textNode("span", ` · ${memory.scope === "user" ? "用户" : "工作区"} · v${memory.revision}${memory.pinned ? " · 优先" : ""}`, "muted"));
    card.append(textNode("div", memory.content, "memory-body"));
    const source = document.createElement("details");
    source.append(textNode("summary", "来源与版本"), textNode("pre", JSON.stringify({origin: memory.origin, evidence: memory.evidence,
      session_id: memory.source_session_id, run_id: memory.source_run_id, source_message_ids: memory.source_message_ids}, null, 2)));
    const history = textNode("button", "查看版本记录", "quiet");
    history.addEventListener("click", async () => {
      try {
        const revisions = await api(`/api/memories/${memory.id}/revisions`);
        history.remove();
        for (const revision of revisions) source.append(textNode("pre", `v${revision.revision} · ${revision.action}\n${revision.payload.content}`));
      } catch (error) { memoryNotice(error.message, true); }
    });
    source.append(history); card.append(source);
    if (memory.status === "candidate") {
      const detail = await api(`/api/memories/${memory.id}`);
      if (version !== memoryState.version) return;
      const conflict = detail.conflict;
      if (conflict) card.append(textNode("p", `将替换当前版本 v${conflict.revision}：${conflict.content}`, "memory-conflict"));
      const accept = textNode("button", conflict ? "替换上述记忆并采纳" : "采纳", "memory-accept");
      accept.addEventListener("click", async () => {
        accept.disabled = true;
        try {
          await post(`/api/memories/${memory.id}/accept`, {expected_revision: memory.revision,
            replace_id: conflict?.id || null, replace_revision: conflict?.revision || null});
          memoryNotice("候选已采纳。"); await loadMemories();
        } catch (error) { memoryNotice(error.message, true); accept.disabled = false; }
      });
      const reject = textNode("button", "拒绝", "quiet");
      reject.addEventListener("click", async () => {
        try { await post(`/api/memories/${memory.id}/reject`, {expected_revision: memory.revision}); await loadMemories(); }
        catch (error) { memoryNotice(error.message, true); }
      });
      card.append(accept, reject);
    }
    if (["active", "candidate"].includes(memory.status)) {
      const edit = textNode("button", "编辑", "quiet"); edit.addEventListener("click", () => editMemory(memory));
      const remove = textNode("button", "删除", "quiet");
      remove.addEventListener("click", async () => {
        try {
          await memoryRequest(`/api/memories/${memory.id}`, "DELETE", {expected_revision: memory.revision});
          memoryNotice("已停止使用这条记忆。"); await loadMemories();
        } catch (error) { memoryNotice(error.message, true); }
      });
      card.append(edit, remove);
    }
    $("memory-list").append(card);
  }
  memoryState.cursor = page.next_cursor; $("more-memories").hidden = !page.next_cursor;
}
$("open-memory").addEventListener("click", () => {
  $("memory-dialog").showModal(); memoryNotice(""); loadMemories().catch(error => memoryNotice(error.message, true));
});
$("close-memory").addEventListener("click", () => $("memory-dialog").close());
$("new-memory").addEventListener("click", () => editMemory());
$("cancel-memory-edit").addEventListener("click", () => { $("memory-editor").hidden = true; memoryState.editing = null; });
$("memory-filter").addEventListener("change", () => loadMemories().catch(error => memoryNotice(error.message, true)));
$("more-memories").addEventListener("click", () => loadMemories(true).catch(error => memoryNotice(error.message, true)));
$("memory-editor").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const memory = memoryState.editing;
    const fields = {content: $("memory-content").value, pinned: $("memory-pinned").checked};
    if (memory) await memoryRequest(`/api/memories/${memory.id}`, "PATCH", {...fields, expected_revision: memory.revision});
    else await post("/api/memories", {...fields, scope: $("memory-scope").value, key: $("memory-key").value});
    $("memory-editor").hidden = true; memoryState.editing = null; memoryNotice("已保存。"); await loadMemories();
  } catch (error) { memoryNotice(error.message, true); }
});
