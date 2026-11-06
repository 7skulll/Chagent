/* The only renderer for the live area: `render(items, state)` in, DOM out.
 *
 * It reuses one node per item id and never reorders or re-inserts what is
 * already on screen, so painting the same state twice is a no-op and a pending
 * approval card keeps its buttons while a tool row above it updates.
 *
 * Repaint is coalesced per animation frame: a 3 000-token answer would
 * otherwise re-lay out the whole paragraph thousands of times. `flush` is the
 * same draw without the wait, so tests do not depend on frame timing.
 */

import { el, textNode } from "../core/dom.js";
import { renderMarkdown } from "../core/markdown.js";
import { approvalCard } from "../render/approval.js";
import { liveAnswerNode } from "../render/message.js";

export function createRunRenderer({root, status, decide, afterPaint}) {
  //: item id -> {kind, node, content?, state?}
  const drawn = new Map();
  let frame = 0;
  let pending = null;
  let lastStatus = null;

  function removeNode(id) {
    const entry = drawn.get(id);
    if (!entry) return;
    entry.node.remove();
    drawn.delete(id);
  }

  function textItem(item) {
    let entry = drawn.get(item.id);
    if (!entry) {
      const {node, content} = liveAnswerNode();
      root.append(node);
      entry = {kind: "text", node, content};
      drawn.set(item.id, entry);
    }
    entry.content.innerHTML = renderMarkdown(item.text);
    return entry;
  }

  function toolItem(item) {
    let entry = drawn.get(item.id);
    if (!entry) {
      const row = el("div", "tool-status");
      const state = textNode("span", "", "tool-state");
      row.append(textNode("code", item.name, "tool-name"), state);
      root.append(row);
      entry = {kind: "tool", node: row, state};
      drawn.set(item.id, entry);
    }
    entry.state.textContent = item.state;
    entry.node.classList.toggle("failed", Boolean(item.failed));
    return entry;
  }

  function noticeItem(item) {
    let entry = drawn.get(item.id);
    if (!entry) {
      const row = el("div", "tool-status");
      row.textContent = item.text;
      root.append(row);
      entry = {kind: "notice", node: row};
      drawn.set(item.id, entry);
    }
    entry.node.classList.toggle("failed", Boolean(item.failed));
    return entry;
  }

  function approvalItem(item) {
    let entry = drawn.get(item.id);
    const signature = `${item.state}:${item.request ? item.request.id : ""}`;
    if (!entry || entry.signature !== signature) {
      const card = approvalCard(item, decide);
      // A state change (loading -> pending) replaces the card rather than
      // appending a second one. It is the newest row while it is pending: the
      // run cannot emit another item between asking and answering.
      if (entry) entry.node.remove();
      root.append(card);
      entry = {kind: "approval", node: card, signature};
      drawn.set(item.id, entry);
    }
    return entry;
  }

  function draw(state) {
    const live = new Set();
    for (const item of state.items) {
      live.add(item.id);
      if (item.kind === "text") textItem(item);
      else if (item.kind === "tool") toolItem(item);
      else if (item.kind === "notice") noticeItem(item);
      else if (item.kind === "approval") approvalItem(item);
    }
    // Only an approval card can disappear; text, tools and notices are history
    // of this run and stay where they appeared.
    for (const id of [...drawn.keys()]) if (!live.has(id)) removeNode(id);
    // A run state with no status is not "idle"; it means this renderer has
    // nothing to say about the status line, which other code owns right now.
    if (status && state.status && state.status !== lastStatus) {
      lastStatus = state.status;
      status.textContent = state.status;
    }
    if (afterPaint) afterPaint(state);
  }

  return {
    flush(state) {
      pending = null;
      draw(state);
    },
    schedule(state) {
      pending = state;
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (pending) draw(pending);
        pending = null;
      });
    },
    reset() {
      if (frame) { cancelAnimationFrame(frame); frame = 0; }
      pending = null;
      drawn.clear();
      lastStatus = null;
      root.replaceChildren();
    },
  };
}
