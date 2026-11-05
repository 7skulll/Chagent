/* The 依据 card: where a run's web results, document locators and injected
 * memories are collected and drawn.
 *
 * Sources travel in the tool result's details, so the transcript can rebuild
 * them without another request. Memory is the exception: it is not a message,
 * so the session loads its run→memory map once and passes it in.
 */

import { el, textNode } from "../core/dom.js";
import { hostOf, locatorLabel } from "../core/format.js";

export function collectSources(buckets, runId, message) {
  if (message.role !== "toolResult" || !runId) return;
  const details = message.details;
  if (!details || typeof details !== "object") return;
  let bucket = buckets.get(runId);
  if (!bucket) { bucket = {web: [], docs: []}; buckets.set(runId, bucket); }
  if (message.tool_name === "web_search" && Array.isArray(details.results)) {
    for (const row of details.results) {
      if (!row || typeof row.url !== "string" || bucket.web.some(item => item.url === row.url)) continue;
      // site_name is the provider's own label and is better than a parsed host.
      bucket.web.push({title: row.title || row.url, url: row.url, snippet: row.snippet || "",
                       site: row.site_name || ""});
    }
  } else if (message.tool_name === "web_fetch" && typeof details.url === "string") {
    // A failed fetch never reaches here: it is an error result with no details.
    if (!bucket.web.some(item => item.url === details.url)) {
      bucket.web.push({title: details.title || details.url, url: details.url, snippet: "",
                       site: hostOf(details.url)});
    }
  } else if (message.tool_name === "read_file" && details.format === "document"
             && typeof details.path === "string") {
    const citations = Array.isArray(details.citations) ? details.citations : [];
    const existing = bucket.docs.find(item => item.path === details.path);
    if (existing) existing.citations.push(...citations);
    else bucket.docs.push({path: details.path, citations});
  }
}

export function appendSources(node, buckets, runId, memories) {
  const bucket = buckets.get(runId) || {web: [], docs: []};
  const total = bucket.web.length + bucket.docs.length + memories.length;
  // No sources, no card: an empty 依据 would be a promise with nothing behind it.
  if (!total) return;
  const details = el("details", "sources");
  details.append(textNode("summary", `依据 · ${total} 处`));
  for (const item of bucket.web) {
    const card = el("div", "source-card");
    const link = document.createElement("a");
    link.href = item.url; link.target = "_blank"; link.rel = "noopener noreferrer";
    link.textContent = item.title;
    card.append(link, textNode("span", item.site || hostOf(item.url), "source-host"));
    if (item.snippet) card.append(textNode("p", item.snippet, "source-snippet"));
    details.append(card);
  }
  for (const doc of bucket.docs) {
    const row = el("div", "source-doc");
    row.append(textNode("code", doc.path), textNode("span", locatorLabel(doc.citations), "source-pages"));
    details.append(row);
  }
  for (const memory of memories) {
    const row = el("div", "source-memory");
    row.append(textNode("strong", `${memory.key} v${memory.revision}`),
      textNode("span", memory.scope === "user" ? "用户偏好" : "工作区事实", "source-host"));
    row.append(textNode("p", memory.content, "source-snippet"));
    details.append(row);
  }
  node.append(details);
}

// Only the last assistant article of a run carries its 依据: a run may answer in
// several model turns, and repeating the same list under each would be noise.
export function attachSources(nodes, buckets, runMemories) {
  const last = new Map();
  for (const node of nodes) {
    if (!node || !node.classList || !node.classList.contains("assistant") || !node.dataset.run) continue;
    last.set(node.dataset.run, node);
  }
  for (const [runId, node] of last) appendSources(node, buckets, runId, runMemories.get(runId) || []);
}
