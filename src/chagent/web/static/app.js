"use strict";

const $ = id => document.getElementById(id);
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const state = {session: null, run: null, source: null, version: 0, before: null, sessionCursor: null,
  busy: false, loading: false, workspace: null, archived: false, query: "", readonly: false,
  active: new Map()};
let liveText = null;
const toolRows = new Map();
const ARCHIVED_HINT = "已归档，恢复后才能继续对话";
// Under 960px the sidebar is a drawer over the chat, not a column beside it.
const narrow = window.matchMedia("(max-width: 960px)");

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `请求失败 (${response.status})`);
  return value;
}
const post = (path, value) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
const patch = (path, value) => api(path, {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
// Every workspace-scoped call names its workspace; the server refuses ones that
// were never opened, so a stale tab cannot act in a directory it did not pick.
const withWorkspace = path => state.workspace
  ? `${path}${path.includes("?") ? "&" : "?"}workspace=${encodeURIComponent(state.workspace)}`
  : path;
function showError(error) { $("error").textContent = error.message || String(error); $("error").hidden = false; }
function clearError() { $("error").hidden = true; }
function textNode(tag, text, className) {
  const node = document.createElement(tag); node.textContent = text;
  if (className) node.className = className;
  return node;
}
function scrollBottom() { const view = $("conversation"); view.scrollTop = view.scrollHeight; }
// One place decides whether the composer accepts input: a run in flight, a
// turn loading, and an archived session all block it for different reasons.
function updateComposer() {
  const blocked = state.busy || state.loading || state.readonly;
  $("send").disabled = blocked;
  $("attach").disabled = state.readonly;
  $("input").disabled = state.readonly;
  $("input").placeholder = state.readonly ? "该会话已归档，恢复后可继续" : "随心输入";
}
function setBusy(busy) {
  state.busy = busy;
  updateComposer();
  $("stop").hidden = !busy;
  if (busy) $("status").textContent = "正在生成…";
  else $("status").textContent = state.readonly ? ARCHIVED_HINT : "就绪";
}
// The empty state's openers. Each is a task the base tool surface can finish
// (read, search, explain), so nothing here promises a network call or a host
// command the current configuration may refuse. They fill the composer rather
// than sending: the first message should be yours to edit.
const STARTERS = [
  "读一遍 README，用三句话说明这个项目做什么",
  "在 src 下找出所有 TODO 和 FIXME，整理成一份清单",
  "解释 src/chagent 的目录结构，指出最该先看的三个文件",
];
function renderStarters() {
  const target = $("starters");
  target.replaceChildren(textNode("p", "试试", "starters-label"));
  for (const prompt of STARTERS) {
    const row = textNode("button", prompt, "starter");
    row.type = "button";
    row.addEventListener("click", () => {
      if (state.readonly) return;
      $("input").value = prompt;
      $("input").focus();
      $("input").setSelectionRange(prompt.length, prompt.length);
    });
    target.append(row);
  }
}
renderStarters();

function displayMessage(message, target = $("messages")) {
  if (message.role === "system") return;
  const node = document.createElement("article");
  node.className = `message ${message.role === "user" ? "user" : "assistant"}`;
  // The name stays for screen readers; on screen the bubble and the alignment
  // already say who is speaking, and repeating it above every message is noise.
  node.append(textNode("div", message.role === "user" ? "你" : message.role === "toolResult" ? "工具结果" : "Chagent", "role sr-only"));
  const content = message.role === "toolResult" && message.artifact
    ? [{type: "text", text: message.artifact.preview}] : message.content;
  if (typeof content === "string") node.append(textNode("div", content, "content"));
  else for (const block of content || []) {
    if (block.type === "text") node.append(textNode("div", block.text, "content"));
    if (block.type === "toolCall") {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.append(textNode("code", block.name, "tool-name"), textNode("span", "调用", "tool-state"));
      details.append(summary, textNode("pre", JSON.stringify(block.arguments, null, 2)));
      node.append(details);
    }
  }
  if (message.role === "toolResult") {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.append(textNode("code", message.tool_name, "tool-name"),
      textNode("span", message.is_error ? "失败" : "完成", message.is_error ? "tool-state failed" : "tool-state"));
    if (message.artifact) summary.append(textNode("span", `产物 ${message.artifact.char_count} 字符`, "tool-note"));
    details.append(summary);
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
          button.textContent = `继续读取（从字符 ${offset}）`;
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
  const params = new URLSearchParams();
  if (state.workspace) params.set("workspace", state.workspace);
  if (append && state.sessionCursor) params.set("before", state.sessionCursor);
  if (state.archived) params.set("archived", "only");
  if (state.query) params.set("q", state.query);
  const query = params.toString();
  const page = await api(`/api/sessions${query ? `?${query}` : ""}`);
  if (!append) $("sessions").replaceChildren();
  for (const session of page.items) $("sessions").append(sessionRow(session));
  state.sessionCursor = page.next_cursor; $("more-sessions").hidden = !page.next_cursor;
  $("sessions-label").textContent = state.archived ? "已归档" : "最近";
  if (page.archived_count !== null && page.archived_count !== undefined) {
    $("archived-count").textContent = page.archived_count;
    $("archived-toggle").hidden = page.archived_count === 0 && !state.archived;
  }
  paintActivity();
}
function sessionRow(session) {
  const row = document.createElement("div");
  row.className = "session-row"; row.dataset.id = session.id; row.dataset.archived = session.archived_at ? "1" : "";
  row.classList.toggle("active", session.id === state.session);
  const open = document.createElement("button");
  open.type = "button"; open.className = "session-open";
  open.title = session.title || "未命名会话";
  // The dot is the only signal that a run is still going in a session the user
  // is not looking at; it is painted by the active-run poll, not by this row.
  const dot = document.createElement("span"); dot.className = "dot"; dot.hidden = true;
  const name = document.createElement("span"); name.className = "session-name";
  name.textContent = session.title || "未命名会话";
  open.append(dot, name);
  open.addEventListener("click", () => openSession(session.id).catch(showError));
  const menu = document.createElement("button");
  menu.type = "button"; menu.className = "session-menu"; menu.textContent = "⋯";
  menu.title = "会话操作"; menu.setAttribute("aria-label", "会话操作");
  menu.addEventListener("click", event => { event.stopPropagation(); toggleSessionMenu(row, session); });
  row.append(open, menu);
  return row;
}
function closeSessionMenus() {
  for (const node of document.querySelectorAll(".session-menu-pop")) node.remove();
  for (const node of document.querySelectorAll(".session-row.menu-open")) node.classList.remove("menu-open");
}
function toggleSessionMenu(row, session) {
  const opened = row.classList.contains("menu-open");
  closeSessionMenus();
  if (opened) return;
  row.classList.add("menu-open");
  const pop = document.createElement("div"); pop.className = "session-menu-pop";
  const rename = textNode("button", "重命名");
  rename.addEventListener("click", () => { closeSessionMenus(); startRename(row, session); });
  const toggle = textNode("button", session.archived_at ? "恢复" : "归档");
  toggle.addEventListener("click", () => { closeSessionMenus(); setArchived(session, !session.archived_at); });
  pop.append(rename, toggle);
  row.append(pop);
}
function startRename(row, session) {
  const input = document.createElement("input");
  input.className = "session-rename"; input.maxLength = 200; input.value = session.title || "";
  input.setAttribute("aria-label", "会话标题");
  row.querySelector(".session-open").replaceWith(input);
  input.focus(); input.select();
  let done = false;
  const finish = async save => {
    if (done) return; done = true;
    const title = input.value.trim();
    if (save && title !== (session.title || "")) {
      try {
        await patch(`/api/sessions/${session.id}`, {title});
        if (state.session === session.id) $("session-title").textContent = title || "未命名会话";
      } catch (error) { showError(error); }
    }
    await listSessions();
  };
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); finish(true); }
    if (event.key === "Escape") { event.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}
async function setArchived(session, archived) {
  try {
    await patch(`/api/sessions/${session.id}`, {archived});
    await listSessions();
    // Archiving what is on screen turns the composer read-only instead of
    // yanking the transcript away.
    if (state.session === session.id) await openSession(session.id);
  } catch (error) { showError(error); }
}
document.addEventListener("click", event => {
  if (!event.target.closest(".session-menu-pop") && !event.target.closest(".session-menu")) closeSessionMenus();
});
async function openSession(id) {
  const version = ++state.version;
  resetStream(); clearError(); state.session = id; state.readonly = false; state.loading = true; setBusy(false);
  $("messages").replaceChildren(); $("empty").hidden = true;
  localStorage.setItem("chagent.session", id);
  for (const row of $("sessions").children) row.classList.toggle("active", row.dataset.id === id);
  try {
    const [session, page] = await Promise.all([api(`/api/sessions/${id}`), api(`/api/sessions/${id}/messages`)]);
    if (version !== state.version) return;
    $("session-title").textContent = session.title || "未命名会话";
    // An archived session is still readable; only new input is refused. The
    // server enforces the same rule, this is the part the user can see.
    state.readonly = Boolean(session.archived_at);
    updateComposer();
    if (narrow.matches) applyCollapsed("sidebar", true);
    if (state.readonly) $("status").textContent = ARCHIVED_HINT;
    const active = !state.readonly && session.latest_run && !terminal.has(session.latest_run.status) ? session.latest_run : null;
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
    loadUsagePill();
    if ($("context-panel").open) loadContext().catch(showError);
    scrollBottom();
  } finally {
    if (version === state.version) { state.loading = false; updateComposer(); }
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
    turn_limit: "已达到模型轮数上限", repeated_tool_call: "重复调用同一工具且没有进展，已停止",
    run_timeout: "已达到运行时间上限",
    context_budget_exhausted: "上下文预算不足",
    output_limit: "模型输出达到上限", model_aborted: "模型已停止生成",
    unwatched: "无人查看审批，命令未执行"};
  $("status").textContent = state.readonly ? ARCHIVED_HINT : (reasons[run.end_reason] || labels[run.status] || run.status);
  if (run.error && run.status !== "cancelled") showError(new Error(run.error));
}

// ---------- 用量（只报 token，不报钱） ----------
function formatTokens(value) {
  if (value < 1000) return String(value);
  if (value < 1e6) return `${(value / 1000).toFixed(1)}k`;
  return `${(value / 1e6).toFixed(1)}M`;
}
// A missing denominator is not a 0% hit rate; it is "no fresh input to serve".
function formatRate(rate) { return rate === null || rate === undefined ? "—" : `${Math.round(rate * 100)}%`; }
async function loadUsagePill() {
  const pill = $("usage-pill");
  const id = state.session;
  if (!id) { pill.hidden = true; return; }
  try {
    const usage = await api(`/api/sessions/${id}/usage`);
    if (id !== state.session) return;
    if (!usage.totals.total_tokens) { pill.hidden = true; return; }
    pill.textContent = `${formatTokens(usage.totals.total_tokens)} tokens，缓存 ${formatRate(usage.totals.cache_hit_rate)}`;
    pill.title = `输入 ${formatTokens(usage.totals.input)}，输出 ${formatTokens(usage.totals.output)}，`
      + `缓存读 ${formatTokens(usage.totals.cache_read)}，缓存写 ${formatTokens(usage.totals.cache_write)}`;
    pill.hidden = false;
  } catch { pill.hidden = true; }
}
function usageTable(rows, columns) {
  const table = document.createElement("table"); table.className = "usage-table";
  const head = document.createElement("tr");
  for (const [, label] of columns) head.append(textNode("th", label));
  table.append(head);
  for (const row of rows) {
    const line = document.createElement("tr");
    for (const [key] of columns) line.append(document.createElement("td"));
    columns.forEach(([key], index) => {
      const value = row[key];
      line.children[index].textContent = key === "started_at" || key === "day" ? value
        : key === "model_calls" ? String(value) : formatTokens(value);
    });
    table.append(line);
  }
  return table;
}
async function loadUsageDialog() {
  const notice = $("usage-notice");
  notice.textContent = ""; notice.classList.remove("failed");
  $("usage-summary").replaceChildren(); $("usage-table").replaceChildren();
  const id = state.session;
  if (!id) { notice.textContent = "先选择会话。"; return; }
  const usage = await api(`/api/sessions/${id}/usage`);
  const totals = usage.totals;
  const summary = $("usage-summary");
  const facts = [["运行", `${totals.runs} 次`], ["模型调用", `${totals.model_calls} 次`],
    ["总 token", formatTokens(totals.total_tokens)],
    ["输入 / 输出", `${formatTokens(totals.input)} / ${formatTokens(totals.output)}`],
    ["缓存读 / 写", `${formatTokens(totals.cache_read)} / ${formatTokens(totals.cache_write)}`],
    ["缓存命中率", formatRate(totals.cache_hit_rate)]];
  for (const [label, value] of facts) summary.append(textNode("dt", label), textNode("dd", value));
  if (!usage.items.length) { $("usage-table").append(textNode("p", "还没有模型调用。", "muted")); return; }
  const rows = usage.items.map(item => ({...item,
    started_at: item.started_at ? new Date(item.started_at).toLocaleString() : "—"}));
  $("usage-table").append(usageTable(rows, [["started_at", "开始"], ["model_calls", "调用"],
    ["input", "输入"], ["output", "输出"], ["cache_read", "缓存读"], ["total_tokens", "合计"]]));
}
$("usage-pill").addEventListener("click", () => {
  $("usage-dialog").showModal();
  loadUsageDialog().catch(error => {
    $("usage-notice").textContent = error.message; $("usage-notice").classList.add("failed");
  });
});
$("close-usage").addEventListener("click", () => $("usage-dialog").close());

// ---------- 后台运行可见 ----------
let activeTimer = null;
function paintActivity() {
  for (const row of $("sessions").children) {
    const dot = row.querySelector(".dot");
    if (dot) dot.hidden = !state.active.has(row.dataset.id);
  }
}
async function refreshActive() {
  if (activeTimer) { clearTimeout(activeTimer); activeTimer = null; }
  try {
    const page = await api("/api/runs?active=true");
    state.active = new Map(page.items.map(item => [item.session_id, item]));
  } catch { /* the dot is a hint; a failed poll must not break the page */ }
  paintActivity();
  // Poll only while something is running, so an idle page makes no requests.
  if (state.active.size || state.run) activeTimer = setTimeout(refreshActive, 1000);
}
function noteActive() { if (!activeTimer) refreshActive(); }

function connectRun(runId, version) {
  state.run = runId; setBusy(true); $("empty").hidden = true;
  noteActive();
  const source = new EventSource(`/api/runs/${runId}/events`); state.source = source;
  source.addEventListener("update", event => {
    if (version !== state.version) return;
    const nearBottom = $("conversation").scrollHeight - $("conversation").scrollTop - $("conversation").clientHeight < 100;
    const {type, payload} = JSON.parse(event.data);
    if (type === "memory_extraction_started") $("status").textContent = "正在整理记忆候选…";
    if (type === "memory_saved") {
      const active = payload.items.filter(item => item.status === "active").length;
      const pending = payload.items.filter(item => item.status === "candidate").length;
      $("live").append(textNode("div", `记忆已处理，${active} 条已生效，${pending} 条待审核`, "tool-status"));
    }
    if (["memory_extraction_failed", "memory_write_failed"].includes(type)) {
      $("live").append(textNode("div", "本次记忆整理未完成，回答已保留。", "tool-status failed"));
    }
    if (type === "compaction_started") $("status").textContent = "正在压缩历史上下文…";
    if (type === "compaction_completed") {
      $("live").append(textNode("div", `历史已压缩，摘要第 ${payload.revision} 版，${payload.tokens_before} → ${payload.tokens_after} 估算 token`, "tool-status"));
    }
    if (type === "approval_requested") { showApproval(runId).catch(showError); $("status").textContent = "等待确认命令…"; }
    if (["approval_decided", "approval_expired", "approval_cancelled"].includes(type)) hideApproval();
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
        const state = textNode("span", "", "tool-state");
        row.append(textNode("code", payload.name, "tool-name"), state);
        tool = {row, name: payload.name, state}; toolRows.set(payload.tool_call_id, tool); $("live").append(row);
      }
      tool.state.textContent = type === "tool_queued" ? "等待执行" : "正在执行";
      if (type === "tool_started") $("status").textContent = `正在执行 ${payload.name}…`;
    }
    if (type === "tool_completed") {
      const tool = toolRows.get(payload.tool_call_id);
      if (tool) {
        const state = payload.outcome_unknown ? "结果未知" : payload.is_error ? "失败" : "完成";
        tool.state.textContent = state;
        tool.row.classList.toggle("failed", payload.is_error || payload.outcome_unknown);
      }
    }
    if (type === "tool_progress") {
      const tool = toolRows.get(payload.tool_call_id);
      if (tool) tool.state.textContent = payload.text;
    }
    if (type === "tool_interrupted" || type === "tool_cancelled") {
      const tool = toolRows.get(payload.tool_call_id);
      if (tool) {
        tool.state.textContent = type === "tool_cancelled" ? "未执行，已取消" : "结果未知";
        tool.row.classList.add("failed");
      }
    }
    if (nearBottom) scrollBottom();
  });
  source.addEventListener("end", () => {
    source.close();
    if (version === state.version) openSession(state.session).catch(showError);
    refreshActive();
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
function hideApproval() {
  const row = $("approval");
  if (row) row.remove();
}

async function showApproval(runId) {
  const data = await api(`/api/runs/${runId}/approvals`);
  if (!data.pending.length || state.run !== runId) return;
  hideApproval();
  const request = data.pending[0];
  const row = textNode("div", "", "approval");
  row.id = "approval";
  row.append(textNode("div", `需要确认：${request.tool_name}`, "approval-title"));
  row.append(textNode("code", request.subject, "approval-command"));
  row.append(textNode("div", `目录 ${request.cwd}，${new Date(request.expires_at).toLocaleTimeString()} 前有效，批准只对这一次调用生效`, "approval-note"));
  const actions = textNode("div", "", "approval-actions");
  const approve = textNode("button", "批准这一次", "primary");
  const deny = textNode("button", "拒绝", "");
  const decide = async value => {
    approve.disabled = deny.disabled = true;
    try { await post(`/api/approvals/${request.id}/decide`, {approve: value}); }
    catch (error) { showError(error); }
    finally { hideApproval(); }
  };
  approve.addEventListener("click", () => decide(true));
  deny.addEventListener("click", () => decide(false));
  actions.append(approve, deny);
  row.append(actions);
  $("live").append(row);
  scrollBottom();
}

$("composer").addEventListener("submit", async event => {
  event.preventDefault();
  const text = $("input").value;
  if (!text.trim() || state.busy || state.loading || state.readonly) return;
  clearError(); state.loading = true; updateComposer();
  try {
    if (!state.session) {
      const session = await post("/api/sessions", {title: text.trim().slice(0, 30), workspace: state.workspace}); state.session = session.id;
      await listSessions();
    }
    const id = state.session;
    await post(`/api/sessions/${id}/runs`, {text});
    $("input").value = "";
    if (id === state.session) await openSession(id);
  } catch (error) { showError(error); }
  finally { state.loading = false; updateComposer(); }
});
$("input").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("composer").requestSubmit(); }
});

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// The file body is posted as-is: the endpoint takes raw bytes with any content
// type, so a 20 MiB report never becomes a base64 string inside JSON.
async function uploadFile(file) {
  const response = await fetch(withWorkspace(`/api/uploads?name=${encodeURIComponent(file.name)}`), {
    method: "POST",
    headers: {"Content-Type": file.type || "application/octet-stream"},
    body: file,
  });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error || `上传失败 (${response.status})`);
  return value;
}

