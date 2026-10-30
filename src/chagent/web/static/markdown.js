/* Markdown subset renderer for assistant answers (WK1).
 *
 * Escaping happens first, on the whole input: every `<`, `>`, `&` and quote
 * becomes an entity before any structure is recognised, so no input can emit a
 * tag that this file did not write. Structure is then matched against the
 * escaped text — a blockquote therefore arrives as `&gt; ` — and the only
 * markup that ever leaves this module is the set of templates below: h1–h4, p,
 * ul/ol/li, blockquote, pre/code, table, a, strong, em, code, hr.
 *
 * Links are limited to http(s) at the pattern level, so `javascript:` and
 * `data:` cannot become an href; everything else stays literal text. Inline
 * HTML is not supported and never will be: the answer is untrusted model
 * output, and there is no allow-list that makes an `<img onerror>` safe.
 *
 * The supported subset is what answers actually use: headings, lists, quotes,
 * tables, fenced code, inline code, links, bold and italic. No images, no
 * footnotes, no math, no reference-style links.
 */
(function (global) {
  "use strict";

  const ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
  const escapeHtml = text => String(text).replace(/[&<>"']/g, ch => ESCAPES[ch]);

  const FENCE = /^\s*```(.*)$/;
  const FENCE_END = /^\s*```\s*$/;
  const HEADING = /^(#{1,4})\s+(.*)$/;
  const RULE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
  const QUOTE = /^&gt;\s?(.*)$/;
  const LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  // Placeholders survive the inline passes untouched: they hold finished HTML
  // for code spans and links, which later passes must not re-interpret.
  const HOLD = "\u0000";

  function inline(text) {
    const held = [];
    const keep = html => {
      held.push(html);
      return `${HOLD}${held.length - 1}${HOLD}`;
    };
    let value = text.replace(/`([^`\n]+)`/g, (_, code) => keep(`<code>${code}</code>`));
    value = value.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, href) => keep(`<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`));
    value = value.replace(/(^|[\s(（])(https?:\/\/[^\s<>）)]+)/g,
      (_, lead, href) => lead + keep(`<a href="${href}" target="_blank" rel="noopener noreferrer">${href}</a>`));
    value = value.replace(/\*\*([^*\n]+)\*\*/g, (_, strong) => `<strong>${strong}</strong>`);
    value = value.replace(/(^|[^\w*])\*([^*\n]+)\*(?![\w*])/g, (_, lead, em) => `${lead}<em>${em}</em>`);
    value = value.replace(/(^|[^\w_])_([^_\n]+)_(?![\w_])/g, (_, lead, em) => `${lead}<em>${em}</em>`);
    return value.replace(/\u0000(\d+)\u0000/g, (_, index) => held[Number(index)]);
  }

  function codeBlock(lang, body) {
    const label = escapeHtml(lang || "text");
    return `<div class="code-block"><div class="code-head"><span class="code-lang">${label}</span>`
      + `<button type="button" class="code-copy" data-copy-code>复制</button></div>`
      + `<pre><code class="lang-${label}">${body}</code></pre></div>`;
  }

  function splitRow(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(cell => cell.trim());
  }

  function renderTable(head, rows) {
    const cells = head.map(cell => `<th>${inline(cell)}</th>`).join("");
    const body = rows.map(row => `<tr>${row.map(cell => `<td>${inline(cell)}</td>`).join("")}</tr>`).join("");
    // A wrapper keeps a wide table scrolling on its own instead of pushing the
    // whole transcript sideways.
    return `<div class="table-wrap"><table><thead><tr>${cells}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function buildList(rows, index, indent) {
    const tag = rows[index].ordered ? "ol" : "ul";
    let html = `<${tag}>`;
    let cursor = index;
    while (cursor < rows.length && rows[cursor].indent === indent) {
      if (rows[cursor].ordered !== rows[index].ordered) break;
      html += `<li>${inline(rows[cursor].text)}`;
      cursor += 1;
      if (cursor < rows.length && rows[cursor].indent > indent) {
        const nested = buildList(rows, cursor, rows[cursor].indent);
        html += nested.html;
        cursor = nested.index;
      }
      html += "</li>";
    }
    return {html: `${html}</${tag}>`, index: cursor};
  }

  function renderBlocks(source) {
    const lines = source.split("\n");
    const out = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index += 1; continue; }

      const fence = FENCE.exec(line);
      if (fence) {
        const lang = fence[1].trim().replace(/[^\w+#.-]/g, "").slice(0, 20);
        const body = [];
        index += 1;
        while (index < lines.length && !FENCE_END.test(lines[index])) { body.push(lines[index]); index += 1; }
        index += 1;
        out.push(codeBlock(lang, body.join("\n")));
        continue;
      }

      const heading = HEADING.exec(line);
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }

      if (RULE.test(line)) { out.push("<hr>"); index += 1; continue; }

      if (QUOTE.test(line)) {
        const body = [];
        while (index < lines.length && QUOTE.test(lines[index])) {
          body.push(QUOTE.exec(lines[index])[1]);
          index += 1;
        }
        out.push(`<blockquote>${renderBlocks(body.join("\n"))}</blockquote>`);
        continue;
      }

      if (line.includes("|") && index + 1 < lines.length && TABLE_RULE.test(lines[index + 1])) {
        const head = splitRow(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          rows.push(splitRow(lines[index]));
          index += 1;
        }
        out.push(renderTable(head, rows));
        continue;
      }

      const item = LIST_ITEM.exec(line);
      if (item) {
        const rows = [];
        while (index < lines.length) {
          const match = LIST_ITEM.exec(lines[index]);
          if (!match) break;
          rows.push({
            indent: match[1].replace(/\t/g, "  ").length,
            ordered: /\d/.test(match[2]),
            text: match[3],
          });
          index += 1;
        }
        let cursor = 0;
        while (cursor < rows.length) {
          const built = buildList(rows, cursor, rows[cursor].indent);
          out.push(built.html);
          cursor = built.index;
        }
        continue;
      }

      const paragraph = [];
      while (index < lines.length && lines[index].trim() && !startsBlock(lines, index)) {
        paragraph.push(lines[index]);
        index += 1;
      }
      out.push(`<p>${inline(paragraph.join("\n"))}</p>`);
    }
    return out.join("");
  }

  // A paragraph ends where a block starts. Written out rather than re-entered so
  // a line is classified once, by the same patterns the loop above uses.
  function startsBlock(lines, index) {
    const line = lines[index];
    if (FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line)) return true;
    if (LIST_ITEM.test(line)) return true;
    return line.includes("|") && index + 1 < lines.length && TABLE_RULE.test(lines[index + 1]);
  }

  function renderMarkdown(text) {
    try {
      return renderBlocks(escapeHtml(text == null ? "" : text));
    } catch (error) {
      // A renderer bug must not cost the reader the answer.
      return `<p>${escapeHtml(text == null ? "" : text)}</p>`;
    }
  }

  const api = {renderMarkdown, escapeHtml};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.ChagentMarkdown = api;
})(typeof globalThis === "undefined" ? this : globalThis);
