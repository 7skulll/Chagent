/* The confirmation card for one pending question — or for one batch of them.
 *
 * A write is not a command: the card has to say what it would replace, not name
 * the tool and leave the user reading JSON. The three states are "still
 * fetching", "ask", and "the fetch failed" — the last one must not look like a
 * refusal, because nothing was decided.
 *
 * A batch is shown as one card listing every file, because it is answered as
 * one: the server applies the decision to all rows of the batch atomically, and
 * a card that showed only the first file would misdescribe what a click does.
 */

import { el, textNode } from "../core/dom.js";
import { approvalTitle } from "../core/format.js";

export function approvalCard(item, decide) {
  const row = el("div", "approval");
  row.id = "approval";
  if (item.state === "loading") {
    row.append(textNode("div", "需要确认：正在读取…", "approval-title"));
    return row;
  }
  if (item.state === "failed") {
    row.append(textNode("div", "需要确认：审批请求读取失败", "approval-title"));
    row.append(textNode("div", item.error || "刷新页面后再试。", "approval-note"));
    return row;
  }
  const request = item.request;
  const items = Array.isArray(request.items) && request.items.length ? request.items : [request];
  const batched = items.length > 1;
  row.append(textNode("div",
    batched ? `需要确认：${items.length} 处文件改动` : `需要确认：${approvalTitle(request.tool_name)}`,
    "approval-title"));
  if (batched) {
    const list = el("ul", "approval-items");
    for (const entry of items) {
      const line = el("li", "approval-item");
      line.append(textNode("code", entry.subject, "approval-command"));
      list.append(line);
    }
    row.append(list);
  } else {
    row.append(textNode("code", request.subject, "approval-command"));
  }
  row.append(textNode("div",
    batched
      ? `目录 ${request.cwd}，${new Date(request.expires_at).toLocaleTimeString()} 前有效；` +
        `批准会一次作用于这 ${items.length} 次调用，每次仍然独立核销`
      : `目录 ${request.cwd}，${new Date(request.expires_at).toLocaleTimeString()} 前有效，批准只对这一次调用生效`,
    "approval-note"));
  const actions = el("div", "approval-actions");
  const approve = textNode("button", batched ? `批准这 ${items.length} 处` : "批准这一次", "primary");
  const deny = textNode("button", "拒绝");
  const press = value => {
    // Both buttons disable together: a second press on the other one would race
    // the first decision, and only one of them can be consumed.
    approve.disabled = deny.disabled = true;
    decide(request.id, value);
  };
  approve.addEventListener("click", () => press(true));
  deny.addEventListener("click", () => press(false));
  actions.append(approve, deny);
  row.append(actions);
  return row;
}
