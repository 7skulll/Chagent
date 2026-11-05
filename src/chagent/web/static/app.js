"use strict";

// Module boundaries live in docs/前端重构方案.md. core/ has no knowledge of the
// conversation; everything below may use it, and core/ never imports upward.
import { $, textNode } from "./core/dom.js";
import { api, post, patch } from "./core/api.js";

const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const state = {session: null, run: null, source: null, version: 0, before: null, sessionCursor: null,
  busy: false, loading: false, workspace: null, archived: false, query: "", readonly: false,
  //: Which face this session resolves; null while loading or when no session is open.
  //: A PATCH is in flight, so a second click would race its response.
  active: new Map(), sources: new Map(), runMemories: new Map(), promptPrefix: ""};
let liveText = null;
// Streamed answer text, painted once per animation frame instead of per delta.
let liveBuffer = "";
let liveFrame = 0;
const toolRows = new Map();
// markdown.js is a separate file so it can be tested on its own; if it failed to
// load, the answer is still shown, just as preformatted text.
const markdown = window.ChagentMarkdown || (() => {
  const escape = text => { const node = document.createElement("span"); node.textContent = String(text == null ? "" : text); return node.innerHTML; };
  return {renderMarkdown: text => `<p>${escape(text)}</p>`, escapeHtml: escape};
})();
//: Answers longer than this open folded to their first eight lines.
const CLAMP_CHARS = 2000;
const ARCHIVED_HINT = "已归档，恢复后才能继续对话";
// Under 960px the sidebar is a drawer over the chat, not a column beside it.
const narrow = window.matchMedia("(max-width: 960px)");
const narrowTools = window.matchMedia("(max-width: 1180px)");

// Every workspace-scoped call names its workspace; the server refuses ones that
// were never opened, so a stale tab cannot act in a directory it did not pick.
const withWorkspace = path => state.workspace
  ? `${path}${path.includes("?") ? "&" : "?"}workspace=${encodeURIComponent(state.workspace)}`
  : path;
function showError(error) { $("error").textContent = error.message || String(error); $("error").hidden = false; }
function clearError() { $("error").hidden = true; }
// ---------- 滚动跟随 ----------
// Following the stream is the default. Scrolling up hands control back to the
// reader — new text must not yank the page — and the button is how they return.
const NEAR_BOTTOM = 96;
let follow = true;
function nearBottom() {
  const view = $("conversation");
  return view.scrollHeight - view.scrollTop - view.clientHeight < NEAR_BOTTOM;
}
function scrollBottom() {
  const view = $("conversation");
  view.scrollTop = view.scrollHeight;
  follow = true;
  $("jump-latest").hidden = true;
}
$("conversation").addEventListener("scroll", () => {
  follow = nearBottom();
  $("jump-latest").hidden = follow;
});
$("jump-latest").addEventListener("click", scrollBottom);

function scheduleLiveFlush() {
  if (liveFrame) return;
  liveFrame = requestAnimationFrame(() => {
    liveFrame = 0;
    if (!liveText) return;
    liveText.innerHTML = markdown.renderMarkdown(liveBuffer);
    if (follow) scrollBottom();
  });
}
// One place decides whether the composer accepts input: a run in flight, a
// turn loading, and an archived session all block it for different reasons.
function updateComposer() {
  const pending = uploads.items.some(item => ["queued", "uploading", "pending"].includes(item.status));
  const blocked = state.busy || state.loading || state.readonly || pending || !$("input").value.trim();
  $("send").disabled = blocked;
  $("attach").disabled = state.readonly;
  $("input").disabled = state.readonly;
  $("input").placeholder = state.readonly ? "该会话已归档，恢复后可继续" : "和 Chagent 一起工作";
  $("send").title = pending ? "等待附件上传完成" : "发送";
  $("composer-hint").textContent = pending ? "附件上传中，完成后即可发送" : "Enter 发送，Shift + Enter 换行";
}
function resizeComposer() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = `${Math.max(56, Math.min(220, input.scrollHeight))}px`;
}
$("input").addEventListener("input", () => { resizeComposer(); updateComposer(); });
function setBusy(busy) {
  state.busy = busy;
  updateComposer();
  $("stop").hidden = !busy;
  if (busy) $("status").textContent = "正在生成…";
  else $("status").textContent = state.readonly ? ARCHIVED_HINT : "就绪";
}
// ---------- 会话模式开关 ----------
// Mode decides the whole tool face, so it is shown as state, not as a setting:
// The empty state's openers. Each is a task the base tool surface can finish
// (read, search, explain), so nothing here promises a network call or a host
// command the current configuration may refuse. They fill the composer rather
// than sending: the first message should be yours to edit.
const STARTERS = [
  "列出工作区里的文件，帮我了解有哪些资料",
  "阅读我上传的文档，概括重点并标明出处",
  "根据已有资料整理一份可下载的 Markdown 摘要",
];
function renderStarters() {
  const target = $("starters");
  target.replaceChildren(textNode("p", "试试", "starters-label"));
  const labels = [["浏览资料", "看看工作区里有哪些文件"], ["阅读文档", "概括重点，保留原文出处"], ["整理成果", "生成一份可下载的摘要"]];
  for (const [index, prompt] of STARTERS.entries()) {
    const row = textNode("button", "", "starter");
    row.append(textNode("span", labels[index][0], "starter-title"), textNode("span", labels[index][1], "starter-caption"));
    row.setAttribute("aria-label", prompt);
    row.type = "button";
    row.addEventListener("click", () => {
      if (state.readonly) return;
      $("input").value = prompt;
      resizeComposer(); updateComposer();
      $("input").focus();
      $("input").setSelectionRange(prompt.length, prompt.length);
    });
    target.append(row);
  }
}
renderStarters();

// ---------- 消息渲染（WK1） ----------
function plainText(message) {
  const content = message.role === "toolResult" && message.artifact
    ? [{type: "text", text: message.artifact.preview}] : message.content;
  if (typeof content === "string") return content;
  return (content || []).filter(block => block.type === "text").map(block => block.text).join("\n");
}
// Escaping happens inside markdown.js, before any structure is recognised, so
// this is never fed raw model output as markup.
function contentNode(text, prose) {
  const node = document.createElement("div");
  if (prose) { node.className = "content md"; node.innerHTML = markdown.renderMarkdown(text); }
  else { node.className = "content"; node.textContent = text; }
  return node;
}
// An answer over 2 000 characters is read in two passes: first eight lines, then
// the whole thing. The button sits outside the clipped box, so it stays visible.
function clampContent(node, text) {
  if (text.length <= CLAMP_CHARS) return null;
  const label = `展开全文（${text.length} 字）`;
  node.classList.add("clamped");
  const button = textNode("button", label, "read-more");
  button.type = "button";
  button.addEventListener("click", () => {
    button.textContent = node.classList.toggle("clamped") ? label : "收起";
  });
  return button;
}
async function copyText(text, button) {
  const label = button.textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "已复制";
    setTimeout(() => { button.textContent = label; }, 1200);
  } catch {
    showError(new Error("浏览器拒绝了剪贴板访问，请手动选中后复制"));
  }
}

