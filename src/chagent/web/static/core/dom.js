/* The only module that talks to the document directly.
 *
 * Two helpers, no state, no knowledge of the conversation: everything above
 * this layer is expected to build nodes through here so that "which module
 * touches the DOM" stays a question with one answer.
 */

export const $ = id => document.getElementById(id);

export function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}
