/* Fetch helpers shared by the page.
 *
 * Deliberately unaware of the page state: the workspace query string is added
 * by the caller (`withWorkspace` in app.js), because that is a policy about
 * which workspace this tab may act in, not a property of HTTP.
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
