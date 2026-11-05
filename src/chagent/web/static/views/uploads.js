/* The attachment queue.
 *
 * The file body is posted as-is: the endpoint takes raw bytes with any content
 * type, so a 20 MiB report never becomes a base64 string inside JSON. XHR is
 * used instead of fetch for one reason: upload progress events.
 *
 * Paths ride in the composer instead of being sent behind the user's back: they
 * can edit or drop the sentence before pressing send.
 */

import { $, el, on, textNode } from "../core/dom.js";
import { api, post, withWorkspace } from "../core/api.js";
import { IMAGE, MAX_UPLOAD_BYTES, attachmentPrompt, formatSize, readableName, uploadStatusLabel } from "../core/format.js";

export function createUploadsView(ui) {
  const state = ui.state;
  const queue = state.uploads;

  const uploadPaths = () => queue.items
    .filter(item => item.status === "done" && item.readable)
    .map(item => item.path);

  // The prefix is tracked so removing a file rewrites the line instead of
  // appending a second one.
  function applyUploadPrompt() {
    const input = $("input");
    const paths = uploadPaths();
    const previous = state.promptPrefix;
    if (previous && input.value.startsWith(previous)) input.value = input.value.slice(previous.length);
    state.promptPrefix = attachmentPrompt(paths);
    if (state.promptPrefix) {
      const rest = input.value.replace(/^[：:]\s*/, "");
      input.value = rest ? `${state.promptPrefix}：${rest}` : `${state.promptPrefix}：`;
    }
    // A run in flight owns the status line; the queue does not talk over it.
    if (!state.busy) {
      $("status").textContent = paths.length ? `已上传 ${paths.length} 个文件` : "就绪";
    }
  }

  async function removeItem(item) {
    if (item.status === "pending" && item.outputId) {
      try {
        const output = await api(`/api/workspaces/${item.workspaceId}/outputs/${item.outputId}`);
        if (output.status === "pending") {
          await post(`/api/workspaces/${item.workspaceId}/outputs/${item.outputId}/decision`,
            {token: output.token, choice: "cancel"});
        }
        await ui.refreshFiles();
      } catch (error) { ui.showError(error); return; }
    }
    if (item.request) { try { item.request.abort(); } catch { /* already finished */ } }
    queue.items = queue.items.filter(other => other !== item);
    renderUploads(); applyUploadPrompt();
  }

  function itemView(item) {
    const row = el("div", "upload-row");
    const thumb = el("div", "upload-thumb");
    if (item.preview) {
      const image = document.createElement("img");
      image.src = item.preview; image.alt = "";
      thumb.append(image);
    } else {
      thumb.textContent = (item.name.split(".").pop() || "file").slice(0, 4);
    }
    const facts = el("div", "upload-facts");
    facts.append(textNode("div", item.name, "upload-name"));
    const meta = el("div", "upload-meta", uploadStatusLabel(item));
    meta.classList.toggle("failed", item.status === "failed");
    facts.append(meta);
    if (item.status === "uploading") {
      // A <progress> element, not a styled div: the page is served under
      // `style-src 'self'`, and a value is markup rather than an inline style.
      const bar = el("progress", "upload-bar");
      bar.max = 1; bar.value = item.progress;
      facts.append(bar);
    }
    if (item.note) facts.append(textNode("div", item.note, "upload-meta"));
    if (item.status === "done" && item.workspaceId && item.path) {
      const open = textNode("button", "打开文件", "upload-meta"); open.type = "button";
      open.addEventListener("click", () => ui.openFile(item.workspaceId, item.path).catch(ui.showError));
      const download = textNode("a", "下载", "upload-meta");
      download.href = `/api/workspaces/${item.workspaceId}/content?${new URLSearchParams({path: item.path})}`;
      download.download = item.name;
      facts.append(open, download);
    }
    row.append(thumb, facts);
    if (item.status === "failed" && item.retryable) {
      const retry = textNode("button", "↻", "upload-retry");
      retry.type = "button";
      retry.title = "重试上传";
      retry.setAttribute("aria-label", `重试上传 ${item.name}`);
      retry.addEventListener("click", () => {
        item.status = "queued"; item.progress = 0; item.error = "";
        renderUploads(); runUploads();
      });
      row.append(retry);
    }
    const remove = textNode("button", "×", "upload-remove");
    remove.type = "button"; remove.title = "移除"; remove.setAttribute("aria-label", `移除 ${item.name}`);
    remove.addEventListener("click", () => removeItem(item));
    row.append(remove);
    return row;
  }

  function renderUploads() {
    $("uploads").replaceChildren(...queue.items.map(itemView));
    ui.updateComposer();
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
      request.open("POST", withWorkspace(
        `/api/uploads?name=${encodeURIComponent(item.name)}&request_id=${item.requestId}`, state.workspace));
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
            ui.showFiles();
          }
          ui.refreshFiles().catch(ui.showError);
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
    if (queue.running) return;
    queue.running = true;
    try {
      while (true) {
        const next = queue.items.find(item => item.status === "queued");
        if (!next) break;
        await uploadOne(next);
        renderUploads();
        applyUploadPrompt();
        ui.resizeComposer(); ui.updateComposer();
      }
    } finally { queue.running = false; }
  }

  function enqueueFiles(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    // An archived session refuses new runs, so a queue there would only produce
    // a prompt that cannot be sent.
    if (state.readonly) { ui.showError(new Error("该会话已归档，恢复后才能上传")); return; }
    ui.clearError();
    for (const file of list) {
      const item = {id: queue.nextId++, file, name: file.name || "upload", size: file.size,
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
      queue.items.push(item);
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

  /* A decision made in the file panel is about a row in this queue: the output
   * it was waiting for now has a state, and the upload keeps it. */
  function outputChanged(output) {
    for (const item of queue.items) {
      if (item.outputId !== output.id) continue;
      item.status = output.status === "committed" ? "done" : "failed";
      item.path = output.output_path || item.path;
      item.note = output.status === "committed" ? "文件已保存到工作区" : "输出已取消或超时";
    }
    renderUploads(); applyUploadPrompt();
    ui.loadGeneratedFiles().catch(ui.showError);
  }

  // Polls what the file panel could not resolve on its own: an output decided
  // while this tab was in the background.
  async function checkPendingUploads() {
    for (const item of queue.items.filter(entry => entry.status === "pending" && entry.outputId)) {
      const output = await api(`/api/workspaces/${item.workspaceId}/outputs/${item.outputId}`);
      if (["committed", "cancelled", "expired", "failed"].includes(output.status)) outputChanged(output);
    }
  }

  on($("attach"), "click", () => $("file-input").click());
  on($("file-input"), "change", event => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (files.length) enqueueFiles(files);
  });
  on($("input"), "paste", event => {
    const files = Array.from((event.clipboardData && event.clipboardData.files) || []);
    if (!files.length) return;
    // A pasted screenshot is a file, not text: it goes to the queue instead of
    // becoming a base64 blob in the message body.
    event.preventDefault();
    enqueueFiles(files);
  });

  /* Drag and drop over the conversation column. */
  const chat = document.querySelector(".chat");
  let dragDepth = 0;
  const draggingFiles = event => Array.from((event.dataTransfer && event.dataTransfer.types) || []).includes("Files");
  on(chat, "dragenter", event => {
    if (!draggingFiles(event)) return;
    event.preventDefault(); dragDepth += 1; chat.classList.add("drop-target");
  });
  on(chat, "dragover", event => {
    if (!draggingFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  });
  on(chat, "dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) chat.classList.remove("drop-target");
  });
  on(chat, "drop", event => {
    if (!draggingFiles(event)) return;
    event.preventDefault();
    dragDepth = 0; chat.classList.remove("drop-target");
    enqueueFiles(event.dataTransfer ? event.dataTransfer.files : null);
  });

  return {renderUploads, applyUploadPrompt, enqueueFiles, outputChanged, checkPendingUploads};
}