// Download the original answer, including Markdown, rather than rendered HTML.
function downloadAnswer(message, meta) {
  const text = plainText(message);
  if (!text.trim()) return;
  const url = URL.createObjectURL(new Blob([text], {type: "text/markdown;charset=utf-8"}));
  const link = document.createElement("a");
  const id = String(meta.id || "answer").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 64) || "answer";
  link.href = url;
  link.download = `chagent-${id}.md`;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- 依据（WK3） ----------
// Web results and document locators travel in the tool result's details, so the
// transcript can rebuild them without another request. Memory is the exception:
// it is not a message, so the session loads its run→memory map once.
function collectSources(runId, message) {
  if (message.role !== "toolResult" || !runId) return;
  const details = message.details;
  if (!details || typeof details !== "object") return;
  let bucket = state.sources.get(runId);
  if (!bucket) { bucket = {web: [], docs: []}; state.sources.set(runId, bucket); }
  if (message.tool_name === "web_search" && Array.isArray(details.results)) {
    for (const row of details.results) {
      if (!row || typeof row.url !== "string" || bucket.web.some(item => item.url === row.url)) continue;
      // site_name is the provider's own label and is better than a parsed host.
      bucket.web.push({title: row.title || row.url, url: row.url, snippet: row.snippet || "",
                       site: row.site_name || ""});
    }
  } else if (message.tool_name === "web_fetch" && typeof details.url === "string") {
    // A failed fetch never reaches here: it is an error result with no details.
    if (!bucket.web.some(item => item.url === details.url)) {
      bucket.web.push({title: details.title || details.url, url: details.url, snippet: "",
                       site: hostOf(details.url)});
    }
  } else if (message.tool_name === "read_file" && details.format === "document"
             && typeof details.path === "string") {
    const citations = Array.isArray(details.citations) ? details.citations : [];
    const existing = bucket.docs.find(item => item.path === details.path);
    if (existing) existing.citations.push(...citations);
    else bucket.docs.push({path: details.path, citations});
  }
}
function hostOf(url) { try { return new URL(url).host; } catch { return ""; } }
// "第 2、3 页" for a PDF, the heading path for a Word file, the sheet name for
// Excel — whichever locator the parser actually produced.
function locatorLabel(citations) {
  const pages = [...new Set(citations.map(item => item.page).filter(page => page != null))];
  if (pages.length) return `第 ${pages.join("、")} 页`;
  const sections = [...new Set(citations.map(item => item.section).filter(Boolean))];
  return sections.slice(0, 3).join(" · ");
}
function appendSources(node, runId) {
  const bucket = state.sources.get(runId) || {web: [], docs: []};
  const memories = state.runMemories.get(runId) || [];
  const total = bucket.web.length + bucket.docs.length + memories.length;
  // No sources, no card: an empty 依据 would be a promise with nothing behind it.
  if (!total) return;
  const details = document.createElement("details");
  details.className = "sources";
  details.append(textNode("summary", `依据 · ${total} 处`));
  for (const item of bucket.web) {
    const card = textNode("div", "", "source-card");
    const link = document.createElement("a");
    link.href = item.url; link.target = "_blank"; link.rel = "noopener noreferrer";
    link.textContent = item.title;
    card.append(link, textNode("span", item.site || hostOf(item.url), "source-host"));
    if (item.snippet) card.append(textNode("p", item.snippet, "source-snippet"));
    details.append(card);
  }
  for (const doc of bucket.docs) {
    const row = textNode("div", "", "source-doc");
    row.append(textNode("code", doc.path), textNode("span", locatorLabel(doc.citations), "source-pages"));
    details.append(row);
  }
  for (const memory of memories) {
    const row = textNode("div", "", "source-memory");
    row.append(textNode("strong", `${memory.key} v${memory.revision}`),
      textNode("span", memory.scope === "user" ? "用户偏好" : "工作区事实", "source-host"));
    row.append(textNode("p", memory.content, "source-snippet"));
    details.append(row);
  }
  node.append(details);
}
// Only the last assistant article of a run carries its 依据: a run may answer in
// several model turns, and repeating the same list under each would be noise.
function attachSources(nodes) {
  const last = new Map();
  for (const node of nodes) {
    if (!node || !node.classList || !node.classList.contains("assistant") || !node.dataset.run) continue;
    last.set(node.dataset.run, node);
  }
  for (const [runId, node] of last) appendSources(node, runId);
}

// ---------- 重新生成 / 编辑重发 / 重试（WK2） ----------
async function requestRun(payload) {
  const id = state.session;
  if (!id || state.busy || state.loading || state.readonly) return;
  clearError(); state.loading = true; updateComposer();
  try {
    await post(`/api/sessions/${id}/runs`, payload);
    if (id === state.session) await openSession(id);
  } catch (error) { showError(error); }
  finally { state.loading = false; updateComposer(); }
}
function startEdit(node, message, meta) {
  if (node.querySelector(".message-edit")) return;
  const content = node.querySelector(".content");
  const form = textNode("div", "", "message-edit");
  const area = document.createElement("textarea");
  area.value = plainText(message);
  area.maxLength = 16000;
  area.setAttribute("aria-label", "编辑这条消息后重发");
  const actions = textNode("div", "", "message-edit-actions");
  const cancel = textNode("button", "取消");
  const send = textNode("button", "发送", "primary");
  cancel.type = send.type = "button";
  actions.append(cancel, send);
  form.append(area, actions);
  if (content) content.hidden = true;
  node.append(form);
  area.focus();
  area.setSelectionRange(area.value.length, area.value.length);
  const close = () => { form.remove(); if (content) content.hidden = false; };
  cancel.addEventListener("click", close);
  send.addEventListener("click", () => {
    const text = area.value.trim();
    if (!text) { area.focus(); return; }
    close();
    requestRun({text, edit_message_id: meta.id});
  });
  area.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send.click(); }
  });
}
function messageActions(message, meta, node) {
  const row = textNode("div", "", "message-actions");
  const copy = textNode("button", "复制");
  copy.type = "button";
  copy.addEventListener("click", () => copyText(plainText(message), copy));
  row.append(copy);
  if (message.role === "assistant" && plainText(message).trim()) {
    const download = textNode("button", "下载");
    download.type = "button";
    download.title = "下载 Markdown 文件";
    download.addEventListener("click", () => downloadAnswer(message, meta));
    row.append(download);
  }
  // A replaced record is history: offering to replace it again would fork the
  // transcript without a tree to hold the branches.
  const actionable = meta.id && meta.run_id && !meta.superseded_by && !state.readonly && !state.busy;
  if (message.role === "assistant" && actionable) {
    const again = textNode("button", "重新生成");
    again.type = "button";
    again.addEventListener("click", () => requestRun({regenerate_from: meta.id}));
    row.append(again);
  }
  if (message.role === "user" && meta.id && !meta.superseded_by && !state.readonly && !state.busy) {
    const edit = textNode("button", "编辑");
    edit.type = "button";
    edit.addEventListener("click", () => startEdit(node, message, meta));
    row.append(edit);
  }
  return row;
}

