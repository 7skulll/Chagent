/* A DOM stub for running the page scripts under Node.
 *
 * Extracted from the string that used to live inside tests/test_web_render.py
 * so both page tests share one definition. It answers exactly the calls the
 * page scripts make on load; anything else is a stub with no behaviour.
 *
 * This is not a browser: no layout, no CSS, no pointer. It proves a handler is
 * attached and that rendering produces the expected nodes — visual judgement
 * stays manual.
 */

const els = new Map();
export const MEDIA = new Map();

function matches(node, selector) {
  return selector.split(",").some(part => {
    const sel = part.trim();
    if (!sel) return false;
    if (sel.startsWith(".")) return Boolean(node._classes && node._classes.has(sel.slice(1)));
    return node.tagName === sel.toUpperCase();
  });
}

function find(node, selector, first) {
  const out = [];
  const walk = current => {
    for (const child of current.children || []) {
      if (matches(child, selector)) { out.push(child); if (first) return true; }
      if (walk(child) && first) return true;
    }
    return false;
  };
  walk(node);
  return first ? (out[0] || null) : out;
}

export function makeEl(tag) {
  const el = {
    nodeType: 1, tagName: String(tag || "div").toUpperCase(), children: [], dataset: {}, style: {},
    listeners: {}, _text: "", _classes: new Set(),
    get textContent() { return this._text; },
    set textContent(value) { this._text = String(value); this.children = []; },
    get className() { return [...this._classes].join(" "); },
    set className(value) { this._classes = new Set(String(value).split(/\s+/).filter(Boolean)); },
    set innerHTML(value) { this._html = String(value); },
    get innerHTML() { return this._html || ""; },
    append(...nodes) { for (const node of nodes) if (node) this.children.push(node); },
    prepend(...nodes) { this.children = [...nodes, ...this.children]; },
    replaceChildren(...nodes) { this.children = nodes.filter(Boolean); },
    appendChild(node) { this.children.push(node); return node; },
    insertBefore(node) { this.children.unshift(node); },
    remove() {}, replaceWith() {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    removeEventListener() {},
    fire(type, event = {}) { for (const fn of this.listeners[type] || []) fn({preventDefault() {}, stopPropagation() {}, ...event}); },
    querySelector(selector) { return find(this, selector, true); },
    querySelectorAll(selector) { return find(this, selector, false); },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    focus() {}, select() {}, setSelectionRange() {}, closest() { return null; },
    requestSubmit() {}, showModal() {}, close() {},
    click() { if (this.tagName === "A") globalThis.lastDownload = {href: this.href, name: this.download}; },
    getBoundingClientRect() { return {top: 0, bottom: 100}; },
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    value: "", hidden: true, disabled: false, open: false, maxLength: 0, title: "", placeholder: "", type: "",
  };
  el.classList = {
    contains: name => el._classes.has(name),
    add: name => el._classes.add(name),
    remove: name => el._classes.delete(name),
    toggle: (name, force) => {
      const on = force === undefined ? !el._classes.has(name) : Boolean(force);
      if (on) el._classes.add(name); else el._classes.delete(name);
      return on;
    },
  };
  return el;
}

/* Install the browser globals the page scripts expect. Call once per process.
 *
 * Assignment is not enough: this module is strict (ESM), and Node defines some
 * of these names — ``navigator`` since v21 — as getter-only accessors on
 * ``globalThis``. Under the old CommonJS stdin harness that failed silently;
 * here it would throw. Define the properties instead of assigning them.
 */
function setGlobal(name, value) {
  Object.defineProperty(globalThis, name, {value, writable: true, configurable: true, enumerable: true});
}

export function installDom() {
  setGlobal("document", {
    getElementById(id) { if (!els.has(id)) els.set(id, makeEl("div")); els.get(id).id = id; return els.get(id); },
    createElement: tag => makeEl(tag),
    createDocumentFragment: () => makeEl("fragment"),
    createTextNode: text => ({nodeType: 3, textContent: String(text), children: []}),
    querySelector: () => makeEl("div"), querySelectorAll: () => [],
    addEventListener() {}, body: makeEl("body"),
  });
  setGlobal("window", {addEventListener() {}, matchMedia: query => {
    if (!MEDIA.has(query)) MEDIA.set(query, {matches: false, addEventListener(type, fn) { this.changed = fn; }});
    return MEDIA.get(query);
  }});
  setGlobal("matchMedia", globalThis.window.matchMedia);
  setGlobal("localStorage", {store: new Map(), getItem(k) { return this.store.has(k) ? this.store.get(k) : null; },
    setItem(k, v) { this.store.set(k, String(v)); }, removeItem(k) { this.store.delete(k); }});
  setGlobal("navigator", {clipboard: {writeText: async () => {}}});
  setGlobal("requestAnimationFrame", cb => setTimeout(cb, 0));
  setGlobal("cancelAnimationFrame", id => clearTimeout(id));
  setGlobal("EventSource", class { addEventListener() {} close() {} });
  setGlobal("XMLHttpRequest", class {
    upload = {addEventListener() {}}; listeners = {};
    open(method, url) { const m = /name=([^&]+)/.exec(url || ""); this.sentName = m ? decodeURIComponent(m[1]) : "upload"; }
    setRequestHeader() {} abort() {}
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    send() {
      setTimeout(() => {
        this.status = 201; this.responseText = JSON.stringify({path: "uploads/" + this.sentName});
        for (const fn of this.listeners.load || []) fn();
      }, 5);
    }
  });
  setGlobal("URL", {createObjectURL: blob => { globalThis.downloadBlob = blob; return "blob:x"; }, revokeObjectURL() {}});
  setGlobal("FileReader", class { readAsDataURL() {} addEventListener() {} });
  return {els, media: MEDIA};
}
