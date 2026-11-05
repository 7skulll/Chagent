/* One state object, replaced only through `update`, with subscribers.
 *
 * Used for the streamed run, which is the only part of the page whose state is
 * produced by something other than a click: SSE events arrive whether or not
 * anyone is looking, and several of them can land between two screen paints.
 * Everything else on the page is a direct consequence of a request the user
 * started, so it keeps calling its own refresh function.
 */

export function createStore(initial) {
  let state = initial;
  const listeners = [];

  return {
    get: () => state,
    /* `change` receives the current state and returns the next one. Returning
     * the same reference means "nothing to announce", which is how a repeated
     * event (an SSE replay) costs no repaint. */
    update(change) {
      const next = change(state);
      if (next === state) return state;
      state = next;
      for (const listener of listeners) listener(state);
      return state;
    },
    subscribe(listener) {
      listeners.push(listener);
      return () => {
        const index = listeners.indexOf(listener);
        if (index >= 0) listeners.splice(index, 1);
      };
    },
  };
}