// Only files actually created by the model belong in the artifact panel.
function fileUrl(session, file) {
  return `/api/sessions/${encodeURIComponent(session)}/files/${encodeURIComponent(file.id)}`;
}
function fileRow(file, session, inMessage = false) {
  const row = document.createElement("div"); row.className = inMessage ? "generated-file message-file" : "generated-file";
  const icon = textNode("span", (file.name.split(".").pop() || "文件").toUpperCase().slice(0, 4), "generated-file-icon");
  const info = document.createElement("div"); info.className = "generated-file-info";
  info.append(textNode("span", file.name, "generated-file-name"), textNode("span", `${file.size} 字节`, "generated-file-size"));
  const download = textNode("a", "下载", "generated-file-download");
  download.href = fileUrl(session, file); download.download = file.name;
  download.setAttribute("aria-label", `下载 ${file.name}`);
  if (file.workspace_id && file.path) {
    if (file.status === "committed") {
      download.href = `/api/workspaces/${file.workspace_id}/content?${new URLSearchParams({path:file.path})}`;
      const open = textNode("button", "打开文件", "generated-file-download"); open.type = "button";
      open.addEventListener("click", () => window.ChagentFiles?.open(file.workspace_id,file.path).catch(showError));
      info.append(open);
    } else {
      download.removeAttribute("href"); download.textContent = "等待确认";
      const view = textNode("button", "查看输出状态", "generated-file-download"); view.type="button";
      view.addEventListener("click", async () => {
        try {
          const output=await api(`/api/workspaces/${file.workspace_id}/outputs/${file.id}`);
          if(output.status === "committed") await window.ChagentFiles?.open(file.workspace_id,output.output_path);
          else { window.ChagentShowFiles?.();await window.ChagentFiles?.refresh(); }
        } catch(error) { showError(error); }
      }); info.append(view);
    }
  } else {
    const save = textNode("button", "保存到工作区", "generated-file-download"); save.type="button";
    save.addEventListener("click", async () => {
      try {
        await post(`/api/sessions/${session}/files/${file.id}/export`, {});
        window.ChagentShowFiles?.(); await window.ChagentFiles?.refresh();
      } catch(error) { showError(error); }
    }); info.append(save);
  }
  row.append(icon, info, download);
  return row;
}
async function loadGeneratedFiles() {
  const id = state.session, version = state.version, list = $("artifacts-list");
  if (!id) { $("artifacts-count").textContent = "0"; list.replaceChildren(textNode("p", "选择会话后查看文件。", "artifacts-empty")); return; }
  const result = await api(`/api/sessions/${id}/files`);
  if (version !== state.version || id !== state.session) return;
  const items = result.items || [];
  $("artifacts-count").textContent = String(items.length);
  list.replaceChildren(...items.map(file => fileRow(file, id)));
  if (!items.length) list.append(textNode("p", "生成文件后会显示在这里。", "artifacts-empty"));
  window.ChagentFiles?.refresh().catch(showError);
}

