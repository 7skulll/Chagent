/* The one table shape the page uses for numbers over time.
 *
 * Two dialogs show token counts and both are read by comparing rows, so a table
 * is the right markup in both — and one implementation keeps the column order
 * and the number formatting identical.
 */

import { el, textNode } from "../core/dom.js";
import { formatTokens } from "../core/format.js";

const VERBATIM = new Set(["started_at", "day"]);

export function usageTable(rows, columns) {
  const table = el("table", "usage-table");
  const head = el("tr");
  for (const [, label] of columns) head.append(textNode("th", label));
  table.append(head);
  for (const row of rows) {
    const line = el("tr");
    for (const [key] of columns) line.append(el("td"));
    columns.forEach(([key], index) => {
      const value = row[key];
      line.children[index].textContent = VERBATIM.has(key) ? value
        : key === "model_calls" ? String(value) : formatTokens(value);
    });
    table.append(line);
  }
  return table;
}
