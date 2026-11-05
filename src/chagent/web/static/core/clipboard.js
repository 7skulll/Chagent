/* Copying text from a button that reports its own success.
 *
 * Two callers (the answer action row and the delegated code-block control) and
 * one behaviour: the label says 已复制 for a moment, and a refused clipboard is
 * reported rather than swallowed — the user clicked something and nothing
 * happened is the failure mode this exists to avoid.
 */

export async function copyText(text, button, onError) {
  const label = button.textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "已复制";
    setTimeout(() => { button.textContent = label; }, 1200);
  } catch {
    onError(new Error("浏览器拒绝了剪贴板访问，请手动选中后复制"));
  }
}