function displayMessage(message, target = $("messages"), meta = {}, live = false) {
  if (message.role === "system") return null;
  const node = document.createElement("article");
  node.className = `message ${message.role === "user" ? "user" : "assistant"}`;
  if (meta.id) node.dataset.messageId = meta.id;
  if (meta.run_id) node.dataset.run = meta.run_id;
  collectSources(meta.run_id, message);
  // The name stays for screen readers; on screen the bubble and the alignment
  // already say who is speaking, and repeating it above every message is noise.
  node.append(textNode("div", message.role === "user" ? "你" : message.role === "toolResult" ? "工具结果" : "Chagent", "role sr-only"));
  const content = message.role === "toolResult" && message.artifact
    ? [{type: "text", text: message.artifact.preview}] : message.content;
  // Only an answer is prose. A user message keeps the newlines it was typed
  // with, and a tool result is data, not something to typographically improve.
  const prose = message.role === "assistant";
  const addText = text => {
    const body = contentNode(text, prose);
    node.append(body);
    if (prose && !live) { const more = clampContent(body, text); if (more) node.append(more); }
  };
  if (typeof content === "string") addText(content);
  else for (const block of content || []) {
    if (block.type === "text") addText(block.text);
    if (block.type === "toolCall") {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.append(textNode("code", block.name, "tool-name"), textNode("span", "调用", "tool-state"));
      details.append(summary, textNode("pre", JSON.stringify(block.arguments, null, 2)));
      node.append(details);
    }
  }
  if (message.role === "toolResult" && message.details?.workspace_files && !message.is_error) {
    for(const file of message.details.workspace_files) node.append(fileRow(file,state.session,true));
  }
  if (message.role === "toolResult" && message.details?.workspace_file && !message.is_error) {
    node.append(fileRow(message.details.workspace_file, state.session, true));
  }
  if (message.role === "toolResult" && message.tool_name === "create_file" && message.details && message.details.generated_file && !message.is_error) {
    node.append(fileRow(message.details.generated_file, state.session, true));
  }
  if (message.role === "toolResult") {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.append(textNode("code", message.tool_name, "tool-name"),
      textNode("span", message.is_error ? "失败" : "完成", message.is_error ? "tool-state failed" : "tool-state"));
    if (message.artifact) summary.append(textNode("span", `产物 ${message.artifact.char_count} 字符`, "tool-note"));
    details.append(summary);
    for (const child of Array.from(node.children).slice(1)) { if (!child.classList.contains("message-file")) details.append(child); }
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
  if (message.error_message) {
    node.append(textNode("div", message.error_message, "tool-status failed"));
    // A failed turn keeps its question in the transcript, so retrying is the
    // same request as regenerating that answer — no new user message is written.
    if (meta.id && meta.run_id && !meta.superseded_by && !state.readonly) {
      const retry = textNode("button", "重试", "quiet");
      retry.type = "button";
      retry.addEventListener("click", () => requestRun({regenerate_from: meta.id}));
      node.append(retry);
    }
  }
  if (!live) node.append(messageActions(message, meta, node));
  // Thinking is internal; only response text and tool records are displayed.
  if (!live && !node.querySelector(".content, details, .tool-status")) return null;
  if (meta.superseded_by) {
    // Kept, not deleted: the old answer is what the reader compares against.
    const fold = document.createElement("details");
    fold.className = "superseded-fold";
    fold.append(textNode("summary", message.role === "user" ? "已编辑的原文" : "被重新生成的回答"));
    fold.append(node);
    target.append(fold);
    return fold;
  }
  target.append(node);
  return node;
}
function resetStream() {
  if (state.source) state.source.close();
  state.source = null; state.run = null; liveText = null; toolRows.clear(); $("live").replaceChildren();
  liveBuffer = "";
  if (liveFrame) { cancelAnimationFrame(liveFrame); liveFrame = 0; }
}
async function listSessions(append = false) {
  const epoch = state.navEpoch = (state.navEpoch || 0) + 1;
  const workspaces = await api("/api/workspaces");
  if (epoch !== state.navEpoch) return;
  const target = $("sessions");
  if (!append) target.replaceChildren();
  let currentPage = null;
  for (const workspace of workspaces.items) {
    if (append && workspace.path !== state.workspace) continue;
    if (state.query && workspace.path !== state.workspace) continue;
    const group = document.createElement("section"); group.className = "workspace-group";
    const heading = textNode("button", workspaceName(workspace.path), "workspace-group-title");
    heading.type = "button"; heading.title = workspace.path;
    heading.disabled = !workspace.available;
    heading.addEventListener("click", () => switchWorkspace(workspace.path).catch(showError));
    group.append(heading);
    if (!workspace.available) {
      group.append(textNode("p", workspace.reason || "工作区不可用", "muted"));
      target.append(group); continue;
    }
    const add = textNode("button", "＋ 新对话", "workspace-new"); add.type = "button";
    add.addEventListener("click", async () => {
      try {
        await switchWorkspace(workspace.path);
        $("new-session").click();
      } catch (error) { showError(error); }
    });
    group.append(add);
    const params = new URLSearchParams({workspace: workspace.path});
    if (state.archived) params.set("archived", "only");
    if (state.query) params.set("q", state.query);
    if (append && state.sessionCursor) params.set("before", state.sessionCursor);
    // Register explicitly before listing: historical registration is not permission.
    try {
      await post("/api/workspaces", {path: workspace.path, touch: false});
      const page = await api(`/api/sessions?${params}`);
      if (epoch !== state.navEpoch) return;
      for (const session of page.items) {
        const row = sessionRow(session);
        row.dataset.workspace = workspace.path;
        group.append(row);
      }
      if (workspace.path === state.workspace) currentPage = page;
      if (page.next_cursor && workspace.path !== state.workspace) {
        const more = textNode("button", "切换以查看更多", "load-more");
        more.addEventListener("click", () => switchWorkspace(workspace.path).catch(showError));
        group.append(more);
      }
    } catch (error) { group.append(textNode("p", error.message, "muted")); }
    target.append(group);
  }
  state.sessionCursor = currentPage?.next_cursor;
  $("more-sessions").hidden = !state.sessionCursor;
  $("sessions-label").textContent = state.query ? "当前工作区搜索" : state.archived ? "已归档 · 按项目" : "项目";
  if (currentPage) {
    $("archived-count").textContent = currentPage.archived_count || 0;
    $("archived-toggle").hidden = !currentPage.archived_count && !state.archived;
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
  open.addEventListener("click", async () => {
    try {
      if (row.dataset.workspace && row.dataset.workspace !== state.workspace) await switchWorkspace(row.dataset.workspace);
      await openSession(session.id);
    } catch (error) { showError(error); }
  });
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
async function openSession(id, {preserveScroll = false} = {}) {
  const view = $("conversation");
  const keepPosition = preserveScroll && id === state.session && !follow;
  const oldTop = view.scrollTop;
  const anchor = keepPosition ? Array.from($("messages").children).find(node =>
    node.dataset.messageId && node.getBoundingClientRect().bottom > view.getBoundingClientRect().top) : null;
  const anchorId = anchor && anchor.dataset.messageId;
  const anchorTop = anchor && anchor.getBoundingClientRect().top;
  const version = ++state.version;
  resetStream(); clearError(); state.session = id; state.readonly = false; state.loading = true;
  // The previous session's mode must not stand in for this one while the fetch
  // is in flight: the switch would show the wrong face under the new title.
  setBusy(false);
  $("messages").replaceChildren(); $("empty").hidden = true;
  $("artifacts-list").replaceChildren(textNode("p", "正在加载…", "artifacts-empty")); $("artifacts-count").textContent = "0";
  state.sources = new Map(); state.runMemories = new Map();
  localStorage.setItem("chagent.session", id);
  for (const row of $("sessions").querySelectorAll(".session-row")) row.classList.toggle("active", row.dataset.id === id);
  try {
    const [session, page, sources] = await Promise.all([
      api(`/api/sessions/${id}`), api(`/api/sessions/${id}/messages`),
      // Citations are an enhancement: a failure here must not cost the transcript.
      api(`/api/sessions/${id}/sources`).catch(() => ({runs: []}))]);
    if (version !== state.version) return;
    if (session.workspace !== state.workspace) await ensureWorkspace(session.workspace);
    $("session-title").textContent = session.title || "未命名会话";
    // An archived session is still readable; only new input is refused. The
    // server enforces the same rule, this is the part the user can see.
    state.readonly = Boolean(session.archived_at);
    updateComposer();
    if (narrow.matches) applyCollapsed("sidebar", true);
    if (state.readonly) $("status").textContent = ARCHIVED_HINT;
    state.runMemories = new Map((sources.runs || []).map(run => [run.run_id, run.memories || []]));
    const active = !state.readonly && session.latest_run && !terminal.has(session.latest_run.status) ? session.latest_run : null;
    const nodes = [];
    for (const item of page.items) {
      // SSE replays the entire active run, so render only its user input from history.
      if (active && item.run_id === active.id && item.message.role !== "user") continue;
      nodes.push(displayMessage(item.message, $("messages"), item));
    }
    attachSources(nodes);
    state.before = page.next_before; $("older").hidden = !state.before;
    $("empty").hidden = page.items.length > 0;
    if (active) connectRun(active.id, version);
    else if (session.latest_run) {
      showRunStatus(session.latest_run);
      watchMemoryJob(session.latest_run.id, version);
    }
    loadUsagePill();
    loadGeneratedFiles().catch(showError);
    if ($("context-panel").open) loadContext().catch(showError);
    if (keepPosition) {
      const restored = Array.from($("messages").children).find(node => node.dataset.messageId === anchorId);
      view.scrollTop = restored ? oldTop + restored.getBoundingClientRect().top - anchorTop : oldTop;
      follow = nearBottom();
      $("jump-latest").hidden = follow;
    } else scrollBottom();
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
    unwatched: "无人查看审批，操作未执行"};
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
  for (const row of $("sessions").querySelectorAll(".session-row")) {
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
    if (type === "approval_requested") { showApproval(runId).catch(showError); $("status").textContent = "等待确认操作…"; }
    if (["approval_decided", "approval_expired", "approval_cancelled"].includes(type)) hideApproval();
    if (type === "compaction_failed") {
      $("live").append(textNode("div", "压缩未完成，继续使用原有摘要和近期完整轮次。", "tool-status failed"));
    }
    if (type === "model_started") { liveText = null; liveBuffer = ""; $("status").textContent = "正在思考…"; }
    if (type === "text_delta") {
      if (!liveText) {
        const row = displayMessage({role: "assistant", content: ""}, $("live"), {}, true);
        liveText = row && row.querySelector(".content");
        liveBuffer = "";
      }
      liveBuffer += payload.delta;
      // One repaint per frame, not per token: a 3 000-token answer would
      // otherwise re-lay out the whole paragraph thousands of times.
      scheduleLiveFlush();
      $("status").textContent = "正在生成…";
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
    if (follow) scrollBottom();
  });
  source.addEventListener("end", () => {
    source.close();
    if (version === state.version) openSession(state.session, {preserveScroll: true}).catch(showError);
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
  // A write is not a command: the card has to say what it would replace, not
  // name the tool and leave the user reading JSON.
  const writeLabels = {write_file: "写入文件", edit_file: "修改文件", apply_patch: "应用补丁", create_directory: "新建目录"};
  const row = textNode("div", "", "approval");
  row.id = "approval";
  row.append(textNode("div", `需要确认：${writeLabels[request.tool_name] || request.tool_name}`, "approval-title"));
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
  if (!state.workspace) { $("workspace-button").click(); return; }
  if (!text.trim() || state.busy || state.loading || state.readonly || uploads.items.some(item => ["queued", "uploading", "pending"].includes(item.status))) return;
  clearError(); state.loading = true; updateComposer();
  try {
    if (!state.session) {
      const session = await post("/api/sessions", {title: text.trim().slice(0, 30), workspace: state.workspace}); state.session = session.id;
      await listSessions();
    }
    const id = state.session;
    await post(`/api/sessions/${id}/runs`, {text});
    $("input").value = "";
    resizeComposer();
    state.promptPrefix = "";
    // The files just sent are part of the message now; a failed one stays in the
    // queue so it can be retried without retyping anything.
    uploads.items = uploads.items.filter(item => item.status !== "done");
    renderUploads();
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

// ---------- 上传（WK4） ----------
// The file body is posted as-is: the endpoint takes raw bytes with any content
// type, so a 20 MiB report never becomes a base64 string inside JSON. XHR is
// used instead of fetch for one reason: upload progress events.
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
//: What read_file can actually open. Anything else is refused here rather than
//: uploaded and then reported as unreadable three tool calls later.
const READABLE = /\.(pdf|docx|xlsx|txt|md|markdown|rst|json|jsonl|ndjson|yaml|yml|toml|csv|tsv|log|py|js|mjs|cjs|jsx|ts|tsx|go|rs|java|kt|rb|php|c|h|cc|cpp|hpp|cs|swift|m|mm|sh|bash|zsh|fish|sql|html|htm|css|scss|sass|less|vue|svelte|xml|ini|cfg|conf|env|properties|gradle|lock|mod|sum|proto|graphql|gql)$/i;
const IMAGE = /\.(png|jpe?g|gif|webp|bmp|svg|heic|avif)$/i;
const uploads = {items: [], nextId: 1, running: false};

function readableName(name) {
  if (!name.includes(".")) return true;   // README, Makefile, LICENSE
  return READABLE.test(name);
}
function uploadPaths() {
  return uploads.items.filter(item => item.status === "done" && item.readable).map(item => item.path);
}
// The paths ride in the composer instead of being sent behind the user's back:
// they can edit or drop the sentence before pressing send. The prefix is tracked
// so removing a file rewrites the line instead of appending a second one.
function applyUploadPrompt() {
  const input = $("input");
  const paths = uploadPaths();
  const previous = state.promptPrefix;
  if (previous && input.value.startsWith(previous)) input.value = input.value.slice(previous.length);
  state.promptPrefix = paths.length ? `请阅读 ${paths.join("、")}` : "";
  if (state.promptPrefix) {
    const rest = input.value.replace(/^[：:]\s*/, "");
    input.value = rest ? `${state.promptPrefix}：${rest}` : `${state.promptPrefix}：`;
  }
  // A run in flight owns the status line; the queue does not talk over it.
  if (!state.busy) {
    $("status").textContent = paths.length ? `已上传 ${paths.length} 个文件` : "就绪";
  }
}
function uploadItemView(item) {
  const row = textNode("div", "", "upload-row");
  const thumb = textNode("div", "", "upload-thumb");
  if (item.preview) {
    const image = document.createElement("img");
    image.src = item.preview; image.alt = "";
    thumb.append(image);
  } else {
    thumb.textContent = (item.name.split(".").pop() || "file").slice(0, 4);
  }
  const facts = textNode("div", "", "upload-facts");
  facts.append(textNode("div", item.name, "upload-name"));
  const meta = textNode("div", "", "upload-meta");
  const labels = {queued: "等待上传", uploading: `上传中 ${Math.round(item.progress * 100)}%`,
    pending: "等待同名输出确认", done: `已上传 ${formatSize(item.size)}`, failed: item.error || "上传失败"};
  meta.textContent = labels[item.status];
  meta.classList.toggle("failed", item.status === "failed");
  facts.append(meta);
  if (item.status === "uploading") {
    // A <progress> element, not a styled div: the page is served under
    // `style-src 'self'`, and a value is markup rather than an inline style.
    const bar = document.createElement("progress");
    bar.className = "upload-bar";
    bar.max = 1; bar.value = item.progress;
    facts.append(bar);
  }
  if (item.note) facts.append(textNode("div", item.note, "upload-meta"));
  if(item.status === "done" && item.workspaceId && item.path) {
    const open=textNode("button","打开文件","upload-meta");open.type="button";
    open.addEventListener("click",()=>window.ChagentFiles?.open(item.workspaceId,item.path).catch(showError));
    const download=textNode("a","下载","upload-meta");download.href=`/api/workspaces/${item.workspaceId}/content?${new URLSearchParams({path:item.path})}`;download.download=item.name;
    facts.append(open,download);
  }
  row.append(thumb, facts);
  if (item.status === "failed" && item.retryable) {
    const retry = textNode("button", "↻", "upload-retry");
    retry.type = "button"; retry.title = "重试上传"; retry.setAttribute("aria-label", `重试上传 ${item.name}`);
    retry.addEventListener("click", () => { item.status = "queued"; item.progress = 0; item.error = ""; renderUploads(); runUploads(); });
    row.append(retry);
  }
  const remove = textNode("button", "×", "upload-remove");
  remove.type = "button"; remove.title = "移除"; remove.setAttribute("aria-label", `移除 ${item.name}`);
  remove.addEventListener("click", async () => {
    if (item.status === "pending" && item.outputId) {
      try {
        const output = await api(`/api/workspaces/${item.workspaceId}/outputs/${item.outputId}`);
        if (output.status === "pending") await post(`/api/workspaces/${item.workspaceId}/outputs/${item.outputId}/decision`, {token:output.token,choice:"cancel"});
        await window.ChagentFiles?.refresh();
      } catch (error) { showError(error); return; }
    }
    if (item.request) { try { item.request.abort(); } catch { /* already finished */ } }
    uploads.items = uploads.items.filter(other => other !== item);
    renderUploads(); applyUploadPrompt();
  });
  row.append(remove);
  return row;
}
function renderUploads() {
  $("uploads").replaceChildren(...uploads.items.map(uploadItemView));
  updateComposer();
}
function uploadOne(item) {
  return new Promise(resolve => {
    const request = new XMLHttpRequest();
    item.request = request;
    item.status = "uploading"; item.progress = 0;
    // Painted before the first byte: the row says "上传中" the moment it is
    // queued, not when the first progress event arrives.
    renderUploads();
    if (!item.requestId) item.requestId = crypto.randomUUID();
    request.open("POST", withWorkspace(`/api/uploads?name=${encodeURIComponent(item.name)}&request_id=${item.requestId}`));
    request.setRequestHeader("Content-Type", item.file.type || "application/octet-stream");
    request.upload.addEventListener("progress", event => {
      if (!event.lengthComputable) return;
      item.progress = event.total ? event.loaded / event.total : 0;
      renderUploads();
    });
    request.addEventListener("load", () => {
      let value = {};
      try { value = JSON.parse(request.responseText || "{}"); } catch { value = {}; }
      if (request.status >= 200 && request.status < 300) {
        item.status = value.status === "pending" || value.status === "staged" ? "pending" : "done";
        item.path = value.path; item.progress = 1; item.error = "";
        item.outputId = value.output_id; item.workspaceId = value.workspace_id;
        if (item.status === "pending") {
          item.note = "同名文件等待确认：请在文件面板选择覆盖、保留两份或取消。";
          window.ChagentShowFiles?.();
        }
        window.ChagentFiles?.refresh().catch(showError);
      } else {
        item.status = "failed"; item.retryable = request.status !== 413;
        item.error = request.status === 413
          ? "文件超过服务端上限，未上传"
          : value.error || `上传失败（HTTP ${request.status}），请稍后重试`;
      }
      resolve();
    });
    request.addEventListener("error", () => {
      item.status = "failed"; item.retryable = true; item.error = "网络中断，未上传成功"; resolve();
    });
    request.addEventListener("abort", () => { item.status = "failed"; item.error = "已取消"; resolve(); });
    request.send(item.file);
  });
}
async function runUploads() {
  if (uploads.running) return;
  uploads.running = true;
  try {
    while (true) {
      const next = uploads.items.find(item => item.status === "queued");
      if (!next) break;
      await uploadOne(next);
      renderUploads();
      applyUploadPrompt();
      resizeComposer(); updateComposer();
    }
  } finally { uploads.running = false; }
}
function enqueueFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  // An archived session refuses new runs, so a queue there would only produce a
  // prompt that cannot be sent.
  if (state.readonly) { showError(new Error("该会话已归档，恢复后才能上传")); return; }
  clearError();
  for (const file of list) {
    const item = {id: uploads.nextId++, file, name: file.name || "upload", size: file.size,
      status: "queued", progress: 0, error: "", path: null, retryable: false,
      readable: readableName(file.name || ""), request: null, note: "", preview: null};
    if (file.size > MAX_UPLOAD_BYTES) {
      item.status = "failed";
      item.error = `超过 20 MiB 上限（${formatSize(file.size)}），未上传`;
    } else if (IMAGE.test(file.name || "")) {
      // Stored so the workspace has it, but read_file has no OCR: say so here
      // rather than letting the model promise a description it cannot make.
      item.note = "图片：当前工具读不了图片正文";
      previewFile(file, data => { item.preview = data; renderUploads(); });
    } else if (!item.readable) {
      item.status = "failed";
      item.error = "不支持这种文件类型（PDF / Word / Excel / 文本）";
    }
    uploads.items.push(item);
  }
  renderUploads();
  runUploads();
}
// A data: URL, not a blob: one — `img-src 'self' data:` is what the page allows.
function previewFile(file, apply) {
  const reader = new FileReader();
  reader.addEventListener("load", () => { if (typeof reader.result === "string") apply(reader.result); });
  reader.addEventListener("error", () => { /* no thumbnail; the name still shows */ });
  reader.readAsDataURL(file);
}

$("attach").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", event => {
  const files = Array.from(event.target.files || []);
  event.target.value = "";
  if (files.length) enqueueFiles(files);
});
$("input").addEventListener("paste", event => {
  const files = Array.from((event.clipboardData && event.clipboardData.files) || []);
  if (!files.length) return;
  // A pasted screenshot is a file, not text: it goes to the queue instead of
  // becoming a base64 blob in the message body.
  event.preventDefault();
  enqueueFiles(files);
});
const chat = document.querySelector(".chat");
let dragDepth = 0;
const draggingFiles = event => Array.from((event.dataTransfer && event.dataTransfer.types) || []).includes("Files");
chat.addEventListener("dragenter", event => {
  if (!draggingFiles(event)) return;
  event.preventDefault(); dragDepth += 1; chat.classList.add("drop-target");
});
chat.addEventListener("dragover", event => {
  if (!draggingFiles(event)) return;
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
});
chat.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) chat.classList.remove("drop-target");
});
chat.addEventListener("drop", event => {
  if (!draggingFiles(event)) return;
  event.preventDefault();
  dragDepth = 0; chat.classList.remove("drop-target");
  enqueueFiles(event.dataTransfer ? event.dataTransfer.files : null);
});
// One listener for every code block the renderer produces: the markup is a
// string, so per-node handlers would be re-attached on each repaint.
document.addEventListener("click", event => {
  const button = event.target.closest("[data-copy-code]");
  if (!button) return;
  const block = button.closest(".code-block");
  const code = block && block.querySelector("pre code");
  if (code) copyText(code.textContent, button);
});
$("new-session").addEventListener("click", async () => {
  if (!state.workspace) { $("workspace-button").click(); return; }
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
    const nodes = [];
    for (const item of page.items) nodes.push(displayMessage(item.message, fragment, item));
    attachSources(nodes);
    $("messages").prepend(fragment); state.before = page.next_before; $("older").hidden = !state.before;
  } catch (error) { showError(error); }
  finally { $("older").disabled = false; }
});
$("more-sessions").addEventListener("click", () => listSessions(true).catch(showError));

