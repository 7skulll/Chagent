"""Real-model F3/F4 file workflow probe in a disposable schema and workspace.

Uses configured credentials without printing them. Sends only synthetic task
content. No production sessions/files are modified. Runs both work and writable
code with bounded turns; code commands remain isolated and offline.
"""
import asyncio
import httpx
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.config.dotenv import load_missing
from chagent.domain.memories import MemoryPolicy
from chagent.models.factory import create_model_client
from chagent.models.settings import ModelSettings
from chagent.outputs import OutputService
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.tools.sandbox import Sandbox, SandboxSettings


async def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--http-lifecycle',action='store_true',help='Also validate upload/save/rename/trash/restore/runtime recovery via HTTP')
    args=parser.parse_args()
    load_missing(Path(__file__).resolve().parents[1]/'.env')
    settings=Settings.from_env()
    selected=replace(ModelSettings.from_env(),max_tokens=2048,run_timeout=180)
    report={'at':datetime.now(timezone.utc).isoformat(),'provider':selected.provider,'model':selected.model_id,'modes':[]}
    schema='chagent_f34_real_'+uuid4().hex
    async with await AsyncConnection.connect(settings.database_url,autocommit=True) as admin:
        await admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            with tempfile.TemporaryDirectory(prefix='chagent-f34-real-') as folder:
                root=Path(folder).resolve();(root/'uploads').mkdir()
                async with Database(replace(settings,database_url=make_conninfo(settings.database_url,options=f'-c search_path={schema}'))) as database:
                    await migrate(database)
                    client=create_model_client(selected)
                    sandbox=Sandbox(SandboxSettings(mode='isolated',writable_workspace=True,network=False))
                    runner=create_runner(database,selected.model(),client,code_mode=True,allow_host_commands=True,
                        allow_workspace_writes=True,code_workspace_writes=True,versioned_files=True,process_stdin=False,
                        sandbox=sandbox,max_turns=12,timeout=180,memory_policy=MemoryPolicy(enabled=False))
                    from chagent.web.app import WebRuntime,create_app
                    web_settings=Settings(code_mode=True,allow_host_commands=True,sandbox='isolated',execution_profile='trusted-development')
                    runtime=WebRuntime(runner,root,web_settings)
                    app=create_app(workspace=root,injected_runtime=runtime);app.state.runtime=runtime
                    http=httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1',trust_env=False)
                    async def api(method,url,**kwargs):
                        response=await http.request(method,url,**kwargs);response.raise_for_status();return response.json()
                    workspace=await api('POST','/api/workspaces',json={'path':str(root)})
                    base='/api/workspaces/'+workspace['id']
                    try:
                        for mode in ['work','code']:
                            note=f'uploads/{mode}.txt';marker='UPDATED_'+mode.upper()
                            if args.http_lifecycle:
                                await api('POST','/api/uploads?name='+mode+'.txt',content=b'PLACEHOLDER\n')
                            else:
                                (root/note).write_text('PLACEHOLDER\n')
                            sid=await runner.conversations.create_session(root,title='F34 synthetic '+mode,mode=mode)
                            session=await runner.conversations.get_session(sid)
                            prompt=(f'这是合成文件工具验收。请先用 read_file 读取 {note}，再用 edit_file 携带读取的 expected_sha256 '
                                    f'将 PLACEHOLDER 改成 {marker}。然后用 create_directory 新建 outputs_{mode} 目录，'
                                    f'再用 create_file 在 outputs_{mode}/result.md 生成内容为 # {marker} 的 Markdown 文件。'
                                    '不要执行 Shell 命令。必须实际调用工具并检查成功，不要仅描述方案。')
                            print('Real file probe: '+mode,flush=True)
                            result=await runner.run(sid,prompt)
                            output_path=f'outputs_{mode}/result.md'
                            item={'mode':mode,'edit_run':str(result.run_id),'edited_upload':(root/note).read_text()==marker+'\n',
                                  'output_exists':(root/output_path).is_file()}
                            if args.http_lifecycle:
                                read=await api('GET',base+'/text',params={'path':note})
                                await api('PUT',base+'/text',json={key:read[key] for key in ['path','version','identity']}|{'content':marker+'\nMANUAL\n'})
                                item['manual_save']=(root/note).read_text()==marker+'\nMANUAL\n'
                            original=(root/output_path).read_bytes() if item['output_exists'] else b''
                            collision=await runner.run(sid,f'继续验收：只用 create_file 在 {output_path} 生成新内容 # PROPOSED_{mode}。这是同名输出，等待宿主确认，不得使用其他工具覆盖现有文件。')
                            rows=await OutputService(database).pending(session.workspace_id)
                            pending=next((row for row in rows if row['path']==output_path and row['status']=='pending'),None)
                            item.update(collision_run=str(collision.run_id),pending_confirmation=pending is not None,
                                        original_preserved=(root/output_path).read_bytes()==original)
                            if pending:
                                if args.http_lifecycle:
                                    saved=await api('POST',base+'/outputs/'+str(pending['id'])+'/decision',json={'token':str(pending['token']),'choice':'keep-both'})
                                else:
                                    saved=await OutputService(database).decide(session.workspace_id,root,pending['id'],pending['token'],'keep-both')
                                item['keep_both_saved']=(root/saved['output_path']).is_file()
                            else:
                                item['keep_both_saved']=False
                            item['passed']=all(item[key] for key in ['edited_upload','output_exists','pending_confirmation','original_preserved','keep_both_saved'])
                            if args.http_lifecycle:
                                read=await api('GET',base+'/text',params={'path':note});target=note.replace('.txt','-renamed.txt')
                                await api('POST',base+'/rename',json={'path':note,'target':target,'identity':read['identity']})
                                moved=await api('GET',base+'/text',params={'path':target})
                                deleted=await api('POST',base+'/trash',json={'path':target,'identity':moved['identity']})
                                await api('POST',base+'/trash/'+deleted['id']+'/restore',json={})
                                app.state.runtime=WebRuntime(runner,root,web_settings)
                                opened=await api('POST','/api/workspaces',json={'path':str(root)})
                                history=await api('GET','/api/sessions/'+str(sid))
                                item['lifecycle_recovered']=opened['id']==workspace['id'] and history['id']==str(sid) and (root/target).read_text()==marker+'\nMANUAL\n'
                                item['passed']=item['passed'] and item['manual_save'] and item['lifecycle_recovered']
                            report['modes'].append(item)
                            print(mode+': '+('passed' if item['passed'] else 'failed'),flush=True)
                    finally:
                        await http.aclose();await client.aclose();sandbox.close()
        finally:
            await admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if len(report['modes'])!=2 or not all(item['passed'] for item in report['modes']):
        raise SystemExit('Real workflow probe incomplete or failed; see report')


if __name__=='__main__':
    asyncio.run(main())
