/* The closing record of a finished run: what changed, what left the container.
 *
 * The live column shows these as they happen and is cleared when the run ends,
 * so a run that finishes before the next animation frame loses its last notice.
 * This block is built from the run row instead of the event stream, which makes
 * it the version that survives a refresh — the one a reader can check later.
 */

import { el, textNode } from "../core/dom.js";
import { diffSummary, networkSummary } from "../core/format.js";

export function runSummaryView(run) {
  const lines = [diffSummary(run.file_diff), networkSummary(run.network_observation)].filter(Boolean);
  if (!lines.length) return null;
  const block = el("section", "run-summary");
  block.append(textNode("h2", "本次运行", "run-summary-title"));
  const list = el("ul", "run-summary-facts");
  for (const line of lines) list.append(textNode("li", line));
  block.append(list);
  return block;
}
