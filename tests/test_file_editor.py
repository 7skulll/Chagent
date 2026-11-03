"""Behavior tests for page-local drafts. Actual HTTP/version checks live in test_web."""
from pathlib import Path
import shutil
import subprocess
import unittest
try:
    from test_web_render import HARNESS
except ImportError:
    from tests.test_web_render import HARNESS

STATIC=Path(__file__).resolve().parents[1]/'src/chagent/web/static'

EDITOR_HARNESS = HARNESS.split('const SESSION =')[0] + r'''
const events = new Map();
window.addEventListener = (name, fn) => events.set(name, fn);
window.dispatchEvent = event => events.get(event.type)?.(event);
window.ChagentShowFiles = () => {};
const rules=[];
document.styleSheets=[{cssRules:rules,insertRule(rule){rules.push({style:{setProperty(){}}});return rules.length-1;}}];
document.documentElement={clientWidth:1440,clientHeight:900};
globalThis.getComputedStyle=()=>({getPropertyValue:()=>"240"});
globalThis.setInterval=()=>0;
for(const name of ['file-choice-dialog','file-disk-dialog']) {
  const el=document.getElementById(name);el.showModal=()=>{el.open=true;};el.close=()=>{el.open=false;};
}
const FILES=new Map([['one/a.md',{content:'# original\r\n',version:'v1',identity:'i1',newline:'crlf'}],['two/a.md',{content:'# workspace two\n',version:'v2',identity:'i2',newline:'lf'}]]);
const writes=[], entryRequests=[];let releaseSave=null,holdSave=false,holdEntries=false,releaseEntries=null;
function response(body,status=200){return {ok:status<400,status,json:async()=>body};}
globalThis.fetch=async(url,options)=>{
  const parsed=new globalThis.NativeURL(url,'http://local');
  const parts=parsed.pathname.split('/'),id=parts[3],operation=parts[4];
  if(operation==='outputs')return response({items:[]});
  if(operation==='entries'){
    entryRequests.push(id);
    if(holdEntries&&id==='one')await new Promise(resolve=>{releaseEntries=resolve;});
    return response({items:[{type:'file',name:id+'.txt',path:id+'.txt',identity:id}],next_offset:null});
  }
  if(operation==='text'&&options.method==='PUT'){
    const body=JSON.parse(options.body);writes.push(body);
    const file=FILES.get(id+'/'+body.path);
    if(file.version!==body.version||file.identity!==body.identity)return response({error:'version conflict'},409);
    if(holdSave)await new Promise(resolve=>{releaseSave=resolve;});
    Object.assign(file,{content:body.content,version:'saved-'+writes.length,identity:'saved-'+writes.length});
    return response({path:body.path,...file});
  }
  if(operation==='text'){
    const path=parsed.searchParams.get('path');return response({path,...FILES.get(id+'/'+path)});
  }
  throw Error('Unexpected request '+url);
};
const assert=(value,message)=>{if(!value)throw Error(message);};
const tick=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
const el=id=>document.getElementById(id);
const input=value=>{el('file-editor').value=value;el('file-editor').fire('input');};
const choice=label=>{const found=el('file-choice-actions').querySelectorAll('button').find(button=>button.textContent===label);assert(found,'missing choice '+label);found.fire('click');};
require(process.cwd()+'/markdown.js');window.ChagentMarkdown=globalThis.ChagentMarkdown;
eval(require('fs').readFileSync(process.cwd()+'/file-editor.js','utf8'));
(async()=>{
  const one={id:'one',path:'/one'},two={id:'two',path:'/two'};
  window.ChagentFiles.workspace(one);await tick();await window.ChagentFiles.open('one','a.md');
  input('# submitted\n');
  assert(FILES.get('one/a.md').content==='# original\r\n','editing wrote to disk');
  assert(el('file-state').textContent.includes('未保存'),'missing dirty marker');
  let prevented=false;events.get('beforeunload')({preventDefault(){prevented=true;}});assert(prevented,'missing unload protection');
  holdSave=true;el('file-save').fire('click');await tick();
  input('# typed during save\n');releaseSave();await tick();holdSave=false;
  assert(FILES.get('one/a.md').content==='# submitted\r\n','save changed CRLF or submitted the wrong draft');
  assert(el('file-editor').value==='# typed during save\n','typing during save was lost');
  assert(el('file-state').textContent.includes('未保存'),'save cleared a newer draft');
  FILES.get('one/a.md').version='external';FILES.get('one/a.md').content='# external\r\n';
  el('file-save').fire('click');await tick();
  assert(!el('file-conflict').hidden,'old revision save should show conflict');
  assert(el('file-editor').value==='# typed during save\n','conflict lost draft');
  const switching=window.ChagentFiles.beforeWorkspace('/two');await tick();
  assert(el('file-choice-dialog').open,'workspace switch bypassed dirty prompt');choice('取消');
  assert(await switching===false,'cancel did not cancel switch');
  assert(el('file-editor').value==='# typed during save\n','cancel changed draft');
  el('file-tabs').querySelectorAll('button').find(button=>button.textContent==='×').fire('click');await tick();
  choice('取消');await tick();assert(el('file-path').textContent==='a.md','cancel closed tab');
  const changing=window.ChagentFiles.beforeWorkspace('/two');await tick();choice('放弃修改');assert(await changing,'discard failed');
  window.ChagentFiles.workspace(two);await tick();await window.ChagentFiles.open('two','a.md');
  assert(el('file-editor').value==='# workspace two\n','same-name cross-workspace draft leaked');
  input('<script>alert(1)</script>\n');el('file-markdown').fire('click');
  assert(el('file-markdown-view').innerHTML.includes('&lt;script&gt;'),'Markdown preview executed markup');
  const previous=entryRequests.filter(id=>id==='two').length;
  holdEntries=true;window.ChagentFiles.workspace(one);await tick();
  assert(releaseEntries,'old workspace directory request was not held');
  window.ChagentFiles.workspace(two);await tick();
  assert(entryRequests.filter(id=>id==='two').length>previous,'old refresh blocked the new workspace');
  releaseEntries();await tick();
  const names=el('file-tree').querySelectorAll('button').map(button=>button.textContent);
  assert(names.some(name=>name.includes('two.txt'))&&!names.some(name=>name.includes('one.txt')),'old directory response leaked into new workspace');
  console.log('draft/save/conflict/workspace/unload/Markdown checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


@unittest.skipUnless(shutil.which('node'),'Node required')
class EditorTests(unittest.TestCase):
    def test_drafts_save_races_conflicts_and_workspace_isolation(self):
        # The shared harness replaces URL for download assertions; keep the real parser.
        code='globalThis.NativeURL=URL;\n'+EDITOR_HARNESS
        result=subprocess.run([shutil.which('node'),'-'],input=code,text=True,capture_output=True,cwd=STATIC,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('checks passed',result.stdout)
