/* Workspace admission, the directory pickers, the site link, and the panel
 * layout.
 *
 * Opening a directory is a server-side decision: it joins the set of roots this
 * process may touch. The choice is remembered per browser, not per session, and
 * the file panel gets a chance to veto it while drafts are unsaved.
 */

import { $, el, on, textNode } from "../core/dom.js";
import { api, post } from "../core/api.js";
import { workspaceName, workspaceParent } from "../core/format.js";

export function createWorkspaceView(ui) {
  const state = ui.state;
  const narrow = window.matchMedia("(max-width: 960px)");
  const narrowTools = window.matchMedia("(max-width: 1180px)");

  async function ensureWorkspace(path) {
    if (!await ui.beforeWorkspaceFiles(path)) throw new Error("已取消切换工作区；草稿保留。");
    const opened = await post("/api/workspaces", {path});
    state.workspace = opened.path;
    localStorage.setItem("chagent.workspace", opened.path);
    $("workspace").textContent = workspaceName(opened.path);
    $("workspace-parent").textContent = workspaceParent(opened.path);
    $("workspace").title = opened.path;
    state.currentWorkspace = opened;
    ui.workspaceFiles(opened);
    loadSiteAccess(opened).catch(() => { $("site-access").hidden = true; });
    return opened;
  }

  async function loadSiteAccess(workspace = state.currentWorkspace) {
    const row = $("site-access");
    if (state.config?.controller !== "host-controller" || !workspace?.id) { row.hidden = true; return; }
    const result = await api(`/api/workspaces/${workspace.id}/site`);
    if (result.service?.state !== "running") { row.hidden = true; return; }
    $("site-link").href = result.url;
    $("site-link").title = result.url;
    row.hidden = false;
  }

  on($("site-stop"), "click", async () => {
    const workspace = state.currentWorkspace;
    if (!workspace?.id) return;
    try {
      await post(`/api/workspaces/${workspace.id}/site/stop`, {});
      await loadSiteAccess(workspace);
    } catch (error) { ui.showError(error); }
  });

  /* ---------- switching ---------- */
  function workspaceNotice(message, failed = false) {
    $("workspace-notice").textContent = message;
    $("workspace-notice").classList.toggle("failed", failed);
  }

  async function switchWorkspace(path) {
    if (!await ensureWorkspace(path)) return;
    state.session = null; ui.loadGeneratedFiles();
    localStorage.removeItem("chagent.session");
    ui.showArchived(false); state.query = ""; $("session-search").value = "";
    ui.resetStream(); state.loading = false; state.readonly = false; ui.setBusy(false);
    // An upload belongs to the workspace it landed in; carrying the queue across
    // would offer paths that the new workspace cannot see.
    state.uploads.items = []; state.promptPrefix = "";
    $("input").value = ""; ui.renderUploads();
    $("messages").replaceChildren(); $("empty").hidden = false; $("session-title").textContent = "聊天";
    $("usage-pill").hidden = true;
    await ui.listSessions();
  }

  async function loadWorkspaces() {
    const page = await api("/api/workspaces");
    const target = $("workspace-list"); target.replaceChildren();
    for (const item of page.items) {
      const row = el("button", "workspace-row");
      row.type = "button";
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

  on($("workspace-button"), "click", () => {
    workspaceNotice(""); $("workspace-path").value = ""; $("workspace-browse").hidden = true;
    $("workspace-dialog").showModal();
    loadWorkspaces().catch(error => workspaceNotice(error.message, true));
  });
  on($("close-workspace"), "click", () => $("workspace-dialog").close());
  on($("workspace-form"), "submit", async event => {
    event.preventDefault();
    const path = $("workspace-path").value.trim();
    if (!path) return;
    try {
      await switchWorkspace(path);
      $("workspace-dialog").close();
    } catch (error) { workspaceNotice(error.message, true); }
  });

  /* ---------- in-page directory picker ----------
   * The system dialog only exists on macOS, and the container entry is Linux,
   * so "open a folder" is the same button in both places: ask the host first,
   * and let the page walk the published tree when there is no dialog to ask. */
  const browse = {path: null, parent: null, nextAfter: null};

  async function loadBrowse(path, after = null) {
    const query = new URLSearchParams();
    if (path) query.set("path", path);
    if (after !== null) query.set("after", after);
    const page = await api(`/api/workspace-browse${query.size ? `?${query}` : ""}`);
    browse.path = page.path; browse.parent = page.parent; browse.nextAfter = page.next_after;
    $("workspace-browse-path").textContent = page.path;
    $("workspace-browse-path").title = page.path;
    $("workspace-browse-up").disabled = !page.parent;
    $("workspace-browse-open").textContent = `打开“${workspaceName(page.path)}”`;
    const target = $("workspace-browse-list");
    if (after === null) target.replaceChildren();
    if (!page.entries.length && after === null) target.append(textNode("p", "这里没有子文件夹。", "muted"));
    for (const entry of page.entries) {
      const row = el("button", "workspace-row");
      row.type = "button"; row.title = entry.path;
      row.append(textNode("span", entry.name), textNode("span", entry.reason || (entry.opened ? "已打开" : ""), "muted"));
      row.disabled = !entry.available;
      if (entry.available) {
        row.addEventListener("click", () => loadBrowse(entry.path).catch(error => workspaceNotice(error.message, true)));
      }
      target.append(row);
    }
    $("workspace-browse-more").hidden = !browse.nextAfter;
    $("workspace-browse").hidden = false;
  }

  async function openBrowsedWorkspace() {
    if (!browse.path) return;
    try {
      await switchWorkspace(browse.path);
      $("workspace-browse").hidden = true;
      $("workspace-dialog").close();
    } catch (error) { workspaceNotice(error.message, true); }
  }

  on($("workspace-browse-up"), "click", () => {
    if (browse.parent) loadBrowse(browse.parent).catch(error => workspaceNotice(error.message, true));
  });
  on($("workspace-browse-open"), "click", () => openBrowsedWorkspace());
  on($("workspace-browse-more"), "click", () => {
    if (browse.nextAfter) loadBrowse(browse.path, browse.nextAfter).catch(error => workspaceNotice(error.message, true));
  });
  on($("workspace-browse-close"), "click", () => { $("workspace-browse").hidden = true; });
  on($("native-workspace-picker"), "click", async () => {
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

  /* ---------- panels ----------
   * Desktop columns remember their state. Overlay panels start closed and never
   * replace desktop preferences; only one drawer can cover the conversation. */
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
    ui.panelsChanged();
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

  function closeDrawers() {
    if (narrow.matches) applyCollapsed("sidebar", true);
    if (narrowTools.matches) applyCollapsed("tools", true);
  }

  on($("scrim"), "click", closeDrawers);
  on($("close-tools"), "click", () => { applyCollapsed("tools", true); $("toggle-tools").focus(); });
  on(document, "keydown", event => { if (event.key === "Escape") closeDrawers(); });
  on($("toggle-sidebar"), "click", () =>
    applyCollapsed("sidebar", !document.body.classList.contains("sidebar-collapsed")));
  on($("toggle-tools"), "click", () =>
    applyCollapsed("tools", !document.body.classList.contains("tools-collapsed")));

  // The compact rail is a second route to real, existing actions. Keeping these
  // as forwarded clicks avoids diverging dialog, session and drawer behavior.
  on($("rail-home"), "click", () => $("new-session").click());
  on($("rail-workspace"), "click", () => $("workspace-button").click());
  on($("rail-files"), "click", () => ui.showFiles());
  on($("rail-memory"), "click", () => $("open-memory").click());
  on($("rail-config"), "click", () => $("open-config").click());
  on($("rail-archive"), "click", () => {
    if (narrow.matches) applyCollapsed("sidebar", false);
    $("archived-toggle").click();
  });

  /* The file panel asks for the tools column whenever it opens a file. */
  function showFiles() {
    applyCollapsed("tools", false);
    ui.selectFiles();
  }

  restorePanels();
  narrow.addEventListener("change", restorePanels);
  narrowTools.addEventListener("change", restorePanels);

  return {
    ensureWorkspace, switchWorkspace, loadSiteAccess, applyCollapsed, showFiles,
    isNarrow: () => narrow.matches,
    isNarrowTools: () => narrowTools.matches,
    workspaceNotice,
  };
}
