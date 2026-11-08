/* Page entry point and the only wiring point.
 *
 * Each view owns one column and is created with the same `ui` object. Actions
 * are written into that object as their owner is created, and every call is a
 * plain property read at click time: a missing action throws, it does not
 * silently do nothing the way an optional chain on a page global did.
 *
 * What stays here: the page state, the SSE connection that feeds the reducer,
 * the run renderer that draws it, the startup sequence, and the test seam.
 * Everything else lives in core/, render/, run/ or views/.
 */

import { $, on } from "./core/dom.js";
import { copyText } from "./core/clipboard.js";
import { api, post } from "./core/api.js";
import { createStore } from "./core/store.js";
import { initialRunState, reduce } from "./run/run-reducer.js";
import { createRunRenderer } from "./run/run-render.js";
import { createMessageRenderer } from "./render/message.js";
import { createChatView } from "./views/chat.js";
import { createConfigView } from "./views/config.js";
import { createContextView } from "./views/context.js";
import { createGeneratedView } from "./views/generated.js";
import { createMemoryView } from "./views/memory.js";
import { createOperationsView } from "./views/operations.js";
import { createSessionsView } from "./views/sessions.js";
import { createUploadsView } from "./views/uploads.js";
import { createWorkspaceView } from "./views/workspace.js";
import { initFileEditor } from "./file-editor.js";

const state = {
  session: null, run: null, source: null, version: 0, before: null, sessionCursor: null,
  busy: false, loading: false, workspace: null, archived: false, query: "",
  readonly: false, active: new Map(), sources: new Map(), runMemories: new Map(),
  promptPrefix: "", config: null, currentWorkspace: null,
  //: The attachment queue lives in the page state so the composer can decide
  //: whether sending is allowed without importing the upload view.
  uploads: {items: [], nextId: 1, running: false},
};

function showError(error) {
  $("error").textContent = error.message || String(error);
  $("error").hidden = false;
}
function clearError() { $("error").hidden = true; }

/* Actions every module may call. Filled in below, in dependency order, and read
 * only when a handler runs. */
const ui = {state, showError, clearError};

/* ---------- the run stream ----------
 * One reducer produces the state, one renderer draws it. The renderer never
 * reads the event or the DOM tree it painted last time from anywhere but its
 * own node map, so painting the same state twice changes nothing. */
const runStore = createStore(initialRunState());
const runRenderer = createRunRenderer({
  root: $("live"),
  status: $("status"),
  decide: (approvalId, approve) => decideApproval(approvalId, approve),
  afterPaint: () => { if (ui.isFollowing()) ui.scrollBottom(); },
});
runStore.subscribe(runState => runRenderer.schedule(runState));

async function loadApproval(runId) {
  try {
    const data = await api(`/api/runs/${runId}/approvals`);
    if (!data.pending.length || state.run !== runId) {
      runStore.update(current => reduce(current, {type: "approval_decided"}));
      return;
    }
    // One card per batch: the server decides every row of the batch together,
    // so the card must show them together.
    const first = data.pending[0];
    const items = first.batch_id
      ? data.pending.filter(row => row.batch_id === first.batch_id)
        .sort((left, right) => (left.batch_index ?? 0) - (right.batch_index ?? 0))
      : [first];
    const request = {...first, items: items.map(row => ({id: row.id, tool_name: row.tool_name,
                                                         subject: row.subject}))};
    runStore.update(current => reduce(current, {type: "approval_loaded", payload: {request}}));
  } catch (error) {
    runStore.update(current => reduce(current, {type: "approval_loaded", payload: {error: error.message}}));
  }
}

async function decideApproval(approvalId, approve) {
  try { await post(`/api/approvals/${approvalId}/decide`, {approve}); }
  catch (error) { showError(error); }
  finally { runStore.update(current => reduce(current, {type: "approval_decided"})); }
}

function connectRun(runId, version) {
  state.run = runId;
  ui.setBusy(true);
  $("empty").hidden = true;
  ui.noteActive();
  const source = new EventSource(`/api/runs/${runId}/events`);
  state.source = source;
  source.addEventListener("update", event => {
    if (version !== state.version) return;
    const event_ = JSON.parse(event.data);
    runStore.update(current => reduce(current, event_));
    if (event_.type === "approval_requested") loadApproval(runId);
  });
  source.addEventListener("end", () => {
    source.close();
    if (version === state.version) openSession(state.session, {preserveScroll: true}).catch(showError);
    ui.refreshActive();
    ui.loadSiteAccess().catch(() => {});
  });
  source.onerror = async () => {
    if (version !== state.version) return;
    $("status").textContent = "连接中断，正在重连…";
    // EventSource handles transient reconnects and Last-Event-ID automatically.
    try {
      await api(`/api/runs/${runId}`);
    } catch (error) { source.close(); ui.setBusy(false); showError(error); }
  };
}

function resetStream() {
  if (state.source) state.source.close();
  state.source = null;
  state.run = null;
  runRenderer.reset();
  runStore.update(() => initialRunState(null));
}

/* ---------- message rendering ----------
 * The message renderer needs actions, and the actions live in the views that
 * are created below. It receives a ctx that looks them up when a button is
 * pressed, not when this module is evaluated. */
