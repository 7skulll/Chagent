/* Recent writes to the open workspace.
 *
 * The agent's tools and the file panel both record there, so a change the user
 * did not make is attributable instead of appearing by itself. It is a record,
 * not a live view: fetched when the pane is opened, never polled.
 */

import { $, textNode } from "../core/dom.js";
import { api } from "../core/api.js";

// Verbs stay two characters because the icon slot is 34px wide.
const OPERATION_LABELS = {
  write_file: "写入", edit_file: "编辑", apply_patch: "补丁", create_directory: "目录",
  save_text: "保存", create: "新建", rename: "改名", recycle: "删除", restore: "恢复",
  output: "输出", "confirmed-output": "确认",
};

function operationRow(item) {
  const row = document.createElement("div");
  row.className = "generated-file";
  const icon = textNode("span", OPERATION_LABELS[item.operation] || item.operation.slice(0, 2), "generated-file-icon");
  const info = document.createElement("div");
  info.className = "generated-file-info";
  const who = item.source === "agent" ? "Agent" : item.source === "manual" ? "文件面板" : item.source;
  const when = new Date(item.created_at).toLocaleString("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false});
  info.append(textNode("span", item.path, "generated-file-name"),
    textNode("span", `${who} · ${when}`, "generated-file-size"));
  info.title = `${item.path} · ${who} · ${new Date(item.created_at).toLocaleString("zh-CN", {hour12: false})}`;
  row.append(icon, info);
  return row;
}

export function createOperationsView(ui) {
  const state = ui.state;

  async function loadOperations() {
    const workspace = state.currentWorkspace, list = $("operations-list");
    if (!workspace?.id) {
      $("operations-count").textContent = "0";
      list.replaceChildren(textNode("p", "选择工作区后查看。", "artifacts-empty"));
      return;
    }
    const result = await api(`/api/workspaces/${workspace.id}/operations?limit=30`);
    if (state.currentWorkspace?.id !== workspace.id) return;
    const items = result.items || [];
    $("operations-count").textContent = String(items.length);
    list.replaceChildren(...items.map(operationRow));
    if (!items.length) list.append(textNode("p", "还没有改动记录。", "artifacts-empty"));
  }

  return {loadOperations};
}
