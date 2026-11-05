/* Page entry point.
 *
 * One import, because app.js is the wiring point: it creates the views, hands
 * each of them the same action object, and starts the page. A module the page
 * forgot to load is a missing import in app.js, which fails at load instead of
 * leaving a button dead at runtime.
 *
 * markdown.js stays a classic script in index.html: it is also loaded by
 * `require()` from the Python tests, and it publishes the renderer as a page
 * global before any module evaluates. core/markdown.js is the one place that
 * reads it.
 */

import "./app.js";