const messages = createMessageRenderer({
  state,
  showError: error => showError(error),
  requestRun: payload => ui.requestRun(payload),
  openFile: (workspaceId, path) => ui.openFile(workspaceId, path),
  showFiles: () => ui.showFiles(),
  refreshFiles: () => ui.refreshFiles(),
});
ui.displayMessage = messages.displayMessage;
ui.attachSources = messages.attachSources;
ui.connectRun = connectRun;
ui.resetStream = resetStream;

/* ---------- views ---------- */
const chat = createChatView(ui);
Object.assign(ui, {
  setBusy: chat.setBusy, updateComposer: chat.updateComposer, resizeComposer: chat.resizeComposer,
  scrollBottom: chat.scrollBottom, nearBottom: chat.nearBottom, requestRun: chat.requestRun,
  showRunStatus: chat.showRunStatus, watchMemoryJob: chat.watchMemoryJob,
  appendRunSummary: chat.appendRunSummary,
  loadUsagePill: chat.loadUsagePill, isFollowing: () => chat.following,
  setFollow: value => chat.setFollow(value),
});

// Delegated handler for every code block the renderer produces: the markup is a
// string, so per-node handlers would be re-attached on each repaint.
on(document, "click", event => {
  const button = event.target.closest("[data-copy-code]");
  if (!button) return;
  const block = button.closest(".code-block");
  const code = block && block.querySelector("pre code");
  if (code) copyText(code.textContent, button, showError);
});

const files = initFileEditor(ui);
Object.assign(ui, {
  beforeWorkspaceFiles: files.beforeWorkspace, workspaceFiles: files.workspace,
  openFile: files.open, refreshFiles: files.refresh, selectFiles: files.selectFiles,
  panelsChanged: files.panelsChanged,
});

const uploads = createUploadsView(ui);
Object.assign(ui, {
  renderUploads: uploads.renderUploads, applyUploadPrompt: uploads.applyUploadPrompt,
  outputChanged: uploads.outputChanged, checkPendingUploads: uploads.checkPendingUploads,
});

const generated = createGeneratedView(ui);
ui.loadGeneratedFiles = generated.loadGeneratedFiles;

const context = createContextView(ui);
ui.loadContext = context.loadContext;

const operations = createOperationsView(ui);
ui.loadOperations = operations.loadOperations;

const sessions = createSessionsView(ui);
Object.assign(ui, {
  listSessions: sessions.listSessions, openSession: sessions.openSession,
  showArchived: sessions.showArchived, refreshActive: sessions.refreshActive,
  noteActive: sessions.noteActive,
});

const workspace = createWorkspaceView(ui);
Object.assign(ui, {
  ensureWorkspace: workspace.ensureWorkspace, switchWorkspace: workspace.switchWorkspace,
  loadSiteAccess: workspace.loadSiteAccess, applyCollapsed: workspace.applyCollapsed,
  showFiles: workspace.showFiles, isNarrow: workspace.isNarrow,
});

const memory = createMemoryView(ui);
ui.loadMemories = memory.loadMemories;

const config = createConfigView(ui);
Object.assign(ui, {loadConfig: config.loadConfig, loadConfigUsage: config.loadConfigUsage});

/* ---------- startup ---------- */
async function init() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const loginToken = fragment.get("auth");
  if (loginToken) {
    const response = await fetch("/api/auth", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: loginToken})});
    if (!response.ok) throw new Error("本机网页认证失败，请重新运行 Chagent 启动器");
    history.replaceState(null, "", location.pathname + location.search);
  }
  const info = await api("/api/info");
  state.config = await api("/api/config");
  $("model").textContent = `${info.provider} / ${info.model}`;
  const stored = localStorage.getItem("chagent.workspace");
  if (stored && stored !== info.workspace) {
    try { await ui.ensureWorkspace(stored); }
    catch {
      state.workspace = null;
      $("workspace-button").click();
      workspace.workspaceNotice("上次工作区已不可用，请明确选择本机目录。历史会话仍保留。", true);
      return;
    }
  } else if (stored) await ui.ensureWorkspace(info.workspace);
  else {
    $("workspace-button").click();
    $("status").textContent = "请先打开本机文件夹，再开始聊天";
    return;
  }
  await ui.listSessions();
  ui.refreshActive();
  const previous = localStorage.getItem("chagent.session");
  if (previous) {
    try {
      const groups = await api("/api/workspaces");
      for (const item of groups.items.filter(entry => entry.available)) {
        await post("/api/workspaces", {path: item.path, touch: false});
      }
      await openSession(previous);
    } catch (error) {
      localStorage.removeItem("chagent.session");
      state.session = null;
      ui.loadGeneratedFiles();
      showError(error);
    }
  }
}
init().catch(showError);

/* Test seams. tests/js/render-page.mjs drives the same functions a click would,
 * through an import instead of an appended `eval` line: a renamed export fails
 * the test instead of silently leaving the harness with a stale name.
 * Nothing in the page reads these. */
export const openSession = sessions.openSession;
export function setFollowForTest(value) { chat.setFollow(value); }
