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

/* Parent links make `remove()` and `insertBefore(node, ref)` behave, which is
 * what a re-rendered list depends on. They are non-enumerable so the existing
 * "serialise the DOM" assertions stay acyclic. */
function adopt(parent, node) {
  if (!node || node.nodeType !== 1) return node;
  Object.defineProperty(node, "_parent", {value: parent, writable: true, configurable: true, enumerable: false});
  return node;
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
    append(...nodes) { for (const node of nodes) if (node) this.children.push(adopt(this, node)); },
    prepend(...nodes) { this.children = [...nodes.map(node => adopt(this, node)), ...this.children]; },
    replaceChildren(...nodes) { this.children = nodes.filter(Boolean).map(node => adopt(this, node)); },
    appendChild(node) { this.children.push(adopt(this, node)); return node; },
    insertBefore(node, reference) {
      adopt(this, node);
      const index = reference ? this.children.indexOf(reference) : -1;
      if (index < 0) this.children.push(node); else this.children.splice(index, 0, node);
      return node;
    },
    remove() {
      const parent = this._parent;
      if (!parent) return;
      const index = parent.children.indexOf(this);
      if (index >= 0) parent.children.splice(index, 1);
    },
    replaceWith(node) {
      const parent = this._parent;
      if (!parent) return;
      const index = parent.children.indexOf(this);
      adopt(parent, node);
      if (index >= 0) parent.children.splice(index, 1, node);
    },
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
    /* The file panel installs its resize CSS custom properties through one
     * inserted rule and reads the viewport width to clamp them. */
    documentElement: {clientWidth: 1200, clientHeight: 900},
    styleSheets: [{cssRules: [], insertRule() {
      this.cssRules.push({style: {setProperty() {}}});
      return this.cssRules.length - 1;
    }}],
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
  /* The page polls the file panel every five seconds. A real interval would keep
   * the Node process alive after the verdict is printed, and the Python test
   * would time out with a passing assert already in hand. */
  setGlobal("setInterval", () => 0);
  // Layout sizes for the panel resize rules: no CSS, one number.
  setGlobal("getComputedStyle", () => ({getPropertyValue: () => "240"}));
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
