/* The conversation column: openers, composer, stop, older pages, usage, scroll.
 *
 * Owns the parts of the page that answer "what am I looking at right now". It
 * knows the page state and the API, but not the session list, the upload queue
 * or the file panel: those arrive through `ui`.
 */

import { $, el, on, textNode } from "../core/dom.js";
import { api, post } from "../core/api.js";
import { ARCHIVED_HINT, NEAR_BOTTOM, formatRate, formatTokens, isPendingUpload, runStatusText } from "../core/format.js";
import { usageTable } from "../render/usage.js";

// The empty state's openers. Each is a task the base tool surface can finish
// (read, search, explain), so nothing here promises a network call or a host
// command the current configuration may refuse.
const STARTERS = [
  "列出工作区里的文件，帮我了解有哪些资料",
  "阅读我上传的文档，概括重点并标明出处",
  "根据已有资料整理一份可下载的 Markdown 摘要",
];

export function createChatView(ui) {
  const state = ui.state;

  /* ---------- scroll follow ----------
   * Following the stream is the default. Scrolling up hands control back to the
   * reader — new text must not yank the page — and the button is how they
   * return. */
  let follow = true;
  const nearBottom = () => {
    const view = $("conversation");
    return view.scrollHeight - view.scrollTop - view.clientHeight < NEAR_BOTTOM;
  };
  function scrollBottom() {
    const view = $("conversation");
    view.scrollTop = view.scrollHeight;
    follow = true;
    $("jump-latest").hidden = true;
  }
  on($("conversation"), "scroll", () => {
    follow = nearBottom();
    $("jump-latest").hidden = follow;
  });
  on($("jump-latest"), "click", scrollBottom);

  /* ---------- composer ---------- */
  function updateComposer() {
    const pending = state.uploads.items.some(isPendingUpload);
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
  on($("input"), "input", () => { resizeComposer(); updateComposer(); });

  // One place decides whether the composer accepts input: a run in flight, a
  // turn loading, and an archived session all block it for different reasons.
  function setBusy(busy) {
    state.busy = busy;
    updateComposer();
    $("stop").hidden = !busy;
    if (busy) $("status").textContent = "正在生成…";
    else $("status").textContent = state.readonly ? ARCHIVED_HINT : "就绪";
  }

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

  /* ---------- 重新生成 / 编辑重发 / 重试 ---------- */
  async function requestRun(payload) {
    const id = state.session;
    if (!id || state.busy || state.loading || state.readonly) return;
    ui.clearError(); state.loading = true; updateComposer();
    try {
      await post(`/api/sessions/${id}/runs`, payload);
      if (id === state.session) await ui.openSession(id);
    } catch (error) { ui.showError(error); }
    finally { state.loading = false; updateComposer(); }
  }

  on($("composer"), "submit", async event => {
    event.preventDefault();
    const text = $("input").value;
    if (!state.workspace) { $("workspace-button").click(); return; }
    if (!text.trim() || state.busy || state.loading || state.readonly || state.uploads.items.some(isPendingUpload)) return;
    ui.clearError(); state.loading = true; updateComposer();
    try {
      if (!state.session) {
        const session = await post("/api/sessions", {title: text.trim().slice(0, 30), workspace: state.workspace});
        state.session = session.id;
        await ui.listSessions();
      }
      const id = state.session;
      await post(`/api/sessions/${id}/runs`, {text});
      $("input").value = "";
      resizeComposer();
      state.promptPrefix = "";
      // The files just sent are part of the message now; a failed one stays in
      // the queue so it can be retried without retyping anything.
      state.uploads.items = state.uploads.items.filter(item => item.status !== "done");
      ui.renderUploads();
      if (id === state.session) await ui.openSession(id);
    } catch (error) { ui.showError(error); }
    finally { state.loading = false; updateComposer(); }
  });
  on($("input"), "keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault(); $("composer").requestSubmit();
    }
  });
  on($("stop"), "click", async () => {
    if (!state.run) return;
    try { await post(`/api/runs/${state.run}/cancel`, {}); $("status").textContent = "正在停止…"; }
    catch (error) { ui.showError(error); }
  });

  /* ---------- older pages ---------- */
  on($("older"), "click", async () => {
    const id = state.session, before = state.before, version = state.version;
    if (!id || !before) return;
    $("older").disabled = true;
    try {
      const page = await api(`/api/sessions/${id}/messages?before=${before}`);
      if (version !== state.version) return;
      const fragment = document.createDocumentFragment();
      const nodes = [];
      for (const item of page.items) nodes.push(ui.displayMessage(item.message, fragment, item));
      ui.attachSources(nodes);
      $("messages").prepend(fragment);
      state.before = page.next_before; $("older").hidden = !state.before;
    } catch (error) { ui.showError(error); }
    finally { $("older").disabled = false; }
  });

  /* ---------- run status ---------- */
  function showRunStatus(run) {
    $("status").textContent = state.readonly ? ARCHIVED_HINT : runStatusText(run);
    if (run.error && run.status !== "cancelled") ui.showError(new Error(run.error));
  }

  // Memory extraction finishes after the answer; the poll only runs on a run
  // that already stopped streaming, so its line goes straight into the live
  // column rather than through the reducer.
  async function watchMemoryJob(runId, version) {
    let row = null;
    const started = Date.now();
    const refresh = async () => {
      if (version !== state.version) return;
      try {
        const {job} = await api(`/api/runs/${runId}/memory-job`);
        if (version !== state.version || !job) return;
        if (!row) { row = el("div", "tool-status"); $("live").append(row); }
        const labels = {queued: "等待后台整理记忆", running: "正在后台整理记忆",
          completed: "后台记忆整理完成", failed: "后台记忆整理未完成，回答已保留"};
        row.textContent = labels[job.status];
        row.classList.toggle("failed", job.status === "failed");
        if (["queued", "running"].includes(job.status) && Date.now() - started < 120000) setTimeout(refresh, 1000);
      } catch {
        if (version === state.version && row) row.textContent = "后台记忆状态暂时不可用";
      }
    };
    await refresh();
  }

  /* ---------- 用量（只报 token，不报钱） ---------- */
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

  on($("usage-pill"), "click", () => {
    $("usage-dialog").showModal();
    loadUsageDialog().catch(error => {
      $("usage-notice").textContent = error.message;
      $("usage-notice").classList.add("failed");
    });
  });
  on($("close-usage"), "click", () => $("usage-dialog").close());

  renderStarters();

  return {
    setBusy, updateComposer, resizeComposer, scrollBottom, requestRun,
    showRunStatus, watchMemoryJob, loadUsagePill,
    // Test seam: tests/js/render-page.mjs drives the same scroll policy a reader
    // would by scrolling. Nothing in the page reads this.
    setFollow(value) { follow = value; },
    nearBottom() { return nearBottom(); },
    get following() { return follow; },
  };
}
