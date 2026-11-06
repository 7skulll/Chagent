/* Unsaved edits kept across a reload or a crash.
 *
 * A draft lives in the browser, never in the workspace and never in model
 * context: it is what the user typed and did not save. Losing it to a refresh is
 * the one failure this panel could not recover from, which is why it is stored
 * by (workspace id, path) with the version it was based on.
 *
 * The version is what makes the restore honest: a draft based on a version the
 * file no longer has is still offered, but marked as a conflict instead of
 * being applied silently — the same rule the editor already uses for a save.
 *
 * Storage is injected rather than read from `localStorage` directly, so the
 * rules can be tested without a DOM.
 */

//: One namespace, so a future cleanup can find every draft and nothing else.
export const DRAFT_PREFIX = "chagent.draft.";

//: A workspace plus a path can produce long keys; browsers allow far more, but a
//: hard cap keeps a single pathological path from crowding the store out.
export const DEFAULT_LIMIT = 40;
export const DEFAULT_MAX_BYTES = 256 * 1024;

export const draftKey = (workspaceId, path) => `${DRAFT_PREFIX}${workspaceId}:${path}`;

export function createDraftStore(storage, {limit = DEFAULT_LIMIT, maxBytes = DEFAULT_MAX_BYTES} = {}) {
  function entries() {
    const found = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (!key || !key.startsWith(DRAFT_PREFIX)) continue;
      let value = null;
      try { value = JSON.parse(storage.getItem(key) || "null"); } catch { value = null; }
      found.push({key, value});
    }
    return found;
  }

  function prune() {
    const all = entries();
    if (all.length <= limit) return;
    all.sort((left, right) => (left.value?.at || 0) - (right.value?.at || 0));
    for (const entry of all.slice(0, all.length - limit)) storage.removeItem(entry.key);
  }

  return {
    key: draftKey,
    /* Store the draft if it differs from what is on disk. Returns false when the
     * draft is too large to keep — the file itself is still on disk, and a
     * quota failure must never break typing. */
    save(workspaceId, path, {draft, version, identity}) {
      if (typeof draft !== "string" || draft.length > maxBytes) return false;
      try {
        storage.setItem(draftKey(workspaceId, path), JSON.stringify({
          draft, version, identity, at: Date.now(),
        }));
        prune();
        return true;
      } catch {
        return false;   // quota or disabled storage: a convenience, not a promise
      }
    },
    load(workspaceId, path) {
      try {
        const value = JSON.parse(storage.getItem(draftKey(workspaceId, path)) || "null");
        if (!value || typeof value.draft !== "string") return null;
        return value;
      } catch {
        return null;    // a corrupted entry is not a draft
      }
    },
    drop(workspaceId, path) { storage.removeItem(draftKey(workspaceId, path)); },
    entries,
    prune,
  };
}

/* What to do with a stored draft once the file has been read again.
 *
 * `version` is the SHA-256 of what the draft was based on. Equal versions mean
 * the disk has not moved and the draft can simply come back. A different version
 * still returns the text — the user's typing is never thrown away — but flags it
 * so the editor shows the conflict instead of letting a save overwrite a newer
 * file without a word. */
export function restorable(stored, current) {
  if (!stored || typeof stored.draft !== "string") return null;
  if (typeof current?.original !== "string") return null;
  if (stored.draft === current.original) return null;   // nothing unsaved after all
  return {draft: stored.draft, conflict: stored.version !== current.version};
}
