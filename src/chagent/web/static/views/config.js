/* Runtime configuration and the seven-day usage summary.
 *
 * These are the same facts the CLI prints as `Behavior:`, read from the same
 * endpoint, so a start-up banner, `/api/config` and this dialog cannot disagree.
 */

import { $, on, textNode } from "../core/dom.js";
import { api } from "../core/api.js";
import { CONFIG_LABELS, configValue, formatRate, formatTokens } from "../core/format.js";
import { usageTable } from "../render/usage.js";

export function createConfigView(ui) {
  async function loadConfig() {
    const workspaceId = ui.state.currentWorkspace?.id;
    const config = await api(`/api/config${workspaceId ? `?workspace_id=${workspaceId}` : ""}`);
    const target = $("config-list"); target.replaceChildren();
    for (const [key, label] of CONFIG_LABELS) {
      target.append(textNode("dt", label), textNode("dd", configValue(key, config)));
    }
    target.append(textNode("dd", config.behavior, "config-behavior"));
  }

  async function loadConfigUsage() {
    const target = $("config-usage"); target.replaceChildren();
    const usage = await api("/api/usage?days=7");
    if (!usage.items.length) { target.append(textNode("p", "近 7 天没有模型调用。")); return; }
    target.append(textNode("p", `近 7 天 ${formatTokens(usage.totals.total_tokens)} tokens，` +
      `${usage.totals.runs} 次运行，缓存命中 ${formatRate(usage.totals.cache_hit_rate)}`));
    // A day is a row: three figures that are compared, not three facts in a sentence.
    const rows = usage.items.slice(0, 7).map(day => ({...day}));
    target.append(usageTable(rows, [["day", "日期"], ["model_calls", "调用"], ["total_tokens", "合计"]]));
  }

  on($("open-config"), "click", () => {
    const notice = $("config-notice");
    notice.textContent = ""; notice.classList.remove("failed");
    $("config-dialog").showModal();
    loadConfig().catch(error => { notice.textContent = error.message; notice.classList.add("failed"); });
    loadConfigUsage().catch(error => { notice.textContent = error.message; notice.classList.add("failed"); });
  });
  on($("close-config"), "click", () => $("config-dialog").close());

  return {loadConfig, loadConfigUsage};
}
