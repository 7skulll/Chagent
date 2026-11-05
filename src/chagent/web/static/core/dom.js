/* The only module that talks to the document directly.
 *
 * No state, no knowledge of the conversation: everything above this layer
 * builds nodes through here so that "which module touches the DOM" stays a
 * question with one answer.
 */

export const $ = id => document.getElementById(id);

export function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

/* Same as textNode, with the order flipped for the common create-then-fill case.
 * An element is built unfilled when `text` is omitted, which is what a form
 * control needs before it can be assigned a value. */
export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

export function on(node, type, handler) {
  node.addEventListener(type, handler);
  return node;
}

/* Replace a container's children in one step. Every list in the page is painted
 * by rebuilding it, and a half-painted list is never observable in between. */
export function clear(node, ...children) {
  node.replaceChildren(...children.filter(Boolean));
  return node;
}
