"""R4: actual kernel checks on synthetic data; no model requests or credentials."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from uuid import uuid4

from chagent.tools.command import ExecCommandTool
from chagent.tools.context import ToolContext
from chagent.tools.processes import ProcessManager, ProcessTool
from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.tools.tests_runner import RunTestsTool


def snapshot(root):
    result = {}
    for p in sorted(root.rglob('*')):
        info = p.lstat()
        result[str(p.relative_to(root))] = dict(mode=info.st_mode, size=info.st_size,
            inode=info.st_ino, links=info.st_nlink, mtime=info.st_mtime_ns,
            content=sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else '')
    return result


async def main(output):
    checks = {}
    with tempfile.TemporaryDirectory(prefix='chagent-readonly-r4-') as temporary:
        base = Path(temporary).resolve()
        ws = base / 'workspace'; ws.mkdir()
        (ws / 'README.md').write_text('original\n')
        (ws / '.gitignore').write_text('.ignored\n')
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        for args in (['init','-q'], ['add','.'], ['-c','user.name=Probe','-c','user.email=probe@example.invalid',
                     '-c','commit.gpgsign=false','commit','-qm','synthetic baseline']):
            subprocess.run(['/usr/bin/git',*args],cwd=ws,env=env,check=True,capture_output=True)
        (ws / 'README.md').write_text('changed\n')
        (ws / '.ignored').write_text('ignored marker\n')
        (ws / 'tests').mkdir()
        (ws / 'tests/test_smoke.py').write_text('import unittest, tempfile\nclass Smoke(unittest.TestCase):\n def test_tmp(self):\n  with tempfile.TemporaryFile() as f: f.write(b"ok")\n')
        ctx = ToolContext(ws, run_id=uuid4())
        sb = Sandbox(SandboxSettings(mode='isolated', writable_workspace=False))
        manager = ProcessManager(wall_timeout=2)
        try:
            support = await sb.support(sb.policy(ws, owner=ctx.run_id))
            checks.update({f'isolation/{k}':v for k,v in support.checks.items()})
            checks['isolation/available'] = support.available
            if not support.available:
                raise RuntimeError('kernel preflight failed: '+support.detail)
            tool = ExecCommandTool(sb,manager)
            async def command(name, cmd, *, denied=False):
                result = await tool.execute(name,{'cmd':cmd,'timeout_ms':30000},context=ctx)
                text = '\n'.join(c.text for c in result.content)
                checks[name] = (result.is_error and 'Operation not permitted' in text if denied else not result.is_error)
                return result
            before = snapshot(ws)
            for name,cmd in [('shell','printf alive'),('read','cat README.md'),('grep','grep changed README.md'),
                             ('git_status','git status --porcelain'),('git_log','git log --oneline -1'),
                             ('git_diff','git diff -- README.md'),('python','python3 -I -c "print(1)"'),
                             ('scratch','printf ok > "$TMPDIR/probe"; cat "$TMPDIR/probe"')]:
                await command('positive/'+name,cmd)
            for name,cmd in [('redirect','printf bad > new'),('touch','touch README.md'),
                ('tee','printf bad | tee new'),('sed',"sed -i '' s/changed/bad/ README.md"),
                ('checkout','git checkout -- README.md'),('delete','rm README.md'),('rename','mv README.md moved'),
                ('mkdir','mkdir newdir'),('chmod','chmod 000 README.md'),
                ('symlink','ln -s README.md link'),('hardlink','ln README.md hardlink')]:
                await command('denied/'+name,cmd,denied=True)
                checks['unchanged/'+name] = snapshot(ws)==before
            operations = dict(create="os.open('new', os.O_CREAT|os.O_WRONLY, 0o600)",
                overwrite="os.open('README.md', os.O_TRUNC|os.O_WRONLY)", delete="os.unlink('README.md')",
                rename="os.rename('README.md','moved')", mkdir="os.mkdir('newdir')",
                chmod="os.chmod('README.md',0o600)", symlink="os.symlink('README.md','link')",
                hardlink="os.link('README.md','hardlink')")
            for name,operation in operations.items():
                code = "import os,errno\nprint('started',flush=True)\ntry: "+operation+"\nexcept OSError as e:\n print('errno='+str(e.errno))\n raise SystemExit(0 if e.errno==errno.EPERM else 2)\nraise SystemExit(3)"
                result = await command('errno/'+name,'python3 -I -c '+shlex.quote(code))
                checks['errno/'+name] &= 'started\nerrno=1' in result.details['output']
                checks['unchanged/errno_'+name] = snapshot(ws)==before
            result=await RunTestsTool(sb).execute('unittest',{'suite':'unittest'},context=ctx)
            checks['positive/unittest']=not result.is_error and result.details['counts'].get('passed',0)>0
            # pytest is installed in the declared base runtime; never grant a new read root.
            result=await RunTestsTool(sb).execute('pytest',{'suite':'pytest'},context=ctx)
            checks['positive/pytest']=not result.is_error and result.details['counts'].get('passed',0)>0
            checks['unchanged/tests']=snapshot(ws)==before
            proc=await tool.execute('long',{'cmd':'printf ready; sleep 1','yield_time_ms':100},context=ctx)
            process=ProcessTool(manager,allowed_actions=('read','stop'))
            pid=proc.details.get('process_id')
            checks['process/start']=bool(pid)
            if pid:
                read=await process.execute('read',{'action':'read','process_id':pid},context=ctx)
                checks['process/read']='ready' in read.details['output']
                stop=await process.execute('stop',{'action':'stop','process_id':pid},context=ctx)
                checks['process/stop']=not stop.is_error
                for name,args in [('write',{'chars':'bad'}),('eof',{'close_stdin':True})]:
                    try:
                        await process.execute(name,{'action':'write','process_id':pid,**args},context=ctx)
                    except PermissionError:
                        checks['process/'+name+'_denied']=True
                    else:
                        checks['process/'+name+'_denied']=False
            first=sb.policy(ws,owner=ctx.run_id)
            (first.scratch/'home/marker').write_text('synthetic-private')
            other_tool=ExecCommandTool(sb)
            other_ctx=ToolContext(ws,run_id=uuid4())
            # Use exactly the owner whose scratch is tested.
            foreign=await other_tool.execute('foreign',{'cmd':'cat '+shlex.quote(str(first.scratch/'home/marker'))},context=other_ctx)
            checks['scratch/cross_run_read_denied']=foreign.is_error and 'synthetic-private' not in foreign.details.get('output','')
            checks['scratch/distinct']=first.scratch != sb.policy(ws,owner=other_ctx.run_id).scratch
            timeout=await tool.execute('timeout',{'cmd':'sleep 10','timeout_ms':100},context=ctx)
            checks['process/timeout']=timeout.is_error and timeout.details['timed_out']
            cancel=asyncio.create_task(tool.execute('cancel',{'cmd':'sleep 10'},context=ctx))
            await asyncio.sleep(.1); cancel.cancel()
            try: await cancel
            except asyncio.CancelledError: checks['process/cancel']=True
            else: checks['process/cancel']=False
            await manager.terminate_run(ctx.run_id)
            checks['process/reaped']=not manager.owned(ctx.run_id)
            sb.release(ctx.run_id)
            checks['scratch/cleanup']=not first.scratch.exists()
            checks['unchanged/final']=snapshot(ws)==before
        finally:
            await manager.terminate_run(ctx.run_id)
            sb.close()
    data=dict(recorded_at_utc=datetime.now(timezone.utc).isoformat(),scope='R4 synthetic kernel and tool acceptance',
              checks=checks,passed=bool(checks) and all(checks.values()))
    output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':data['passed'],'checks':len(checks),'failed':[k for k,v in checks.items() if not v]}))
    return data['passed']

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.output)) else 1)
