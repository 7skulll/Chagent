/* Column widths and the file-tree height: three drag handles, three numbers.
 *
 * Pointer and keyboard resize share the same clamps, so a keyboard user cannot
 * reach a width a mouse cannot. The values are CSS custom properties written
 * through one inserted rule: the page is served under `style-src 'self'`, which
 * rules out a style element or an inline style attribute.
 */

import { storage } from "../core/account-storage.js";
import { $ } from "../core/dom.js";

const DEFAULTS = {sidebar: 216, files: 320, tree: 190};
const HANDLES = {sidebar: "sidebar-resize", files: "files-resize", tree: "tree-resize"};

export function createLayout() {
  const sheet = document.styleSheets[0];
  const index = sheet.insertRule(":root {}", sheet.cssRules.length);
  const vars = sheet.cssRules[index].style;
  const maxWidth = () => document.documentElement.clientWidth;

  function dimension(kind, value) {
    let min, max, property;
    if (kind === "sidebar") {
      const files = document.body.classList.contains("tools-collapsed")
        ? 0
        : Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--tools-w")) || 320;
      min = 180;
      max = Math.max(min, Math.min(400, maxWidth() - files - 370));
      property = "--sidebar-w";
    } else if (kind === "files") {
      const sidebar = document.body.classList.contains("sidebar-collapsed")
        ? 0
        : Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sidebar-w")) || 216;
      min = 320;
      max = Math.max(min, maxWidth() - sidebar - 370);
      property = "--tools-w";
    } else {
      min = 70;
      max = Math.max(min, ($("conversation-tools").clientHeight || document.documentElement.clientHeight) - 330);
      property = "--tree-h";
    }
    value = Math.round(Math.max(min, Math.min(max, value)));
    const handle = $(HANDLES[kind]);
    handle.setAttribute("aria-valuemin", String(min));
    handle.setAttribute("aria-valuemax", String(max));
    handle.setAttribute("aria-valuenow", String(value));
    handle.setAttribute("aria-orientation", kind === "tree" ? "horizontal" : "vertical");
    vars.setProperty(property, value + "px");
    storage.setItem("chagent.layout." + kind, String(value));
    return value;
  }

  const handles = {};
  for (const [kind, id] of Object.entries(HANDLES)) {
    const handle = $(id);
    const saved = Number(storage.getItem("chagent.layout." + kind));
    const initial = saved || DEFAULTS[kind];
    let value = dimension(kind, initial);
    handles[kind] = () => value;
    handle.addEventListener("pointerdown", event => {
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const start = kind === "tree" ? event.clientY : event.clientX;
      const initialValue = value;
      const move = event_ => {
        const delta = (kind === "tree" ? event_.clientY : event_.clientX) - start;
        value = dimension(kind, initialValue + (kind === "files" ? -delta : delta));
      };
      const end = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", end);
        handle.removeEventListener("pointercancel", end);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", end);
      handle.addEventListener("pointercancel", end);
    });
    handle.addEventListener("keydown", event_ => {
      if (!["ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown"].includes(event_.key)) return;
      event_.preventDefault();
      const step = ["ArrowLeft", "ArrowUp"].includes(event_.key) ? -20 : 20;
      value = dimension(kind, value + step);
    });
  }

  /* Re-clamp after a viewport change: a saved 400px column is not a width, it is
   * a width on the screen it was chosen on. */
  function clampAll() {
    for (const kind of Object.keys(HANDLES)) dimension(kind, Number(storage.getItem("chagent.layout." + kind)));
  }
  window.addEventListener("resize", clampAll);

  return {clampAll};
}