// Search and the archived view are two filters on the same request; each one
// re-reads the first page rather than patching the list in place.
let searchTimer = null;
$("search-sessions").addEventListener("click", () => {
  const input = $("session-search");
  input.hidden = !input.hidden;
  $("search-sessions").setAttribute("aria-expanded", String(!input.hidden));
  if (!input.hidden) { input.focus(); return; }
  if (input.value) {
    input.value = ""; state.query = ""; state.sessionCursor = null;
    listSessions().catch(showError);
  }
});
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
  $("rail-archive").setAttribute("aria-pressed", String(archived));
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
  if (window.ChagentFiles && !await window.ChagentFiles.beforeWorkspace(path)) throw new Error("已取消切换工作区；草稿保留。");
  const opened = await post("/api/workspaces", {path});
  state.workspace = opened.path;
  localStorage.setItem("chagent.workspace", opened.path);
  $("workspace").textContent = workspaceName(opened.path);
  $("workspace-parent").textContent = workspaceParent(opened.path);
  $("workspace").title = opened.path;
  window.ChagentCurrentWorkspace = opened;
  window.ChagentFiles?.workspace(opened);
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
  state.config = await api("/api/config");
  $("model").textContent = `${info.provider} / ${info.model}`;
  const stored = localStorage.getItem("chagent.workspace");
  if (stored && stored !== info.workspace) {
    try { await ensureWorkspace(stored); }
    catch {
      state.workspace = null;
      $("workspace-button").click();
      workspaceNotice("上次工作区已不可用，请明确选择本机目录。历史会话仍保留。", true);
      return;
    }
  } else if (stored) await ensureWorkspace(info.workspace);
  else {
    $("workspace-button").click();
    $("status").textContent = "请先打开本机文件夹，再开始聊天";
    return;
  }
  await listSessions();
  refreshActive();
  const previous = localStorage.getItem("chagent.session");
  if (previous) {
    try {
      const groups = await api("/api/workspaces");
      for (const item of groups.items.filter(item => item.available)) await post("/api/workspaces", {path: item.path, touch: false});
      await openSession(previous);
    }
    catch (error) { localStorage.removeItem("chagent.session"); state.session = null; loadGeneratedFiles(); showError(error); }
  }
}
init().catch(showError);

