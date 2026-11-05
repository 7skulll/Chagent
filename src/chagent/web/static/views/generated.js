/* The generated-file sidebar.
 *
 * Only files actually created by the model belong here; an uploaded input shows
 * up in the upload queue instead, and the server's list is already filtered.
 */

import { $, textNode } from "../core/dom.js";
import { api } from "../core/api.js";
import { fileRow } from "../render/files.js";

export function createGeneratedView(ui) {
  const state = ui.state;

  async function loadGeneratedFiles() {
    const id = state.session, version = state.version, list = $("artifacts-list");
    if (!id) {
      $("artifacts-count").textContent = "0";
      list.replaceChildren(textNode("p", "选择会话后查看文件。", "artifacts-empty"));
      return;
    }
    const result = await api(`/api/sessions/${id}/files`);
    if (version !== state.version || id !== state.session) return;
    const items = result.items || [];
    $("artifacts-count").textContent = String(items.length);
    list.replaceChildren(...items.map(file => fileRow(file, id, ui)));
    if (!items.length) list.append(textNode("p", "生成文件后会显示在这里。", "artifacts-empty"));
    ui.refreshFiles().catch(ui.showError);
  }

  return {loadGeneratedFiles};
}
