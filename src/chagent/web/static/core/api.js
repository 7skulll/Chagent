/* Fetch helpers shared by the page.
 *
 * Deliberately unaware of the page state: the workspace query string is added
 * by `withWorkspace(path, workspace)`, because which workspace a tab may act in
 * is a policy about this tab, not a property of HTTP. Callers pass the value.
 *
 * Every failure becomes an Error carrying the server's message, so callers can
 * `catch (error) { showError(error) }` without inspecting the response.
 */

export async function api(path, options = {}) {
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `请求失败 (${response.status})`);
  return value;
}

export const post = (path, value) =>
  api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});

export const patch = (path, value) =>
  api(path, {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});

export const send = (path, method, value) =>
  api(path, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});

/* Every workspace-scoped call names its workspace; the server refuses ones that
 * were never opened, so a stale tab cannot act in a directory it did not pick. */
export function withWorkspace(path, workspace) {
  if (!workspace) return path;
  return `${path}${path.includes("?") ? "&" : "?"}workspace=${encodeURIComponent(workspace)}`;
}
