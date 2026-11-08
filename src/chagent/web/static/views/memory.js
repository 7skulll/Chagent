/* Long-term memory: list, filter, explicit edit, candidate review, revisions.
 *
 * Generated candidates never auto-activate; this dialog is the only place a
 * candidate becomes effective, and that is a user decision by construction.
 */

import { $, el, on, textNode } from "../core/dom.js";
import { api, post, send, withWorkspace } from "../core/api.js";

export function createMemoryView(ui) {
  const state = ui.state;
  const memoryState = {cursor: null, editing: null, version: 0};

  function memoryNotice(message, failed = false) {
    $("memory-notice").textContent = message;
    $("memory-notice").classList.toggle("failed", failed);
  }

  const scoped = path => withWorkspace(path, state.workspace);
  const memoryRequest = (path, method, value) => send(path, method, value);

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
    const page = await api(scoped(`/api/memories?status=${status}${cursor}`));
    if (version !== memoryState.version) return;
    if (!append) $("memory-list").replaceChildren();
    if (!page.items.length && !append) $("memory-list").append(textNode("p", "暂无记录。", "muted"));
    for (const memory of page.items) {
      const card = el("article", "memory-card");
      card.append(textNode("strong", memory.key),
        textNode("span", `${memory.scope === "user" ? "用户" : "工作区"} v${memory.revision}${memory.pinned ? "，优先" : ""}`, "muted"));
      card.append(el("div", "memory-body", memory.content));
      const source = el("details");
      source.append(textNode("summary", "来源与版本"), textNode("pre", JSON.stringify({
        origin: memory.origin, evidence: memory.evidence,
        session_id: memory.source_session_id, run_id: memory.source_run_id,
        source_message_ids: memory.source_message_ids}, null, 2)));
      const history = textNode("button", "查看版本记录", "quiet");
      history.addEventListener("click", async () => {
        try {
          const revisions = await api(scoped(`/api/memories/${memory.id}/revisions`));
          history.remove();
          for (const revision of revisions) {
            source.append(textNode("pre", `v${revision.revision} ${revision.action}\n${revision.payload.content}`));
          }
        } catch (error) { memoryNotice(error.message, true); }
      });
      source.append(history); card.append(source);
      if (memory.status === "candidate") {
        const detail = await api(scoped(`/api/memories/${memory.id}`));
        if (version !== memoryState.version) return;
        const conflict = detail.conflict;
        if (conflict) card.append(textNode("p", `将替换当前版本 v${conflict.revision}：${conflict.content}`, "memory-conflict"));
        const accept = textNode("button", conflict ? "替换上述记忆并采纳" : "采纳", "memory-accept");
        accept.addEventListener("click", async () => {
          accept.disabled = true;
          try {
            await post(scoped(`/api/memories/${memory.id}/accept`), {expected_revision: memory.revision,
              replace_id: conflict?.id || null, replace_revision: conflict?.revision || null});
            memoryNotice("候选已采纳。"); await loadMemories();
          } catch (error) { memoryNotice(error.message, true); accept.disabled = false; }
        });
        const reject = textNode("button", "拒绝", "quiet");
        reject.addEventListener("click", async () => {
          try {
            await post(scoped(`/api/memories/${memory.id}/reject`), {expected_revision: memory.revision});
            await loadMemories();
          } catch (error) { memoryNotice(error.message, true); }
        });
        card.append(accept, reject);
      }
      if (["active", "candidate"].includes(memory.status)) {
        const edit = textNode("button", "编辑", "quiet");
        edit.addEventListener("click", () => editMemory(memory));
        const remove = textNode("button", "删除", "quiet");
        remove.addEventListener("click", async () => {
          try {
            await memoryRequest(scoped(`/api/memories/${memory.id}`), "DELETE", {expected_revision: memory.revision});
            memoryNotice("已停止使用这条记忆。"); await loadMemories();
          } catch (error) { memoryNotice(error.message, true); }
        });
        card.append(edit, remove);
      }
      $("memory-list").append(card);
    }
    memoryState.cursor = page.next_cursor;
    $("more-memories").hidden = !page.next_cursor;
  }

  on($("open-memory"), "click", () => {
    $("memory-dialog").showModal();
    memoryNotice("");
    loadMemories().catch(error => memoryNotice(error.message, true));
  });
  on($("close-memory"), "click", () => $("memory-dialog").close());
  on($("new-memory"), "click", () => editMemory());
  on($("cancel-memory-edit"), "click", () => { $("memory-editor").hidden = true; memoryState.editing = null; });
  on($("memory-filter"), "change", () => loadMemories().catch(error => memoryNotice(error.message, true)));
  on($("more-memories"), "click", () => loadMemories(true).catch(error => memoryNotice(error.message, true)));
  on($("memory-editor"), "submit", async event => {
    event.preventDefault();
    try {
      const memory = memoryState.editing;
      const fields = {content: $("memory-content").value, pinned: $("memory-pinned").checked};
      if (memory) {
        await memoryRequest(scoped(`/api/memories/${memory.id}`), "PATCH", {...fields, expected_revision: memory.revision});
      } else {
        await post(scoped("/api/memories"), {...fields, scope: $("memory-scope").value, key: $("memory-key").value});
      }
      $("memory-editor").hidden = true; memoryState.editing = null;
      memoryNotice("已保存。"); await loadMemories();
    } catch (error) { memoryNotice(error.message, true); }
  });

  // Task history is derived evidence, not memory: it is shown with its sources
  // and it never enters the model's context on its own.
  async function loadTaskHistory() {
    const list = $("task-history-list");
    if (!list.hidden) { list.hidden = true; return; }
    try {
      const page = await api(scoped("/api/task-history"));
      list.replaceChildren(textNode("p", "以下是历史任务线索；项目现状请重新核实。", "muted"));
      if (!page.items.length) list.append(textNode("p", "暂无任务经历。", "muted"));
      for (const item of page.items) {
        const card = document.createElement("article");
        card.className = "memory-card";
        card.append(textNode("div", item.request_excerpt, "memory-body"));
        const open = textNode("button", "查看来源与结果", "quiet");
        on(open, "click", async () => {
          try {
            const detail = await api(scoped(`/api/task-history/${item.run_id}`));
            card.append(textNode("pre", JSON.stringify({session_id: detail.session_id,
              user_message_id: detail.user_message_id, answer_message_id: detail.answer_message_id,
              run_outcome: detail.run_outcome, task_completion: detail.task_completion,
              task_state: detail.task_state, request: detail.request_detail,
              answer: detail.answer_detail}, null, 2)));
            open.remove();
          } catch (error) { memoryNotice(error.message, true); }
        });
        card.append(open);
        list.append(card);
      }
      list.hidden = false;
    } catch (error) { memoryNotice(error.message, true); }
  }
  on($("task-history-button"), "click", () => loadTaskHistory());

  return {loadMemories, loadTaskHistory};
}
