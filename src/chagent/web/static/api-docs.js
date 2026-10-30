"use strict";

// Self-contained local reference: no CDN scripts, fonts or automatic API calls.
function item(tag, text) { const node = document.createElement(tag); node.textContent = text; return node; }

// The surface is 26 operations over a handful of resources; a flat list of
// identical boxes hides that shape. Order is fixed so the page reads the same
// every time the server restarts.
const GROUPS = [
  ["服务与工作区", ["/api/info", "/api/workspaces", "/api/config"]],
  ["会话与上传", ["/api/sessions", "/api/uploads"]],
  ["运行与审批", ["/api/runs", "/api/approvals"]],
  ["长期记忆", ["/api/memories"]],
  ["用量", ["/api/usage"]],
];

function resourceOf(path) {
  const parts = path.split("/").filter(Boolean);
  // /api/sessions/{id}/usage belongs to sessions, not to a usage resource.
  return `/${parts.slice(0, 2).join("/")}`;
}

function groupOf(path) {
  const resource = resourceOf(path);
  for (const [name, prefixes] of GROUPS) if (prefixes.some(prefix => resource === prefix || resource.startsWith(`${prefix}/`))) return name;
  return "其他";
}

function operationRow(path, method, operation) {
  const row = document.createElement("details"); row.className = "api-row";
  const summary = document.createElement("summary");
  const badge = item("span", method.toUpperCase()); badge.className = `api-method api-${method}`;
  const pathEl = item("span", path); pathEl.className = "api-path";
  summary.append(badge, pathEl);
  if (operation.summary) { const name = item("span", operation.summary); name.className = "api-name"; summary.append(name); }
  row.append(summary);
  if (operation.description) row.append(item("p", operation.description));
  row.append(item("pre", JSON.stringify({parameters: operation.parameters || [], requestBody: operation.requestBody || null,
    responses: operation.responses}, null, 2)));
  return row;
}

async function loadReference() {
  const response = await fetch("/openapi.json");
  if (!response.ok) throw new Error("无法读取本地 OpenAPI 定义");
  const schema = await response.json();
  const target = document.getElementById("api-reference"); target.replaceChildren();
  const intro = item("p", schema.info.description); intro.className = "api-intro"; target.append(intro);

  const sections = new Map();
  for (const [path, operations] of Object.entries(schema.paths)) {
    for (const [method, operation] of Object.entries(operations)) {
      if (!["get", "post", "patch", "delete", "put"].includes(method)) continue;
      const name = groupOf(path);
      if (!sections.has(name)) sections.set(name, []);
      sections.get(name).push([path, method, operation]);
    }
  }
  const order = [...GROUPS.map(([name]) => name), "其他"];
  for (const name of order) {
    const rows = sections.get(name);
    if (!rows || !rows.length) continue;
    const heading = item("h2", name); heading.className = "api-group";
    const list = document.createElement("div"); list.className = "api-list";
    for (const [path, method, operation] of rows) list.append(operationRow(path, method, operation));
    target.append(heading, list);
  }

  const models = document.createElement("details"); models.className = "api-row";
  const modelsList = document.createElement("div"); modelsList.className = "api-list";
  models.append(item("summary", "请求与响应数据模型"), item("pre", JSON.stringify(schema.components.schemas, null, 2)));
  modelsList.append(models); target.append(modelsList);
}
loadReference().catch(error => { document.getElementById("api-reference").textContent = error.message; });
