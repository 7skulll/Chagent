/* The session column: groups, rows, menus, renaming, archiving, search, and the
 * one function that opens a session into the transcript.
 *
 * `openSession` is also the recovery path: it reads stored messages and paints
 * them with the same message renderer the live stream uses, which is why a
 * refresh does not produce a different-looking conversation.
 */

import { storage } from "../core/account-storage.js";
import { $, el, on, textNode } from "../core/dom.js";
import { api, patch, post } from "../core/api.js";
import { ARCHIVED_HINT, isTerminal, workspaceName } from "../core/format.js";
import { runSummaryView } from "../render/run-summary.js";

export function createSessionsView(ui) {
  const state = ui.state;

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
      const group = el("section", "workspace-group");
      const heading = textNode("button", workspaceName(workspace.path), "workspace-group-title");
      heading.type = "button"; heading.title = workspace.path;
      heading.disabled = !workspace.available;
      heading.addEventListener("click", () => ui.switchWorkspace(workspace.path).catch(ui.showError));
      group.append(heading);
      if (!workspace.available) {
        group.append(textNode("p", workspace.reason || "工作区不可用", "muted"));
        target.append(group); continue;
      }
      const add = textNode("button", "＋ 新对话", "workspace-new"); add.type = "button";
      add.addEventListener("click", async () => {
        try {
          await ui.switchWorkspace(workspace.path);
          $("new-session").click();
        } catch (error) { ui.showError(error); }
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
          more.addEventListener("click", () => ui.switchWorkspace(workspace.path).catch(ui.showError));
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
    const row = el("div", "session-row");
    row.classList.toggle("active", session.id === state.session);
    row.dataset.id = session.id;
    row.dataset.archived = session.archived_at ? "1" : "";
    const open = el("button", "session-open");
    open.type = "button";
    open.title = session.title || "未命名会话";
    // The dot is the only signal that a run is still going in a session the user
    // is not looking at; it is painted by the active-run poll, not by this row.
    const dot = el("span", "dot"); dot.hidden = true;
    const name = el("span", "session-name", session.title || "未命名会话");
    open.append(dot, name);
    open.addEventListener("click", async () => {
      try {
        if (row.dataset.workspace && row.dataset.workspace !== state.workspace) {
          await ui.switchWorkspace(row.dataset.workspace);
        }
        await openSession(session.id);
      } catch (error) { ui.showError(error); }
    });
    const menu = el("button", "session-menu", "⋯");
    menu.type = "button";
    menu.title = "会话操作";
    menu.setAttribute("aria-label", "会话操作");
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
    const pop = el("div", "session-menu-pop");
    const rename = textNode("button", "重命名");
    rename.addEventListener("click", () => { closeSessionMenus(); startRename(row, session); });
    const toggle = textNode("button", session.archived_at ? "恢复" : "归档");
    toggle.addEventListener("click", () => { closeSessionMenus(); setArchived(session, !session.archived_at); });
    pop.append(rename, toggle);
    row.append(pop);
  }

  function startRename(row, session) {
    const input = el("input", "session-rename");
    input.maxLength = 200; input.value = session.title || "";
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
        } catch (error) { ui.showError(error); }
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
    } catch (error) { ui.showError(error); }
  }

  on(document, "click", event => {
    if (!event.target.closest(".session-menu-pop") && !event.target.closest(".session-menu")) closeSessionMenus();
  });

  async function openSession(id, {preserveScroll = false} = {}) {
    const view = $("conversation");
    const keepPosition = preserveScroll && id === state.session && !ui.isFollowing();
    const oldTop = view.scrollTop;
    const anchor = keepPosition ? Array.from($("messages").children).find(node =>
      node.dataset.messageId && node.getBoundingClientRect().bottom > view.getBoundingClientRect().top) : null;
    const anchorId = anchor && anchor.dataset.messageId;
    const anchorTop = anchor && anchor.getBoundingClientRect().top;
    const version = ++state.version;
    ui.resetStream(); ui.clearError();
    state.session = id; state.readonly = false; state.loading = true;
    // The previous session's mode must not stand in for this one while the fetch
    // is in flight: the switch would show the wrong face under the new title.
    ui.setBusy(false);
    $("messages").replaceChildren(); $("empty").hidden = true;
    $("artifacts-list").replaceChildren(textNode("p", "正在加载…", "artifacts-empty"));
    $("artifacts-count").textContent = "0";
    state.sources = new Map(); state.runMemories = new Map();
    storage.setItem("chagent.session", id);
    for (const row of $("sessions").querySelectorAll(".session-row")) {
      row.classList.toggle("active", row.dataset.id === id);
    }
    try {
      const [session, page, sources] = await Promise.all([
        api(`/api/sessions/${id}`), api(`/api/sessions/${id}/messages`),
        // Citations are an enhancement: a failure here must not cost the transcript.
        api(`/api/sessions/${id}/sources`).catch(() => ({runs: []}))]);
      if (version !== state.version) return;
      if (session.workspace !== state.workspace) await ui.ensureWorkspace(session.workspace);
      $("session-title").textContent = session.title || "未命名会话";
      // An archived session is still readable; only new input is refused.
      state.readonly = Boolean(session.archived_at);
      ui.updateComposer();
      if (ui.isNarrow()) ui.applyCollapsed("sidebar", true);
      if (state.readonly) $("status").textContent = ARCHIVED_HINT;
      state.runMemories = new Map((sources.runs || []).map(run => [run.run_id, run.memories || []]));
      const active = !state.readonly && session.latest_run && !isTerminal(session.latest_run.status)
        ? session.latest_run : null;
      const nodes = [];
      for (const item of page.items) {
        // SSE replays the entire active run, so render only its user input from history.
        if (active && item.run_id === active.id && item.message.role !== "user") continue;
        nodes.push(ui.displayMessage(item.message, $("messages"), item));
      }
      ui.attachSources(nodes);
      state.before = page.next_before; $("older").hidden = !state.before;
      $("empty").hidden = page.items.length > 0;
      if (active) ui.connectRun(active.id, version);
      else if (session.latest_run) {
        ui.showRunStatus(session.latest_run);
        ui.watchMemoryJob(session.latest_run.id, version);
        // The live column is cleared when the run ends; read the closing record
        // back so a fast run's last notice is still there after a refresh.
        ui.appendRunSummary(session.latest_run.id, version);
      }
      ui.loadUsagePill();
      ui.loadGeneratedFiles().catch(ui.showError);
      if ($("context-panel").open) ui.loadContext().catch(ui.showError);
      if (keepPosition) {
        const restored = Array.from($("messages").children).find(node => node.dataset.messageId === anchorId);
        view.scrollTop = restored ? oldTop + restored.getBoundingClientRect().top - anchorTop : oldTop;
        ui.setFollow(ui.nearBottom());
        $("jump-latest").hidden = ui.isFollowing();
      } else ui.scrollBottom();
    } finally {
      if (version === state.version) { state.loading = false; ui.updateComposer(); }
    }
  }

  /* ---------- 后台运行可见 ---------- */
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

  /* ---------- new session / archived filter / search ---------- */
  on($("new-session"), "click", async () => {
    if (!state.workspace) { $("workspace-button").click(); return; }
    try {
      // A new session is never archived: leave that view so it is visible.
      if (state.archived) showArchived(false);
      const session = await post("/api/sessions", {title: "新会话", workspace: state.workspace});
      await listSessions(); await openSession(session.id); $("input").focus();
    } catch (error) { ui.showError(error); }
  });
  on($("more-sessions"), "click", () => listSessions(true).catch(ui.showError));

  // Search and the archived view are two filters on the same request; each one
  // re-reads the first page rather than patching the list in place.
  let searchTimer = null;
  on($("search-sessions"), "click", () => {
    const input = $("session-search");
    input.hidden = !input.hidden;
    $("search-sessions").setAttribute("aria-expanded", String(!input.hidden));
    if (!input.hidden) { input.focus(); return; }
    if (input.value) {
      input.value = ""; state.query = ""; state.sessionCursor = null;
      listSessions().catch(ui.showError);
    }
  });
  on($("session-search"), "input", event => {
    const value = event.target.value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = value; state.sessionCursor = null;
      listSessions().catch(ui.showError);
    }, 250);
  });

  function showArchived(archived) {
    state.archived = archived; state.sessionCursor = null;
    $("archived-toggle").setAttribute("aria-pressed", String(archived));
    $("rail-archive").setAttribute("aria-pressed", String(archived));
    $("archived-toggle").hidden = false;
  }
  on($("archived-toggle"), "click", () => {
    showArchived(!state.archived);
    listSessions().catch(ui.showError);
  });

  return {listSessions, openSession, showArchived, refreshActive, noteActive, paintActivity};
}
