/* The context pane: what the last model request actually contained.
 *
 * Every figure comes from a stored snapshot, so this shows the builder's
 * decision rather than a re-computation that could disagree with it.
 */

import { $, el, on, textNode } from "../core/dom.js";
import { api } from "../core/api.js";

export function createContextView(ui) {
  const state = ui.state;

  async function loadContext() {
    const version = state.version, id = state.session;
    const target = $("context-info");
    if (!id) {
      target.replaceChildren(textNode("p", "选中一个会话后，这里显示它最近一次的上下文用量与摘要。", "context-note"));
      return;
    }
    const value = await api(`/api/sessions/${id}/context`);
    if (version !== state.version) return;
    target.replaceChildren();
    const latest = value.snapshots[0]?.snapshot;
    if (latest) {
      const selected = latest.selections.filter(item => item.selected).length;
      const summarized = latest.selections.filter(item => item.reason === "summarized").length;
      const excluded = latest.selections.length - selected - summarized;
      const memories = latest.memory_references || [];
      // Figures in pairs, not a paragraph: this pane is scanned for one number.
      const facts = [
        ["估算 token", `${latest.token_estimate} / ${latest.input_budget}`],
        ["保留消息", `${selected} 条`],
        ["摘要覆盖", `${summarized} 条`],
        ["未纳入", `${excluded} 条`],
        ["计数方式", latest.token_counter],
        ["摘要版本", latest.summary_revision ?? "无"],
        ["长期记忆", memories.length
          ? `${memories.length} 条，${latest.memory_token_estimate} 估算 token` : "本轮未注入"],
      ];
      const factsList = el("dl", "context-facts");
      for (const [label, value] of facts) factsList.append(textNode("dt", label), textNode("dd", value));
      target.append(factsList);
      if (memories.length) {
        target.append(textNode("p", memories.map(item => `${item.key} v${item.revision}`).join("、"), "context-note"));
      }
      if (latest.memory_omitted) {
        target.append(textNode("p", `另有 ${latest.memory_omitted} 条检索结果未纳入记忆预算。`, "context-note"));
      }
    } else target.append(textNode("p", "尚无模型请求快照。", "context-note"));
    if (!value.summaries.length) {
      target.append(textNode("p",
        "尚未生成会话摘要。达到预算阈值后会自动压缩旧历史或运行内已完成的轮次。", "context-note"));
    }
    for (const summary of value.summaries) {
      const row = el("details");
      const scope = summary.source_run_id
        ? `（运行内，${summary.replayable ? summary.source_run_status === "running" ? "仅当前运行可用" : "可续接" : "已失效，保留审计"}）` : "";
      row.append(textNode("summary", `摘要第 ${summary.revision} 版，覆盖至消息序号 ${summary.covered_through_sequence}${scope}`));
      row.append(textNode("pre", summary.content));
      target.append(row);
    }
  }

  on($("context-panel"), "toggle", () => {
    if ($("context-panel").open) loadContext().catch(ui.showError);
  });

  return {loadContext};
}
