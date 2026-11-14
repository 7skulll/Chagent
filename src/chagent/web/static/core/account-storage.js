/* One browser storage namespace per authenticated local account. */
export function scopedStorage(storage, account) {
  if (!account) return storage; // Explicit injected legacy/test pages have no account.
  const prefix = `chagent.user.${account}.`;
  const keys = () => Array.from({length: storage.length}, (_, i) => storage.key(i)).filter(k => k && k.startsWith(prefix));
  return {
    get length() { return keys().length; },
    key: index => { const key = keys()[index]; return key ? key.slice(prefix.length) : null; },
    getItem: key => storage.getItem(prefix + key),
    setItem: (key, value) => storage.setItem(prefix + key, value),
    removeItem: key => storage.removeItem(prefix + key),
  };
}
const account = () => globalThis.document?.querySelector?.('meta[name="chagent-account"]')?.content || '';
export const storage = {
  get length() { return scopedStorage(globalThis.localStorage, account()).length; },
  key: i => scopedStorage(globalThis.localStorage, account()).key(i),
  getItem: key => scopedStorage(globalThis.localStorage, account()).getItem(key),
  setItem: (key, value) => scopedStorage(globalThis.localStorage, account()).setItem(key, value),
  removeItem: key => scopedStorage(globalThis.localStorage, account()).removeItem(key),
};
if (globalThis.addEventListener) globalThis.addEventListener('storage', event => {
  if (event.key === 'chagent.account.current' && account() && event.newValue !== account()) {
    globalThis.location.replace('/account');
  }
});
