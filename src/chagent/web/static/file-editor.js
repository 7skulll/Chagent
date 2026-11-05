/* The file panel: tree, tabs, drafts, save, conflicts, preview, layout.
 *
 * Drafts live in this page, keyed by workspace ID and relative path. Opening a
 * file does not add it to model context. Only Save writes an editing draft.
 *
 * It used to be an IIFE that published itself on the page global and called
 * back into the chat through it. Both directions are arguments now:
 * `initFileEditor(ui)` returns the panel's actions, and the panel calls the
 * chat's through the same object, so a rename breaks at the call site instead
 * of leaving a button silently dead.
 */

import { $ } from "./core/dom.js";
import { renderMarkdown } from "./core/markdown.js";
import { createLayout } from "./views/layout.js";
import { createPreview } from "./views/preview.js";

export function initFileEditor(ui) {
  const spaces = new Map();
  let current = null, choices = Promise.resolve(), tabSignature = "";
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const button = (label, action) => { const n = node("button", label); n.type = "button"; n.addEventListener("click", () => Promise.resolve().then(action).catch(fail)); return n; };
  async function request(url, method = "GET", value) {
    const response = await fetch(url, {method, ...(value === undefined ? {} : {headers:{"Content-Type":"application/json"}, body:JSON.stringify(value)})});
    const result = await response.json();
    if (!response.ok) { const error = new Error(result.error || `请求失败 ${response.status}`); error.status = response.status; throw error; }
    return result;
  }
  const base = space => `/api/workspaces/${space.id}`;
  const notice = message => { $("file-notice").textContent = message; };
  const fail = error => notice(error.message || String(error));
  const dirty = tab => (!tab.kind || tab.kind === "text") && tab.draft !== tab.original;
  const normalized = text => text.replace(/\r\n?/g, "\n");
  const serialized = tab => tab.readonly ? tab.raw : tab.newline === "cr" ? tab.draft.replace(/\n/g,"\r") : tab.newline === "crlf" ? tab.draft.replace(/\r?\n/g, "\r\n") : tab.draft;
  const active = () => current && current.tabs.get(current.active);

  function choose(message, options, title="确认操作") {
    const run = () => new Promise(resolve => {
      const dialog = $("file-choice-dialog");
      $("file-choice-title").textContent = title;
      $("file-choice-message").textContent = message;
      $("file-choice-actions").replaceChildren(...options.map(([value,label]) => button(label, () => { dialog.close(); resolve(value); })));
      dialog.oncancel = event => { event.preventDefault(); dialog.close(); resolve("cancel"); };
      dialog.showModal();
    });
    const result = choices.then(run); choices = result.catch(() => {}); return result;
  }
  function askName(title, initial="") {
    const run=()=>new Promise(resolve=>{
      const dialog=$("file-name-dialog"),input=$("file-name-input");
      $("file-name-title").textContent=title;input.value=initial;$("file-name-submit").textContent="确定";
      const finish=value=>{dialog.close();resolve(value);};
      $("file-name-form").onsubmit=event=>{event.preventDefault();if(input.value.trim())finish(input.value.trim());};
      $("file-name-cancel").onclick=()=>finish(null);
      dialog.oncancel=event=>{event.preventDefault();finish(null);};
      dialog.showModal();input.focus();input.select();
    });
    const result=choices.then(run);choices=result.catch(()=>{});return result;
  }
  async function guard(space, paths = null) {
    const tabs = [...space.tabs.values()].filter(tab => dirty(tab) && (!paths || paths.some(path => tab.path === path || tab.path.startsWith(path + "/"))));
    if (!tabs.length) return true;
    const decision = await choose(`${tabs.map(t => t.path).join("、")} 有未保存修改。`, [["save","保存后继续"],["discard","放弃修改"],["cancel","取消"]],"未保存的修改");
    if (decision === "cancel") return false;
    if (decision === "save") {
      for (const tab of tabs) if (!await saveTab(space,tab)) return false;
    } else for (const tab of tabs) { tab.draft = tab.original; tab.changed = false; }
    paint(); return true;
  }
  function takeRead(tab, data) {
    Object.assign(tab, {kind:"text",raw:data.content, original:normalized(data.content), draft:normalized(data.content), version:data.version,
      identity:data.identity, newline:data.newline, readonly:data.newline === "mixed", changed:false, unavailable:false});
  }
  function selectFile(space,path) {
    space.selected=path;space.selection={type:"file",path};
    if(current!==space) return;
    for(const row of $("file-tree").querySelectorAll(".file-tree-row")) {
      const item=row.querySelector("button"),selected=item?.dataset.path===path;
      row.classList.toggle("selected",selected);item?.setAttribute("aria-selected",String(selected));
    }
  }
  async function open(spaceId,path) {
    if (!current || current.id !== String(spaceId)) {
      const list = await request("/api/workspaces");
      const target = list.items.find(item => String(item.id) === String(spaceId));
      if (!target || !target.available) throw new Error("文件所属工作区不可用");
      await ui.switchWorkspace(target.path);
      if (!current || current.id !== String(spaceId)) return;
    }
    const space = current;
    if (space.tabs.has(path)) { selectFile(space,path);space.active = path; reveal(); paint(); return; }
    if (space.tabs.size >= 25) throw new Error("请先关闭部分文件标签（最多 25 个）");
    const tab = {path, preview:false, page:1,zoom:null};
    if(/\.(pdf|png|jpe?g|gif|webp|heic|avif|bmp)$/i.test(path)) {
      await readPreview(space,tab);
    } else {
      try {
        const data=await request(base(space)+"/text?"+new URLSearchParams({path}));takeRead(tab,data);
      } catch(error) {
        if(error.status!==400) throw error;
        await readPreview(space,tab);tab.info.reason=error.message;
      }
    } space.tabs.set(path,tab); space.active=path;
    selectFile(space,path);
    if (current === space) { reveal(); paint(); notice(tab.kind!=="text"?"预览不会把文件内容自动发送给模型。":tab.readonly ? "混合换行文件当前只读，避免破坏原换行。" : "输入仅修改草稿；点击保存或按 ⌘S 写入磁盘。"); }
  }
  function reveal() { ui.showFiles(); selectPane("files"); }
  function paint() {
    const space = current, tab = active();
    const signature=JSON.stringify([space?.id,space?.active,space?[...space.tabs.values()].map(t=>[t.path,dirty(t)]):[]]);
    if(signature!==tabSignature) {
      tabSignature=signature;
      $("file-tabs").replaceChildren(...(space ? [...space.tabs.values()].map(item => {
      const wrapper = node("div",null,"file-tab"+(item===tab?" active":""));
      const select = button((dirty(item)?"● ":"")+item.path, () => { selectFile(space,item.path);space.active=item.path; paint(); });
      select.setAttribute("role","tab"); select.setAttribute("aria-selected",String(item===tab));
      wrapper.append(select,button("×",async () => { if (!await guard(space,[item.path])) return; space.tabs.delete(item.path); if (space.active===item.path) space.active=space.tabs.keys().next().value; paint(); }));
      return wrapper;
    }) : []));
    }
    const binary=tab && tab.kind!=="text";
    $("file-preview").hidden=!binary;
    $("workspace-files").classList.toggle("binary-preview",Boolean(binary));
    $("file-path").textContent = tab?.path || "未打开文件";
    $("file-state").textContent = tab ? tab.saving ? "正在保存…" : tab.unavailable ? "位置失效 · 原内容保留" : binary ? "只读预览" : tab.readonly ? "只读：混合换行" : dirty(tab) ? "● 未保存" : "已保存" : "";
    $("file-save").disabled = !tab || binary || tab.readonly || tab.unavailable || !!tab.saving || !dirty(tab);
    $("file-save-as").disabled = !tab || binary;
    $("file-markdown").disabled = !tab || binary || !/\.md$/i.test(tab.path);
    $("file-markdown").textContent = tab?.preview ? "返回编辑" : "Markdown 预览";
    $("file-editor").hidden = !tab || binary || !!tab.preview;
    $("file-markdown-view").hidden = binary || !tab?.preview;
    $("file-conflict").hidden = !tab || !tab.changed;
    $("file-editor").readOnly = !!tab?.readonly;
    if (binary) { preview.show(space,tab); }
    else if (tab) {
      if ($("file-editor").value !== tab.draft) $("file-editor").value = tab.draft;
      if (tab.preview) $("file-markdown-view").innerHTML = renderMarkdown(tab.draft);
    }
  }
  async function readPreview(space,tab) {
    const info=await request(base(space)+"/preview?"+new URLSearchParams({path:tab.path}));
    Object.assign(tab,{kind:info.kind,info,identity:info.identity,version:info.version,readonly:true,unavailable:false});
    tab.page=Math.min(tab.page||1,info.pages||1);
  }
  async function saveTab(space, tab, revision=null) {
    if (tab.saving || tab.readonly || tab.kind!=="text") return false;
    const submitted=tab.draft, content=serialized(tab);
    tab.saving=true; if(current===space) paint();
    try {
      const result = await request(base(space)+"/text","PUT",{path:tab.path,content,version:revision?.version || tab.version,identity:revision?.identity || tab.identity});
      tab.original=normalized(result.content); tab.version=result.version; tab.identity=result.identity;
      tab.newline=result.newline; tab.changed=false; tab.unavailable=false;
      // Further input during the request is still a draft, not marked saved.
      if (tab.draft===submitted) tab.draft=tab.original;
      if(current===space) notice(dirty(tab)?"已保存提交的版本；仍有后续修改待保存。":"已保存到工作区。");
      return !dirty(tab);
    } catch(error) {
      tab.changed=error.status===409; tab.unavailable=error.status===404;
      if(current===space) fail(error); return false;
    } finally { tab.saving=false; if(current===space) paint(); }
  }
  async function saveAs() {
    const space=current,tab=active(); if(!tab || tab.kind!=="text") return;
    const path=await askName("另存为（工作区相对路径；父目录需已存在）",tab.path); if(!path) return;
    const result=await request(base(space)+"/outputs","POST",{path,content:serialized(tab),request_id:crypto.randomUUID()});
    if(result.status==="committed") { await refresh(); await open(space.id,result.output_path); }
    else { notice("同名文件等待确认；原草稿已保留。"); await loadPending(space); }
  }
  async function showDisk() {
    const space=current,tab=active(); if(!tab) return;
    const disk=await request(base(space)+"/text?"+new URLSearchParams({path:tab.path}));
    $("file-disk-content").textContent=`${tab.path}\nSHA256 ${disk.version}\n\n${disk.content}`;
    $("file-disk-dialog").showModal();
  }
  async function overwrite() {
    const space=current,tab=active(); if(!tab) return;
    const disk=await request(base(space)+"/text?"+new URLSearchParams({path:tab.path}));
    const answer=await choose(`将用当前草稿覆盖 ${tab.path} 的最新磁盘版本（${disk.version.slice(0,12)}）。此后文件再次变化仍会拒绝保存。`,[["overwrite","覆盖此版本"],["cancel","取消"]]);
    if(answer==="overwrite") await saveTab(space,tab,disk);
  }
  async function treeDirectory(space,path,target,offset=0) {
    const page=await request(base(space)+"/entries?"+new URLSearchParams({path,offset,hidden:space.hidden}));
    if(current!==space) return;
    if(!offset) target.replaceChildren();
    for(const item of page.items) {
      const wrapper=node("div"), row=node("div",null,"file-tree-row"+(space.selected===item.path?" selected":""));
      const label=button(`${item.type==="directory"?space.dirs.has(item.path)?"▾ ":"▸ ":item.type==="link"?"↗ ":"  "}${item.name}`,async () => {
        space.selected=item.path; space.selection=item;
        if(item.type==="directory") {
          if(space.dirs.has(item.path)) space.dirs.delete(item.path); else space.dirs.add(item.path);
          await renderTree(space);
        } else if(item.type==="file") {
          try { await open(space.id,item.path); }
          catch(error) {
            notice(error.message+"；可下载查看。");
            const link=node("a","下载 "+item.name); link.href=downloadUrl(space.id,item.path); link.download=item.name; $("file-notice").append(link);
          }
        } else notice("符号链接和特殊文件不可打开或修改。");
      });
      label.dataset.path=item.path;label.setAttribute("role","treeitem");label.setAttribute("aria-selected",String(space.selected===item.path));
      if(item.type==="directory") label.setAttribute("aria-expanded",String(space.dirs.has(item.path)));
      row.append(label);wrapper.append(row);target.append(wrapper);
      if(item.type==="directory"&&space.dirs.has(item.path)) {
        const children=node("div",null,"file-tree-children");wrapper.append(children);
        await treeDirectory(space,item.path,children);
      }
    }
    if(page.next_offset!==null) {
      const more=button("加载更多…",async()=>{more.remove();await treeDirectory(space,path,target,page.next_offset);});target.append(more);
    }
    if(!page.items.length&&!offset) target.append(node("p","空目录","muted"));
  }
  async function renderTree(space=current) {
    if(!space || current!==space) return;
    const target=$("file-tree"), children=node("div");
    const root=button("工作区根目录",()=>{space.selected=null;space.selection=null;notice("已选择工作区根目录");renderTree(space).catch(fail);});
    target.replaceChildren(root,children);
    await treeDirectory(space,"",children);
  }
  function selectedDirectory(space) { return space.selection?.type==="directory" ? space.selected : (space.selected || "").split("/").slice(0,-1).join("/"); }
  async function create(kind) {
    const space=current;if(!space) return;
    const parent=selectedDirectory(space), name=await askName(kind==="file"?"新建文件名":"新建文件夹名");if(!name) return;
    if(/[\\/]/.test(name)||name==="."||name==="..") throw new Error("请输入单个文件或文件夹名称");
    const path=parent?parent+"/"+name:name;
    await request(base(space)+"/entries","POST",{path,type:kind,content:""});
    await renderTree(space);if(kind==="file") await open(space.id,path);
  }
  async function mutate(action) {
    const space=current, path=space?.selected;
    if(!path) throw new Error("请先在文件树选择文件或目录");
    if(!await guard(space,[path])) return;
    const parent=path.split("/").slice(0,-1).join("/");
    const listing=await request(base(space)+"/entries?"+new URLSearchParams({path:parent,limit:500,hidden:true}));
    // Selected metadata may have come from a later page. Fetch that page if needed.
    let item=listing.items.find(item=>item.path===path),next=listing.next_offset;
    while(!item&&next!==null) {
      const page=await request(base(space)+"/entries?"+new URLSearchParams({path:parent,offset:next,limit:500,hidden:true}));
      item=page.items.find(item=>item.path===path);next=page.next_offset;
    }
    if(!item) throw new Error("目标已不存在，请刷新");
    let target=null;
    if(action==="rename") {
      const name=await askName("重命名（不覆盖已有目标）",path.split("/").pop());if(!name) return;
      if(/[\\/]/.test(name)||[".",".."].includes(name)) throw new Error("请输入单个名称");
      target=parent?parent+"/"+name:name;
      await request(base(space)+"/rename","POST",{path,target,identity:item.identity});
    } else {
      const choice=await choose(`将 ${path}${item.type==="directory"?" 及其中全部内容":""}移至工作区回收站，可恢复。`,[["delete","移至回收站"],["cancel","取消"]]);
      if(choice!=="delete") return;
      await request(base(space)+"/trash","POST",{path,identity:item.identity});
    }
    for(const [old,tab] of [...space.tabs]) if(old===path||old.startsWith(path+"/")) {
      space.tabs.delete(old);
      if(target) { tab.path=target+old.slice(path.length);space.tabs.set(tab.path,tab);if(space.active===old) space.active=tab.path; }
    }
    if(!space.tabs.has(space.active)) space.active=space.tabs.keys().next().value;
    space.selected=target;space.selection=target?{...item,path:target}:null;space.dirs.clear();paint();await renderTree(space);
  }
  async function trash() {
    const space=current;if(!space) return;
    const result=await request(base(space)+"/trash");
    if(current!==space) return;
    if(!result.items.length) {
      $("file-confirmations").replaceChildren();space.showingTrash=false;space.pendingSignature=null;
      notice("回收站为空。");await loadPending(space);return;
    }
    space.showingTrash=true;
    $("file-confirmations").replaceChildren(...result.items.map(item=>{
      const row=node("div",null,"output-confirmation");row.append(node("p",`${item.path} · ${item.recycled_at}`));
      row.append(button("恢复…",async()=>{
        const target=await askName("恢复到（同名目标不会被覆盖）",item.path);if(!target) return;
        await request(base(space)+`/trash/${item.id}/restore`,"POST",{target});await renderTree(space);await trash();
      }));return row;
    }));
  }
  async function loadPending(space) {
    const result=await request(base(space)+"/outputs");if(current!==space) return;
    const signature=JSON.stringify(result.items);
    if(space.pendingSignature===signature&&!space.showingTrash) return;
    space.pendingSignature=signature;space.showingTrash=false;
    $("file-confirmations").replaceChildren(...result.items.map(item=>{
      const row=node("div",null,"output-confirmation");
      row.append(node("p",item.status==="staged"?`${item.path}：执行结果尚未确认，请检查磁盘；不会自动重试。`:`同名输出：${item.path} · ${item.size} 字节 · 待确认`));
      if(item.status==="pending") {
        const proposal=node("a","下载待写入内容");proposal.href=base(space)+`/outputs/${item.id}/proposal`;proposal.download=item.path.split("/").pop();row.append(proposal);
        row.append(button("查看磁盘",async()=>{
          const disk=await request(base(space)+"/text?"+new URLSearchParams({path:item.path}));
          $("file-disk-content").textContent=item.path+"\n\n"+disk.content;$("file-disk-dialog").showModal();
        }));
        for(const [choice,label] of [["overwrite","覆盖"],["keep-both","保留两份"],["cancel","取消"]]) row.append(button(label,async()=>{
          // Disable this prompt until a new token/version is fetched.
          for(const b of row.querySelectorAll("button")) b.disabled=true;
          try {
            const output=await request(base(space)+`/outputs/${item.id}/decision`,"POST",{token:item.token,choice});
            ui.outputChanged(output);
            notice(choice==="cancel"?"已取消输出，未修改文件。":"输出已保存。");
          } finally { space.pendingSignature=null;await loadPending(space);await renderTree(space);await checkExternal(space); }
        }));
      }
      return row;
    }));
  }
  async function checkExternal(space=current) {
    if(!space) return;
    for(const tab of [...space.tabs.values()]) {
      try {
        if(tab.kind!=="text") {
          const state=await request(base(space)+"/file-state?"+new URLSearchParams({path:tab.path}));
          if(!space.tabs.has(tab.path)) continue;
          tab.unavailable=false;
          if(state.identity!==tab.identity) { await readPreview(space,tab);tab.imageKey=null; }
          continue;
        }
        const result=await request(base(space)+"/text?"+new URLSearchParams({path:tab.path}));
        if(tab.saving||!space.tabs.has(tab.path)) continue;
        tab.unavailable=false;
        if(result.version!==tab.version||result.identity!==tab.identity) {
          if(dirty(tab)) tab.changed=true;
          else takeRead(tab,result);
        }
      } catch(error) { if(error.status===404) tab.unavailable=true; }
    }
    if(current===space) paint();
  }
  async function refresh() {
    const space=current;if(!space||space.refreshing) return;space.refreshing=true;
    try { await renderTree(space);await loadPending(space);await checkExternal(space); }
    finally { space.refreshing=false; }
  }
  function selectPane(name) {
    for(const [key,id] of [["files","workspace-files"],["context","file-context-pane"],["history","file-history-pane"]]) {
      $(id).hidden=key!==name;$("show-"+key).setAttribute("aria-selected",String(key===name));
    }
    if(name==="context") { $("context-panel").open=true;ui.loadContext(); }
  }
  const downloadUrl=(id,path)=>`/api/workspaces/${id}/content?${new URLSearchParams({path})}`;
  const preview=createPreview({active,current:()=>current,readPreview,paint,downloadUrl});
  $("preview-retry").addEventListener("click",()=>preview.retry().catch(fail));
  const layout=createLayout();
  const api_ = {
    async beforeWorkspace(path) { return !current||current.path===path||await guard(current); },
    workspace(workspace) {
      const id=String(workspace.id);
      if(!spaces.has(id)) spaces.set(id,{id,path:workspace.path,tabs:new Map(),active:null,dirs:new Set(),selected:null,selection:null,hidden:false});
      current=spaces.get(id);$("file-tree").replaceChildren(node("p","正在加载…"));$("file-confirmations").replaceChildren();current.pendingSignature=null;$("file-hidden").checked=current.hidden;notice("当前工作区："+workspace.path+"；打开文件后手动保存。");paint();refresh().catch(fail);
    },
    open,refresh,downloadUrl,selectFiles:()=>selectPane("files"), async outputChanged(){await refresh();},
    // Declared below the returned object; called only by the layout code.
    panelsChanged: () => layout.clampAll(),
  };
  $("file-editor").addEventListener("input",()=>{const tab=active();if(!tab) return;tab.draft=$("file-editor").value;paint();});
  $("file-save").addEventListener("click",()=>{const tab=active();if(tab) saveTab(current,tab);});
  $("file-save-as").addEventListener("click",()=>saveAs().catch(fail));
  $("file-markdown").addEventListener("click",()=>{const tab=active();if(tab){tab.preview=!tab.preview;paint();}});
  $("file-view-disk").addEventListener("click",()=>showDisk().catch(fail));
  $("file-overwrite").addEventListener("click",()=>overwrite().catch(fail));
  $("file-disk-close").addEventListener("click",()=>$("file-disk-dialog").close());
  $("file-refresh").addEventListener("click",()=>refresh().catch(fail));
  $("file-new").addEventListener("click",()=>create("file").catch(fail));
  $("folder-new").addEventListener("click",()=>create("directory").catch(fail));
  $("file-rename").addEventListener("click",()=>mutate("rename").catch(fail));
  $("file-delete").addEventListener("click",()=>mutate("delete").catch(fail));
  $("file-trash").addEventListener("click",()=>trash().catch(fail));
  $("file-hidden").addEventListener("change",()=>{if(current){current.hidden=$("file-hidden").checked;renderTree().catch(fail);}});
  $("show-files").addEventListener("click",()=>selectPane("files"));
  $("show-context").addEventListener("click",()=>selectPane("context"));
  $("show-history").addEventListener("click",()=>selectPane("history"));
  window.addEventListener("beforeunload",event=>{if([...spaces.values()].some(space=>[...space.tabs.values()].some(dirty))){event.preventDefault();event.returnValue="";}});
  window.addEventListener("focus",()=>refresh().catch(fail));
  let polling=false;
  setInterval(async()=>{
    if(document.hidden||!current||polling) return;polling=true;
    const space=current;
    try { await checkExternal(space);if(!space.showingTrash) await loadPending(space);await ui.checkPendingUploads(); }
    catch { /* Explicit refresh and save surface errors. */ }
    finally { polling=false; }
  },5000);
  document.addEventListener("keydown",event=>{if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==="s"){event.preventDefault();const tab=active();if(tab) saveTab(current,tab);}});
  paint();
  if (ui.state.currentWorkspace) api_.workspace(ui.state.currentWorkspace);
  return api_;
}
