/* Page entry point.
 *
 * The page is loaded as ES modules so each script has its own scope and a
 * missing dependency fails loudly instead of silently doing nothing. The import
 * order below is the only ordering guarantee: app.js must evaluate before
 * file-editor.js, because it resolves the current workspace and the file panel
 * reads `window.ChagentCurrentWorkspace` on start.
 *
 * markdown.js stays a classic script in index.html: markdown.js is also loaded
 * by `require()` from the Python tests, and it publishes `ChagentMarkdown`
 * before any module runs.
 *
 * This file is deliberately tiny. Module splitting happens in later stages of
 * docs/前端重构方案.md; the first goal is only to make the load mechanism
 * explicit and testable.
 */

import "./app.js";
import "./file-editor.js";
