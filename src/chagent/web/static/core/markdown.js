/* Load-failure fallback for the Markdown renderer.
 *
 * A single accessor instead of a global read at each call site: if markdown.js
 * did not load, the answer is still shown, just as preformatted text.
 *
 * Why a wrapper instead of `export default` in markdown.js: that file has two
 * other loaders that must keep working — `<script>` in the page, and
 * `require()` in tests/test_markdown_render.py. A file that is simultaneously a
 * classic script and an ES module would have to duplicate itself. The page
 * loads it as a classic script first (see index.html), so by the time any
 * module evaluates, the global exists.
 */

const escape = text => {
  const node = document.createElement("span");
  node.textContent = String(text == null ? "" : text);
  return node.innerHTML;
};

export const markdown = globalThis.ChagentMarkdown || {
  renderMarkdown: text => `<p>${escape(text)}</p>`,
  escapeHtml: escape,
};

export const renderMarkdown = text => markdown.renderMarkdown(text);
