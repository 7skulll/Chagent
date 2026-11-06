/* PDF and image preview: paging, zoom, fit, decode errors.
 *
 * Split out of the file panel because it is a viewer, not an editor: it owns no
 * drafts, never writes, and its only inputs are the open tab and the workspace
 * it belongs to. The tab object is shared by reference, so page and zoom changes
 * land in the same place the tab lives.
 */

import { $ } from "../core/dom.js";

export function createPreview({active, current, readPreview, paint, downloadUrl}) {
  function size() {
    const tab = active(), img = $("preview-image");
    if (!tab || !img.naturalWidth) return;
    img.width = Math.round(tab.zoom === null
      ? Math.min(img.naturalWidth, Math.max(100, $("preview-viewport").clientWidth - 24))
      : img.naturalWidth * tab.zoom);
    $("preview-scale").textContent = tab.zoom === null ? "适应" : Math.round(tab.zoom * 100) + "%";
  }

  function show(space, tab) {
    const pdf = tab.kind === "pdf", supported = pdf || tab.kind === "image";
    for (const id of ["preview-prev", "preview-page", "preview-pages", "preview-next"]) $(id).hidden = !pdf;
    $("preview-page").value = tab.page;
    $("preview-page").max = tab.info.pages || 1;
    $("preview-pages").textContent = "/ " + (tab.info.pages || 1);
    $("preview-prev").disabled = tab.page <= 1;
    $("preview-next").disabled = tab.page >= (tab.info.pages || 1);
    for (const id of ["preview-out", "preview-in", "preview-fit"]) $(id).disabled = !supported;
    const link = $("preview-download");
    link.href = downloadUrl(space.id, tab.path);
    link.download = tab.path.split("/").pop();
    link.hidden = tab.info.download_available === false;
    $("preview-note").textContent = supported
      ? (pdf ? "PDF 分页预览" : "图片静态预览 · " + tab.info.width + " × " + tab.info.height)
        + "；仅供本机查看，不代表模型已理解图片或具备 OCR。"
      : tab.info.reason;
    const image = $("preview-image");
    image.hidden = !supported || tab.unavailable || !!tab.previewError;
    if (!supported) { image.removeAttribute("src"); image.dataset.key = ""; return; }
    const url = `/api/workspaces/${space.id}/preview/page?`
      + new URLSearchParams({path: tab.path, version: tab.version, page: tab.page});
    if (image.dataset.key !== url || tab.imageKey === null) {
      tab.imageKey = url;
      tab.previewError = false;
      image.hidden = false;
      image.dataset.key = url;
      $("preview-error").hidden = true;
      image.onload = () => {
        if (active() !== tab) return;
        tab.previewError = false;
        $("preview-error").hidden = true;
        size();
      };
      image.onerror = () => {
        if (active() !== tab) return;
        tab.previewError = true;
        $("preview-error").textContent = "预览加载失败或文件已变化，请重新加载；也可下载原文件查看。";
        $("preview-error").hidden = false;
        image.hidden = true;
      };
      image.src = url;
    }
    size();
  }

  async function retry() {
    const space = current(), tab = active();
    if (!tab || tab.kind === "text") return;
    await readPreview(space, tab);
    tab.imageKey = null;
    if (current() === space) paint();
  }

  function changePage(delta, absolute = false) {
    const tab = active();
    if (tab?.kind !== "pdf") return;
    tab.page = Math.max(1, Math.min(tab.info.pages, absolute ? Number(delta) || 1 : tab.page + delta));
    paint();
  }

  $("preview-prev").addEventListener("click", () => changePage(-1));
  $("preview-next").addEventListener("click", () => changePage(1));
  $("preview-page").addEventListener("change", () => changePage($("preview-page").value, true));
  $("preview-out").addEventListener("click", () => {
    const tab = active();
    if (tab) { tab.zoom = Math.max(.25, (tab.zoom || 1) / 1.25); size(); }
  });
  $("preview-in").addEventListener("click", () => {
    const tab = active();
    if (tab) { tab.zoom = Math.min(4, (tab.zoom || 1) * 1.25); size(); }
  });
  $("preview-fit").addEventListener("click", () => {
    const tab = active();
    if (tab) { tab.zoom = null; size(); }
  });
  window.addEventListener("resize", size);

  return {size, show, retry, changePage};
}
