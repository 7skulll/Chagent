/* One transcript message, including its tool results and action row.
 *
 * This is the single place a stored message becomes markup. The live answer
 * paragraph during streaming goes through `liveAnswerNode` + `contentNode` from
 * here as well, so "what the reader watched appear" and "what they see after a
 * refresh" are produced by the same code instead of two look-alike blocks.
 *
 * Everything that is not DOM arrives through `ctx`: the page state, the API
 * calls a button makes, and the file panel openers. That keeps this module
 * below the view layer — it cannot import a view, and a missing action fails at
 * the click instead of being skipped by an optional chain.
 */

import { el, textNode } from "../core/dom.js";
import { api } from "../core/api.js";
import { copyText } from "../core/clipboard.js";
import { plainText } from "../core/format.js";
import { renderMarkdown } from "../core/markdown.js";
import { fileRow } from "./files.js";
import { collectSources, attachSources } from "./sources.js";

//: Answers longer than this open folded to their first eight lines.
export const CLAMP_CHARS = 2000;

/* Escaping happens inside markdown.js, before any structure is recognised, so
 * this is never fed raw model output as markup. */
export function contentNode(text, prose) {
  const node = el("div");
  if (prose) { node.className = "content md"; node.innerHTML = renderMarkdown(text); }
  else { node.className = "content"; node.textContent = text; }
  return node;
}

/* An answer over CLAMP_CHARS is read in two passes: first eight lines, then the
 * whole thing. The button sits outside the clipped box, so it stays visible. */
export function clampContent(node, text) {
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

/* The paragraph a running answer is painted into. Same article shape and the
 * same Markdown call as `displayMessage`, so the streamed text and the stored
 * text cannot drift; only the action row and the fold are history-only.
 * Exported outside the factory because it needs no page state. */
export function liveAnswerNode() {
  const node = el("article", "message assistant");
  node.append(textNode("div", "Chagent", "role sr-only"));
  const content = el("div", "content md");
  node.append(content);
  return {node, content};
}

export function createMessageRenderer(ctx) {
  const copyAnswer = (text, button) => copyText(text, button, ctx.showError);

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

  function startEdit(node, message, meta) {
    if (node.querySelector(".message-edit")) return;
    const content = node.querySelector(".content");
    const form = el("div", "message-edit");
    const area = document.createElement("textarea");
    area.value = plainText(message);
    area.maxLength = 16000;
    area.setAttribute("aria-label", "编辑这条消息后重发");
    const actions = el("div", "message-edit-actions");
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
      ctx.requestRun({text, edit_message_id: meta.id});
    });
    area.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); close(); }
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send.click(); }
    });
  }

  function messageActions(message, meta, node) {
    const row = el("div", "message-actions");
    const copy = textNode("button", "复制");
    copy.type = "button";
    copy.addEventListener("click", () => copyAnswer(plainText(message), copy));
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
    const state = ctx.state;
    const actionable = meta.id && meta.run_id && !meta.superseded_by && !state.readonly && !state.busy;
    if (message.role === "assistant" && actionable) {
      const again = textNode("button", "重新生成");
      again.type = "button";
      again.addEventListener("click", () => ctx.requestRun({regenerate_from: meta.id}));
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

  function artifactDetails(message, node) {
    const details = el("details");
    const summary = el("summary");
    summary.append(textNode("code", message.tool_name, "tool-name"),
      textNode("span", message.is_error ? "失败" : "完成", message.is_error ? "tool-state failed" : "tool-state"));
    if (message.artifact) summary.append(textNode("span", `产物 ${message.artifact.char_count} 字符`, "tool-note"));
    details.append(summary);
    for (const child of Array.from(node.children).slice(1)) {
      if (!child.classList.contains("message-file")) details.append(child);
    }
    if (message.artifact) {
      let offset = 0;
      const button = textNode("button", "读取产物全文（分页）", "quiet");
      const output = textNode("pre", "");
      const session = ctx.state.session;
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const page = await api(`/api/sessions/${session}/artifacts/${message.artifact.id}?offset=${offset}`);
          output.append(document.createTextNode(page.text));
          offset = page.next_offset;
          button.hidden = offset === null;
          button.textContent = `继续读取（从字符 ${offset}）`;
        } catch (error) { ctx.showError(error); }
        finally { button.disabled = false; }
      });
      details.insertBefore(button, details.children[1] || null);
      if (message.artifact.source_truncated) {
        details.append(textNode("p", "来源输出已被上游截断，产物保留全部已捕获内容。"));
      }
      details.append(output);
    }
    node.append(details);
  }

  function displayMessage(message, target, meta = {}, live = false) {
    if (message.role === "system") return null;
    const node = el("article", `message ${message.role === "user" ? "user" : "assistant"}`);
    if (meta.id) node.dataset.messageId = meta.id;
    if (meta.run_id) node.dataset.run = meta.run_id;
    collectSources(ctx.state.sources, meta.run_id, message);
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
        const details = el("details");
        const summary = el("summary");
        summary.append(textNode("code", block.name, "tool-name"), textNode("span", "调用", "tool-state"));
        details.append(summary, textNode("pre", JSON.stringify(block.arguments, null, 2)));
        node.append(details);
      }
    }
    if (message.role === "toolResult" && message.details?.workspace_files && !message.is_error) {
      for (const file of message.details.workspace_files) node.append(fileRow(file, ctx.state.session, ctx, true));
    }
    if (message.role === "toolResult" && message.details?.workspace_file && !message.is_error) {
      node.append(fileRow(message.details.workspace_file, ctx.state.session, ctx, true));
    }
    if (message.role === "toolResult" && message.tool_name === "create_file"
        && message.details && message.details.generated_file && !message.is_error) {
      node.append(fileRow(message.details.generated_file, ctx.state.session, ctx, true));
    }
    if (message.role === "toolResult") artifactDetails(message, node);
    if (message.error_message) {
      node.append(textNode("div", message.error_message, "tool-status failed"));
      // A failed turn keeps its question in the transcript, so retrying is the
      // same request as regenerating that answer — no new user message is written.
      if (meta.id && meta.run_id && !meta.superseded_by && !ctx.state.readonly) {
        const retry = textNode("button", "重试", "quiet");
        retry.type = "button";
        retry.addEventListener("click", () => ctx.requestRun({regenerate_from: meta.id}));
        node.append(retry);
      }
    }
    if (!live) node.append(messageActions(message, meta, node));
    // Thinking is internal; only response text and tool records are displayed.
    if (!live && !node.querySelector(".content, details, .tool-status")) return null;
    if (meta.superseded_by) {
      // Kept, not deleted: the old answer is what the reader compares against.
      const fold = el("details", "superseded-fold");
      fold.append(textNode("summary", message.role === "user" ? "已编辑的原文" : "被重新生成的回答"));
      fold.append(node);
      target.append(fold);
      return fold;
    }
    target.append(node);
    return node;
  }

  const attach = nodes => attachSources(nodes, ctx.state.sources, ctx.state.runMemories);

  return {displayMessage, attachSources: attach};
}