// Desktop columns remember their state. Overlay panels start closed and never
// replace desktop preferences; only one drawer can cover the conversation.
function syncDrawers() {
  const sidebarOpen = narrow.matches && !document.body.classList.contains("sidebar-collapsed");
  const toolsOpen = narrowTools.matches && !document.body.classList.contains("tools-collapsed");
  document.body.classList.toggle("drawer-open", sidebarOpen || toolsOpen);
}
function applyCollapsed(name, collapsed) {
  if (!collapsed && ((name === "sidebar" && narrow.matches) || (name === "tools" && narrowTools.matches))) {
    const other = name === "sidebar" ? "tools" : "sidebar";
    if ((other === "sidebar" && narrow.matches) || (other === "tools" && narrowTools.matches)) {
      document.body.classList.add(`${other}-collapsed`);
      $(other === "sidebar" ? "toggle-sidebar" : "toggle-tools").setAttribute("aria-expanded", "false");
    }
  }
  document.body.classList.toggle(`${name}-collapsed`, collapsed);
  $(name === "sidebar" ? "toggle-sidebar" : "toggle-tools").setAttribute("aria-expanded", String(!collapsed));
  if (name === "tools") $("rail-files").setAttribute("aria-expanded", String(!collapsed));
  syncDrawers();
  window.dispatchEvent?.(new CustomEvent("chagent-panels-changed"));
  if ((name === "sidebar" && narrow.matches) || (name === "tools" && narrowTools.matches)) return;
  localStorage.setItem(`chagent.${name}.collapsed`, collapsed ? "1" : "0");
}
function restorePanels() {
  for (const name of ["sidebar", "tools"]) {
    const overlay = (name === "sidebar" && narrow.matches) || (name === "tools" && narrowTools.matches);
    const saved = localStorage.getItem(`chagent.${name}.collapsed`);
    applyCollapsed(name, overlay || (saved === null ? false : saved === "1"));
  }
}
restorePanels();
narrow.addEventListener("change", restorePanels);
narrowTools.addEventListener("change", restorePanels);
function closeDrawers() {
  if (narrow.matches) applyCollapsed("sidebar", true);
  if (narrowTools.matches) applyCollapsed("tools", true);
}
$("scrim").addEventListener("click", closeDrawers);
$("close-tools").addEventListener("click", () => { applyCollapsed("tools", true); $("toggle-tools").focus(); });
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeDrawers();
});
$("toggle-sidebar").addEventListener("click", () =>
  applyCollapsed("sidebar", !document.body.classList.contains("sidebar-collapsed")));