$("attach").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", async event => {
  const file = event.target.files && event.target.files[0];
  event.target.value = "";
  if (!file) return;
  clearError();
  $("status").textContent = `正在上传 ${file.name}…`;
  try {
    const saved = await uploadFile(file);
    const input = $("input");
    const ask = `请阅读 ${saved.path}`;
    const rest = input.value.trim();
    input.value = rest ? `${ask}：${rest}` : `${ask}：`;
    input.focus();
    $("status").textContent = `已上传 ${saved.name}（${formatSize(saved.size)}）`;
  } catch (error) {
    showError(error);
    $("status").textContent = "就绪";
  }
});
$("new-session").addEventListener("click", async () => {
  try {
    // A new session is never archived: leave that view so it is visible.
    if (state.archived) showArchived(false);
    const session = await post("/api/sessions", {title: "新会话", workspace: state.workspace});
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

// Search and the archived view are two filters on the same request; each one
// re-reads the first page rather than patching the list in place.
let searchTimer = null;
$("session-search").addEventListener("input", event => {
  const value = event.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = value; state.sessionCursor = null;
    listSessions().catch(showError);
  }, 250);
});
function showArchived(archived) {
  state.archived = archived; state.sessionCursor = null;
  $("archived-toggle").setAttribute("aria-pressed", String(archived));
  $("archived-toggle").hidden = false;
}
$("archived-toggle").addEventListener("click", () => {
  showArchived(!state.archived);
  listSessions().catch(showError);
});
async function loadContext() {
  const version = state.version, id = state.session;
  const target = $("context-info");
  if (!id) {
    target.replaceChildren(textNode("p", "选中一个会话后，这里显示它最近一次的上下文用量与摘要。", "context-note"));
    return;
  }
  const value = await api(`/api/sessions/${id}/context`);
  if (version !== state.version) return;
  target.replaceChildren();
  const latest = value.snapshots[0]?.snapshot;
  if (latest) {
    const selected = latest.selections.filter(item => item.selected).length;
    const summarized = latest.selections.filter(item => item.reason === "summarized").length;
    const excluded = latest.selections.length - selected - summarized;
    const memories = latest.memory_references || [];
    // Figures in pairs, not a paragraph: this pane is scanned for one number.
    const facts = [
      ["估算 token", `${latest.token_estimate} / ${latest.input_budget}`],
      ["保留消息", `${selected} 条`],
      ["摘要覆盖", `${summarized} 条`],
      ["未纳入", `${excluded} 条`],
      ["计数方式", latest.token_counter],
      ["摘要版本", latest.summary_revision ?? "无"],
      ["长期记忆", memories.length
        ? `${memories.length} 条，${latest.memory_token_estimate} 估算 token` : "本轮未注入"],
    ];
    const factsList = document.createElement("dl"); factsList.className = "context-facts";
    for (const [label, value] of facts) factsList.append(textNode("dt", label), textNode("dd", value));
    target.append(factsList);
    if (memories.length) target.append(textNode("p", memories.map(item => `${item.key} v${item.revision}`).join("、"), "context-note"));
    if (latest.memory_omitted) target.append(textNode("p", `另有 ${latest.memory_omitted} 条检索结果未纳入记忆预算。`, "context-note"));
  } else target.append(textNode("p", "尚无模型请求快照。", "context-note"));
  if (!value.summaries.length) {
    target.append(textNode("p", "尚未生成会话摘要。达到预算阈值后会自动压缩旧历史或运行内已完成的轮次。", "context-note"));
  }
  for (const summary of value.summaries) {
    const row = document.createElement("details");
    const scope = summary.source_run_id
      ? `（运行内，${summary.replayable ? summary.source_run_status === "running" ? "仅当前运行可用" : "可续接" : "已失效，保留审计"}）` : "";
    row.append(textNode("summary", `摘要第 ${summary.revision} 版，覆盖至消息序号 ${summary.covered_through_sequence}${scope}`));
    row.append(textNode("pre", summary.content)); target.append(row);
  }
}
$("context-panel").addEventListener("toggle", () => { if ($("context-panel").open) loadContext().catch(showError); });
// Opening a directory is a server-side decision: it joins the set of roots this
// process may touch. The choice is remembered per browser, not per session.
async function ensureWorkspace(path) {
  const opened = await post("/api/workspaces", {path});
  state.workspace = opened.path;
  localStorage.setItem("chagent.workspace", opened.path);
  $("workspace").textContent = workspaceName(opened.path);
  $("workspace-parent").textContent = workspaceParent(opened.path);
  $("workspace").title = opened.path;
  return opened;
}
// The full path lives in the title. The sidebar shows the last segment in ink
// with its parent dimmed before it, so a deep checkout never truncates to a
// slash the way a left-anchored path does.
function workspaceName(path) {
  const parts = path.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}
function workspaceParent(path) {
  const parts = path.split("/").filter(Boolean).slice(0, -1);
  return parts.length ? `${parts[parts.length - 1]}/` : "";
}
async function init() {
  const info = await api("/api/info");
  $("model").textContent = `${info.provider} / ${info.model}`;
  const stored = localStorage.getItem("chagent.workspace");
  if (stored && stored !== info.workspace) {
    try { await ensureWorkspace(stored); }
    catch { await ensureWorkspace(info.workspace); }
  } else await ensureWorkspace(info.workspace);
  await listSessions();
  refreshActive();
  const previous = localStorage.getItem("chagent.session");
  if (previous) {
    try { await openSession(previous); }
    catch (error) { localStorage.removeItem("chagent.session"); state.session = null; showError(error); }
  }
}
init().catch(showError);

// The rail's single button opened the context panel; the rail is gone, so the
// panel's own summary is the only way in (it is open by default).

// Collapsible sidebars: state persists across reloads via localStorage. The
// drawer under 960px always starts closed and never writes that state, so a
// phone cannot reload into a chat hidden behind the panel.
function applyCollapsed(name, collapsed) {
  document.body.classList.toggle(`${name}-collapsed`, collapsed);
  if (name === "sidebar") $("toggle-sidebar").setAttribute("aria-expanded", String(!collapsed));
  if (name === "sidebar" && narrow.matches) return;
  localStorage.setItem(`chagent.${name}.collapsed`, collapsed ? "1" : "0");
}
for (const name of ["sidebar", "tools"]) {
  const collapsed = name === "sidebar" && narrow.matches
    ? true : localStorage.getItem(`chagent.${name}.collapsed`) === "1";
  document.body.classList.toggle(`${name}-collapsed`, collapsed);
}
$("toggle-sidebar").setAttribute("aria-expanded", String(!document.body.classList.contains("sidebar-collapsed")));
$("scrim").addEventListener("click", () => applyCollapsed("sidebar", true));
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && narrow.matches && !document.body.classList.contains("sidebar-collapsed")) {
    applyCollapsed("sidebar", true);
  }
});
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
  const page = await api(withWorkspace(`/api/memories?status=${status}${cursor}`));
  if (version !== memoryState.version) return;
  if (!append) $("memory-list").replaceChildren();
  if (!page.items.length && !append) $("memory-list").append(textNode("p", "暂无记录。", "muted"));
  for (const memory of page.items) {
    const card = document.createElement("article"); card.className = "memory-card";
    card.append(textNode("strong", memory.key),
      textNode("span", `${memory.scope === "user" ? "用户" : "工作区"} v${memory.revision}${memory.pinned ? "，优先" : ""}`, "muted"));
    card.append(textNode("div", memory.content, "memory-body"));
    const source = document.createElement("details");
    source.append(textNode("summary", "来源与版本"), textNode("pre", JSON.stringify({origin: memory.origin, evidence: memory.evidence,
      session_id: memory.source_session_id, run_id: memory.source_run_id, source_message_ids: memory.source_message_ids}, null, 2)));
    const history = textNode("button", "查看版本记录", "quiet");
    history.addEventListener("click", async () => {
      try {
        const revisions = await api(withWorkspace(`/api/memories/${memory.id}/revisions`));
        history.remove();
        for (const revision of revisions) source.append(textNode("pre", `v${revision.revision} ${revision.action}\n${revision.payload.content}`));
      } catch (error) { memoryNotice(error.message, true); }
    });
    source.append(history); card.append(source);
    if (memory.status === "candidate") {
      const detail = await api(withWorkspace(`/api/memories/${memory.id}`));
      if (version !== memoryState.version) return;
      const conflict = detail.conflict;
      if (conflict) card.append(textNode("p", `将替换当前版本 v${conflict.revision}：${conflict.content}`, "memory-conflict"));
      const accept = textNode("button", conflict ? "替换上述记忆并采纳" : "采纳", "memory-accept");
      accept.addEventListener("click", async () => {
        accept.disabled = true;
        try {
          await post(withWorkspace(`/api/memories/${memory.id}/accept`), {expected_revision: memory.revision,
            replace_id: conflict?.id || null, replace_revision: conflict?.revision || null});
          memoryNotice("候选已采纳。"); await loadMemories();
        } catch (error) { memoryNotice(error.message, true); accept.disabled = false; }
      });
      const reject = textNode("button", "拒绝", "quiet");
      reject.addEventListener("click", async () => {
        try { await post(withWorkspace(`/api/memories/${memory.id}/reject`), {expected_revision: memory.revision}); await loadMemories(); }
        catch (error) { memoryNotice(error.message, true); }
      });
      card.append(accept, reject);
    }
    if (["active", "candidate"].includes(memory.status)) {
      const edit = textNode("button", "编辑", "quiet"); edit.addEventListener("click", () => editMemory(memory));
      const remove = textNode("button", "删除", "quiet");
      remove.addEventListener("click", async () => {
        try {
          await memoryRequest(withWorkspace(`/api/memories/${memory.id}`), "DELETE", {expected_revision: memory.revision});
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
    if (memory) await memoryRequest(withWorkspace(`/api/memories/${memory.id}`), "PATCH", {...fields, expected_revision: memory.revision});
    else await post(withWorkspace("/api/memories"), {...fields, scope: $("memory-scope").value, key: $("memory-key").value});
    $("memory-editor").hidden = true; memoryState.editing = null; memoryNotice("已保存。"); await loadMemories();
  } catch (error) { memoryNotice(error.message, true); }
});

// Workspace picker: what the database has seen, plus any directory the user
// types. Switching keeps the chat surface but drops the previous session.
function workspaceNotice(message, failed = false) {
  $("workspace-notice").textContent = message;
  $("workspace-notice").classList.toggle("failed", failed);
}
async function switchWorkspace(path) {
  await ensureWorkspace(path);
  state.session = null; localStorage.removeItem("chagent.session");
  showArchived(false); state.query = ""; $("session-search").value = "";
  resetStream(); state.loading = false; state.readonly = false; setBusy(false);
  $("messages").replaceChildren(); $("empty").hidden = false; $("session-title").textContent = "聊天";
  $("usage-pill").hidden = true;
  await listSessions();
}
async function loadWorkspaces() {
  const page = await api("/api/workspaces");
  const target = $("workspace-list"); target.replaceChildren();
  for (const item of page.items) {
    const row = document.createElement("button");
    row.type = "button"; row.className = "workspace-row";
    row.classList.toggle("active", item.path === state.workspace);
    const label = textNode("span", `${workspaceParent(item.path)}${workspaceName(item.path)}`);
    label.title = item.path;
    row.append(label, textNode("span", `${item.sessions} 个会话`, "muted"));
    row.addEventListener("click", () => {
      switchWorkspace(item.path).then(() => $("workspace-dialog").close())
        .catch(error => workspaceNotice(error.message, true));
    });
    target.append(row);
  }
  if (!page.items.length) target.append(textNode("p", "还没有记录。", "muted"));
}
$("workspace-button").addEventListener("click", () => {
  workspaceNotice(""); $("workspace-path").value = "";
  $("workspace-dialog").showModal();
  loadWorkspaces().catch(error => workspaceNotice(error.message, true));
});
$("close-workspace").addEventListener("click", () => $("workspace-dialog").close());
$("workspace-form").addEventListener("submit", async event => {
  event.preventDefault();
  const path = $("workspace-path").value.trim();
  if (!path) return;
  try {
    await switchWorkspace(path);
    $("workspace-dialog").close();
  } catch (error) { workspaceNotice(error.message, true); }
});

// Runtime configuration: the same facts the CLI prints as `Behavior:`.
const configLabels = [
  ["provider", "供应商"], ["model", "模型"], ["tools", "工具数"],
  ["run_timeout", "单次运行上限"], ["sandbox", "沙箱"], ["approval", "审批"],
  ["host_commands", "宿主命令"], ["web_access", "联网"], ["compaction", "上下文压缩"],
  ["tokenizer", "token 计数"], ["max_turns", "最大轮数"], ["max_processes", "会话进程"],
];
function configValue(key, c) {
  switch (key) {
    case "tools": return `${c.tools} 个`;
    case "run_timeout": return `${Math.round(c.run_timeout)} 秒`;
    case "sandbox": return c.sandbox === "off" ? "关闭" : `${c.sandbox}，${c.sandbox_network ? "允许出网" : "禁止出网"}`;
    case "approval": return `${c.approval}，等待 ${Math.round(c.approval_timeout)} 秒，` +
      (c.approval_grace ? `无人查看 ${Math.round(c.approval_grace)} 秒后放弃` : "无人查看也等到超时");
    case "host_commands": return c.host_commands ? "开启" : "关闭";
    case "web_access": return c.web_access === "off" ? "关闭" : `${c.web_access}，密钥${c.web_key === "set" ? "已配置" : "缺失"}`;
    case "compaction": return c.compaction ? "开启" : "关闭";
    case "max_turns": return `${c.max_turns} 轮`;
    case "max_processes": return `${c.max_processes} 个，单个 ${Math.round(c.process_timeout)} 秒`;
    default: return String(c[key]);
  }
}
async function loadConfig() {
  const config = await api("/api/config");
  const target = $("config-list"); target.replaceChildren();
  for (const [key, label] of configLabels) {
    target.append(textNode("dt", label), textNode("dd", configValue(key, config)));
  }
  target.append(textNode("dd", config.behavior, "config-behavior"));
}
async function loadConfigUsage() {
  const target = $("config-usage"); target.replaceChildren();
  const usage = await api("/api/usage?days=7");
  if (!usage.items.length) { target.append(textNode("p", "近 7 天没有模型调用。")); return; }
  target.append(textNode("p", `近 7 天 ${formatTokens(usage.totals.total_tokens)} tokens，` +
    `${usage.totals.runs} 次运行，缓存命中 ${formatRate(usage.totals.cache_hit_rate)}`));
  // A day is a row: three figures that are compared, not three facts in a sentence.
  const rows = usage.items.slice(0, 7).map(day => ({...day}));
  target.append(usageTable(rows, [["day", "日期"], ["model_calls", "调用"], ["total_tokens", "合计"]]));
}
$("open-config").addEventListener("click", () => {
  const notice = $("config-notice");
  notice.textContent = ""; notice.classList.remove("failed");
  $("config-dialog").showModal();
  loadConfig().catch(error => { notice.textContent = error.message; notice.classList.add("failed"); });
  loadConfigUsage().catch(error => { notice.textContent = error.message; notice.classList.add("failed"); });
});
$("close-config").addEventListener("click", () => $("config-dialog").close());
