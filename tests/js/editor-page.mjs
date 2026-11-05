/* Behavior test for page-local drafts: save races, conflicts, workspace isolation.
 * Actual HTTP/version checks live in tests/test_web.py.
 *
 * The Python test only requires exit 0 and "checks passed" on stdout; every
 * assertion lives here, next to the fixtures it depends on.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { installDom } from "./dom-stub.mjs";

const STATIC = fileURLToPath(new URL("../../src/chagent/web/static/", import.meta.url));

// The stub replaces URL for download assertions; keep the real parser.
const RealURL = globalThis.URL;
installDom();
globalThis.NativeURL = RealURL;

const events = new Map();
window.addEventListener = (name, fn) => events.set(name, fn);
window.dispatchEvent = event => events.get(event.type)?.(event);
window.ChagentShowFiles = () => {};
const rules = [];
document.styleSheets = [{cssRules: rules, insertRule(rule) { rules.push({style: {setProperty() {}}}); return rules.length - 1; }}];
document.documentElement = {clientWidth: 1440, clientHeight: 900};
globalThis.getComputedStyle = () => ({getPropertyValue: () => "240"});
globalThis.setInterval = () => 0;
for (const name of ['file-choice-dialog', 'file-disk-dialog']) {
  const el = document.getElementById(name); el.showModal = () => { el.open = true; }; el.close = () => { el.open = false; };
}
const FILES = new Map([['one/a.md', {content: '# original\r\n', version: 'v1', identity: 'i1', newline: 'crlf'}], ['two/a.md', {content: '# workspace two\n', version: 'v2', identity: 'i2', newline: 'lf'}]]);
const preview = {kind: 'pdf', pages: 2, version: 'pdf-v1', identity: 'pdf-i1', size: 123, download_available: true};
let pendingRows = [];
const writes = [], entryRequests = []; let releaseSave = null, holdSave = false, holdEntries = false, releaseEntries = null;
function response(body, status = 200) { return {ok: status < 400, status, json: async () => body}; }
globalThis.fetch = async (url, options) => {
  const parsed = new globalThis.NativeURL(url, 'http://local');
  const parts = parsed.pathname.split('/'), id = parts[3], operation = parts[4];
  if (operation === 'outputs' && parts[6] === 'decision') return response({error: 'temporary failure'}, 503);
  if (operation === 'outputs') return response({items: pendingRows});
  if (operation === 'preview') return response({...preview, path: parsed.searchParams.get('path')});
  if (operation === 'file-state') return response({identity: preview.identity, size: preview.size});
  if (operation === 'entries') {
    entryRequests.push(id);
    if (holdEntries && id === 'one') await new Promise(resolve => { releaseEntries = resolve; });
    return response({items: [{type: 'file', name: id + '.txt', path: id + '.txt', identity: id}], next_offset: null});
  }
  if (operation === 'text' && options.method === 'PUT') {
    const body = JSON.parse(options.body); writes.push(body);
    const file = FILES.get(id + '/' + body.path);
    if (file.version !== body.version || file.identity !== body.identity) return response({error: 'version conflict'}, 409);
    if (holdSave) await new Promise(resolve => { releaseSave = resolve; });
    Object.assign(file, {content: body.content, version: 'saved-' + writes.length, identity: 'saved-' + writes.length});
    return response({path: body.path, ...file});
  }
  if (operation === 'text') {
    const path = parsed.searchParams.get('path'); return response({path, ...FILES.get(id + '/' + path)});
  }
  throw Error('Unexpected request ' + url);
};
const assert = (value, message) => { if (!value) throw Error(message); };
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
const el = id => document.getElementById(id);
const input = value => { el('file-editor').value = value; el('file-editor').fire('input'); };
const choice = label => { const found = el('file-choice-actions').querySelectorAll('button').find(button => button.textContent === label); assert(found, 'missing choice ' + label); found.fire('click'); };

eval(readFileSync(STATIC + 'markdown.js', 'utf8'));
window.ChagentMarkdown = globalThis.ChagentMarkdown;
await import(pathToFileURL(STATIC + 'file-editor.js').href);

(async () => {
  const one = {id: 'one', path: '/one'}, two = {id: 'two', path: '/two'};
  window.ChagentFiles.workspace(one); await tick(); await window.ChagentFiles.open('one', 'a.md');
  input('# submitted\n');
  assert(FILES.get('one/a.md').content === '# original\r\n', 'editing wrote to disk');
  assert(el('file-state').textContent.includes('未保存'), 'missing dirty marker');
  let prevented = false; events.get('beforeunload')({preventDefault() { prevented = true; }}); assert(prevented, 'missing unload protection');
  holdSave = true; el('file-save').fire('click'); await tick();
  input('# typed during save\n'); releaseSave(); await tick(); holdSave = false;
  assert(FILES.get('one/a.md').content === '# submitted\r\n', 'save changed CRLF or submitted the wrong draft');
  assert(el('file-editor').value === '# typed during save\n', 'typing during save was lost');
  assert(el('file-state').textContent.includes('未保存'), 'save cleared a newer draft');
  FILES.get('one/a.md').version = 'external'; FILES.get('one/a.md').content = '# external\r\n';
  el('file-save').fire('click'); await tick();
  assert(!el('file-conflict').hidden, 'old revision save should show conflict');
  assert(el('file-editor').value === '# typed during save\n', 'conflict lost draft');
  const switching = window.ChagentFiles.beforeWorkspace('/two'); await tick();
  assert(el('file-choice-dialog').open, 'workspace switch bypassed dirty prompt'); choice('取消');
  assert(await switching === false, 'cancel did not cancel switch');
  assert(el('file-editor').value === '# typed during save\n', 'cancel changed draft');
  el('file-tabs').querySelectorAll('button').find(button => button.textContent === '×').fire('click'); await tick();
  choice('取消'); await tick(); assert(el('file-path').textContent === 'a.md', 'cancel closed tab');
  const changing = window.ChagentFiles.beforeWorkspace('/two'); await tick(); choice('放弃修改'); assert(await changing, 'discard failed');
  window.ChagentFiles.workspace(two); await tick(); await window.ChagentFiles.open('two', 'a.md');
  assert(el('file-editor').value === '# workspace two\n', 'same-name cross-workspace draft leaked');
  input('<script>alert(1)</script>\n'); el('file-markdown').fire('click');
  assert(el('file-markdown-view').innerHTML.includes('&lt;script&gt;'), 'Markdown preview executed markup');
  const previous = entryRequests.filter(id => id === 'two').length;
  holdEntries = true; window.ChagentFiles.workspace(one); await tick();
  assert(releaseEntries, 'old workspace directory request was not held');
  window.ChagentFiles.workspace(two); await tick();
  assert(entryRequests.filter(id => id === 'two').length > previous, 'old refresh blocked the new workspace');
  releaseEntries(); await tick();
  const names = el('file-tree').querySelectorAll('button').map(button => button.textContent);
  assert(names.some(name => name.includes('two.txt')) && !names.some(name => name.includes('one.txt')), 'old directory response leaked into new workspace');
  await window.ChagentFiles.open('two', 'pages.pdf');
  assert(el('file-editor').hidden && !el('file-preview').hidden, 'PDF opened as editable text');
  assert(el('file-save').disabled && el('file-save-as').disabled, 'binary preview permits text saving');
  assert(el('preview-image').src.includes('page=1'), 'first PDF page was not selected');
  el('preview-next').fire('click'); assert(el('preview-image').src.includes('page=2'), 'PDF next page failed');
  assert(el('preview-next').disabled, 'PDF last-page boundary missing');
  el('preview-page').value = '99'; el('preview-page').fire('change'); assert(el('preview-page').value === 2, 'page clamp failed');
  el('preview-image').naturalWidth = 400; el('preview-viewport').clientWidth = 300; el('preview-image').onload();
  assert(el('preview-image').width === 276, 'fit-width preview failed');
  el('preview-in').fire('click'); assert(el('preview-image').width === 500, 'image/PDF zoom failed');
  el('preview-image').onerror(); assert(!el('preview-error').hidden, 'preview decode error missing');
  el('preview-retry').fire('click'); await tick(); assert(el('preview-error').hidden, 'retry did not reload preview');
  preview.kind = 'unsupported'; preview.reason = 'unsupported format';
  await window.ChagentFiles.open('two', 'image.heic'); assert(el('preview-image').hidden, 'unsupported format rendered inline');
  assert(el('preview-note').textContent === 'unsupported format', 'missing format explanation');
  pendingRows = [{id: 'out', path: 'a.md', size: 3, status: 'pending', token: 'nonce'}];
  await window.ChagentFiles.refresh();
  el('file-confirmations').querySelectorAll('button').find(button => button.textContent === '覆盖').fire('click'); await tick();
  const actions = el('file-confirmations').querySelectorAll('button');
  assert(actions.length && actions.every(button => !button.disabled), 'temporary confirmation failure left controls disabled');
  console.log('draft/save/conflict/workspace/unload/Markdown checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