$("toggle-tools").addEventListener("click", () =>
  applyCollapsed("tools", !document.body.classList.contains("tools-collapsed")));

// The compact rail is a second route to real, existing actions. Keeping these
// as forwarded clicks avoids diverging dialog, session and drawer behavior.
$("rail-home").addEventListener("click", () => $("new-session").click());
$("rail-workspace").addEventListener("click", () => $("workspace-button").click());
$("rail-files").addEventListener("click", () => window.ChagentShowFiles());
$("rail-memory").addEventListener("click", () => $("open-memory").click());
$("rail-config").addEventListener("click", () => $("open-config").click());
$("rail-archive").addEventListener("click", () => {
  if (narrow.matches) applyCollapsed("sidebar", false);
  $("archived-toggle").click();
});

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
  if (!await ensureWorkspace(path)) return;
  state.session = null; loadGeneratedFiles(); localStorage.removeItem("chagent.session");
  showArchived(false); state.query = ""; $("session-search").value = "";
  resetStream(); state.loading = false; state.readonly = false; setBusy(false);
  // An upload belongs to the workspace it landed in; carrying the queue across
  // would offer paths that the new workspace cannot see.
  uploads.items = []; state.promptPrefix = ""; $("input").value = ""; renderUploads();
  $("messages").replaceChildren(); $("empty").hidden = false; $("session-title").textContent = "聊天";
  $("usage-pill").hidden = true;
  await listSessions();
}
async function switchHostWorkspace(path) {
  if (window.ChagentFiles && !await window.ChagentFiles.beforeWorkspace(path)) throw new Error("已取消切换工作区；草稿保留。");
  const switched = await post("/api/workspaces/switch-host", {path});
  if (switched.status === "denied") throw new Error("宿主工作区切换未获确认。");
  if (switched.status !== "restarting") throw new Error("宿主工作区切换未开始。");
  workspaceNotice("已确认切换；正在重新挂载工作区并重启 Chagent…");
  localStorage.setItem("chagent.workspace", "/workspaces");
  const started = Date.now();
  const poll = async () => {
    try {
      const config = await api("/api/config");
      if (config.host_workspace_path === switched.path) { location.reload(); return; }
    } catch (_) { /* The container is restarting. */ }
    if (Date.now() - started < 90000) setTimeout(poll, 1500);
    else workspaceNotice("切换超时，请查看宿主代理日志并刷新页面。", true);
  };
  setTimeout(poll, 1500);
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
    row.append(label, textNode("span", item.available ? `${item.sessions} 个会话` : item.reason, "muted"));
    row.disabled = !item.available;
    row.addEventListener("click", () => {
      switchWorkspace(item.path).then(() => $("workspace-dialog").close())
        .catch(error => workspaceNotice(error.message, true));
    });
    target.append(row);
  }
  if (!page.items.length) target.append(textNode("p", "还没有记录。", "muted"));
}
$("workspace-button").addEventListener("click", () => {
  workspaceNotice(""); $("workspace-path").value = ""; $("workspace-browse").hidden = true;
  $("workspace-dialog").showModal();
  loadWorkspaces().catch(error => workspaceNotice(error.message, true));
});
$("close-workspace").addEventListener("click", () => $("workspace-dialog").close());
$("workspace-form").addEventListener("submit", async event => {
  event.preventDefault();
  const path = $("workspace-path").value.trim();
  if (!path) return;
  try {
    const hostExecution = (await api("/api/config")).host_execution;
    if (hostExecution && path.startsWith("/") && path !== "/workspaces" && !path.startsWith("/workspaces/")) {
      await switchHostWorkspace(path);
    } else {
      await switchWorkspace(path);
      $("workspace-dialog").close();
    }
  } catch (error) { workspaceNotice(error.message, true); }
});

