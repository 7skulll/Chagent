"use strict";

// Self-contained local reference: no CDN scripts, fonts or automatic API calls.
function item(tag, text) { const node = document.createElement(tag); node.textContent = text; return node; }
async function loadReference() {
  const response = await fetch("/openapi.json");
  if (!response.ok) throw new Error("无法读取本地 OpenAPI 定义");
  const schema = await response.json();
  const target = document.getElementById("api-reference"); target.replaceChildren();
  target.append(item("p", schema.info.description));
  for (const [path, operations] of Object.entries(schema.paths)) {
    for (const [method, operation] of Object.entries(operations)) {
      if (!["get", "post", "patch", "delete", "put"].includes(method)) continue;
      const row = document.createElement("details"); row.className = "memory-card";
      row.append(item("summary", `${method.toUpperCase()} ${path} · ${operation.summary || ""}`));
      if (operation.description) row.append(item("p", operation.description));
      row.append(item("pre", JSON.stringify({parameters: operation.parameters || [], requestBody: operation.requestBody || null,
        responses: operation.responses}, null, 2)));
      target.append(row);
    }
  }
  const models = document.createElement("details"); models.className = "memory-card";
  models.append(item("summary", "请求与响应数据模型"), item("pre", JSON.stringify(schema.components.schemas, null, 2)));
  target.append(models);
}
loadReference().catch(error => { document.getElementById("api-reference").textContent = error.message; });
