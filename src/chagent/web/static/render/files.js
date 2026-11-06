/* Cards for files the model produced or the user uploaded.
 *
 * Only files actually created by the model belong in the artifact panel; an
 * uploaded input is a different thing and the server marks it as such, so the
 * caller decides which list a row joins.
 */

import { el, textNode } from "../core/dom.js";
import { api, post } from "../core/api.js";

export const fileUrl = (session, file) =>
  `/api/sessions/${encodeURIComponent(session)}/files/${encodeURIComponent(file.id)}`;

export const workspaceContentUrl = (workspaceId, path) =>
  `/api/workspaces/${workspaceId}/content?${new URLSearchParams({path})}`;

/* `ctx.openFile` opens a path in the file panel, `ctx.showFiles` reveals that
 * panel, `ctx.refreshFiles` reloads it. They arrive from the page entry point,
 * which is the only module allowed to know both the chat and the file panel. */
export function fileRow(file, session, ctx, inMessage = false) {
  const row = el("div", inMessage ? "generated-file message-file" : "generated-file");
  const icon = textNode("span", (file.name.split(".").pop() || "文件").toUpperCase().slice(0, 4), "generated-file-icon");
  const info = el("div", "generated-file-info");
  info.append(textNode("span", file.name, "generated-file-name"), textNode("span", `${file.size} 字节`, "generated-file-size"));
  const download = textNode("a", "下载", "generated-file-download");
  download.href = fileUrl(session, file); download.download = file.name;
  download.setAttribute("aria-label", `下载 ${file.name}`);
  if (file.workspace_id && file.path) {
    if (file.status === "committed") {
      download.href = workspaceContentUrl(file.workspace_id, file.path);
      const open = textNode("button", "打开文件", "generated-file-download"); open.type = "button";
      open.addEventListener("click", () => ctx.openFile(file.workspace_id, file.path).catch(ctx.showError));
      info.append(open);
    } else {
      download.removeAttribute("href"); download.textContent = "等待确认";
      const view = textNode("button", "查看输出状态", "generated-file-download"); view.type = "button";
      view.addEventListener("click", async () => {
        try {
          const output = await api(`/api/workspaces/${file.workspace_id}/outputs/${file.id}`);
          if (output.status === "committed") await ctx.openFile(file.workspace_id, output.output_path);
          else { ctx.showFiles(); await ctx.refreshFiles(); }
        } catch (error) { ctx.showError(error); }
      }); info.append(view);
    }
  } else {
    const save = textNode("button", "保存到工作区", "generated-file-download"); save.type = "button";
    save.addEventListener("click", async () => {
      try {
        await post(`/api/sessions/${session}/files/${file.id}/export`, {});
        ctx.showFiles(); await ctx.refreshFiles();
      } catch (error) { ctx.showError(error); }
    }); info.append(save);
  }
  row.append(icon, info, download);
  return row;
}