// Runtime configuration: the same facts the CLI prints as `Behavior:`.
const configLabels = [
  ["provider", "供应商"], ["model", "模型"], ["tools", "工具数"],
  ["run_timeout", "单次运行上限"], ["boundary", "执行边界"], ["capability", "执行能力"], ["gate", "门禁"], ["approval", "审批"],
  ["host_commands", "命令执行"], ["workspace_writable", "工作区写入"], ["process_stdin", "交互输入"], ["web_access", "网页搜索"], ["compaction", "上下文压缩"],
  ["tokenizer", "token 计数"], ["max_turns", "最大轮数"], ["max_processes", "会话进程"],
];
function configValue(key, c) {
  switch (key) {
    case "tools": return `${c.tools} 个`;
    case "run_timeout": return `${Math.round(c.run_timeout)} 秒`;
    case "boundary": return c.boundary === "container" ? "容器（命令可联网）" : "无命令边界";
    case "capability": return c.capability === "full" ? "可执行命令" : "只读，不执行命令";
    case "gate": return c.gate === "approval" ? "逐次审批" : "无需审批";
    case "approval": return `${c.approval}，等待 ${Math.round(c.approval_timeout)} 秒，` +
      (c.approval_grace ? `无人查看 ${Math.round(c.approval_grace)} 秒后放弃` : "无人查看也等到超时");
    case "workspace_writable": return c.workspace_writable ? "允许" : "关闭";
    case "process_stdin": return c.process_stdin ? "允许" : "关闭";
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

// The system dialog only exists on macOS, and the container entry is Linux, so
// "open a folder" is the same button in both places: ask the host first, and
// let the page walk the published tree when there is no dialog to ask.
const browse = {path: null, parent: null, nextAfter: null};
async function loadBrowse(path, after = null) {
  const query = new URLSearchParams();
  if (path) query.set("path", path);
  if (after !== null) query.set("after", after);
  const page = await api(`/api/workspace-browse${query.size ? `?${query}` : ""}`);
  browse.path = page.path; browse.parent = page.parent; browse.nextAfter = page.next_after;
  $("workspace-browse-path").textContent = page.path; $("workspace-browse-path").title = page.path;
  $("workspace-browse-up").disabled = !page.parent;
  $("workspace-browse-open").textContent = `打开“${workspaceName(page.path)}”`;
  const target = $("workspace-browse-list");
  if (after === null) target.replaceChildren();
  if (!page.entries.length && after === null) target.append(textNode("p", "这里没有子文件夹。", "muted"));
  for (const entry of page.entries) {
    const row = document.createElement("button");
    row.type = "button"; row.className = "workspace-row"; row.title = entry.path;
    row.append(textNode("span", entry.name), textNode("span", entry.reason || (entry.opened ? "已打开" : ""), "muted"));
    row.disabled = !entry.available;
    if (entry.available) row.addEventListener("click", () => loadBrowse(entry.path).catch(error => workspaceNotice(error.message, true)));
    target.append(row);
  }
  $("workspace-browse-more").hidden = !browse.nextAfter;
  $("workspace-browse").hidden = false;
}
async function openBrowsedWorkspace() {
  if (!browse.path) return;
  try { await switchWorkspace(browse.path); $("workspace-browse").hidden = true; $("workspace-dialog").close(); }
  catch (error) { workspaceNotice(error.message, true); }
}
$("workspace-browse-up").addEventListener("click", () => {
  if (browse.parent) loadBrowse(browse.parent).catch(error => workspaceNotice(error.message, true));
});
$("workspace-browse-open").addEventListener("click", () => openBrowsedWorkspace());
$("workspace-browse-more").addEventListener("click", () => {
  if (browse.nextAfter) loadBrowse(browse.path, browse.nextAfter).catch(error => workspaceNotice(error.message, true));
});
$("workspace-browse-close").addEventListener("click", () => { $("workspace-browse").hidden = true; });
$("native-workspace-picker").addEventListener("click", async () => {
  const button = $("native-workspace-picker"); button.disabled = true;
  workspaceNotice("请选择本机文件夹…");
  try {
    const result = await post("/api/workspace-picker", {});
    // No system dialog here (the container entry, or another OS): navigate in
    // the page rather than refusing the request the user just made.
    if (result.native_picker === false) { workspaceNotice(""); await loadBrowse(); return; }
    if (result.cancelled) workspaceNotice("已取消选择");
    else { await switchWorkspace(result.workspace.path); $("workspace-dialog").close(); }
  } catch (error) {
    workspaceNotice(error.message, true);
    // A dialog that exists but refuses (permissions, no display) still leaves
    // the page navigable, so the failed path is not a dead end.
    await loadBrowse().catch(() => {});
  } finally { button.disabled = false; }
});

window.ChagentWorkspaceSwitch = switchWorkspace;
window.ChagentShowFiles = () => { applyCollapsed("tools", false); window.ChagentFiles?.selectFiles(); };
window.ChagentLoadContext = () => loadContext().catch(showError);
window.addEventListener("chagent-output-decision", event => {
  const output = event.detail;
  for (const item of uploads.items) if (item.outputId === output.id) {
    item.status = output.status === "committed" ? "done" : "failed";
    item.path = output.output_path || item.path;
    item.note = output.status === "committed" ? "文件已保存到工作区" : "输出已取消或超时";
  }
  renderUploads(); applyUploadPrompt(); loadGeneratedFiles().catch(showError);
});

window.ChagentCheckUploads = async () => {
  for(const item of uploads.items.filter(item=>item.status === "pending" && item.outputId)) {
    const output=await api(`/api/workspaces/${item.workspaceId}/outputs/${item.outputId}`);
    if(["committed","cancelled","expired","failed"].includes(output.status)) {
      window.dispatchEvent(new CustomEvent("chagent-output-decision", {detail:output}));
    }
  }
};

// Test seams. tests/js/render-page.mjs drives the same functions a click would,
// through an import instead of an appended `eval` line: a renamed export fails
// the test instead of silently leaving the harness with a stale name.
// Nothing in the page reads these.
export { openSession };
export function setFollowForTest(value) { follow = value; }
