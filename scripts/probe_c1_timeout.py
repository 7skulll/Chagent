import asyncio,json,os,tempfile,time
from pathlib import Path
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.context import ToolContext
from chagent.tools.executor import ToolExecutor
from chagent.domain.messages import ToolCall

async def run_case(label, seconds, ceiling, timeout_ms):
    with tempfile.TemporaryDirectory(prefix='chagent-c1-') as d:
        root=Path(d); (root/'tests').mkdir()
        (root/'tests'/'test_wait.py').write_text('import time, unittest\nclass Wait(unittest.TestCase):\n def test_wait(self):\n  time.sleep('+str(seconds)+')\n')
        os.environ['CHAGENT_TOOL_TIMEOUT_CEILING']=str(ceiling)
        executor=ToolExecutor(create_builtin_registry(mode='code',allow_host_commands=True),allow_host_commands=True)
        start=time.monotonic()
        result=await executor.execute(ToolCall(label,'run_tests',{'suite':'unittest','timeout_ms':timeout_ms}),context=ToolContext(root))
        return {'case':label,'seconds':round(time.monotonic()-start,2),'error':result.is_error,'outcome_unknown':result.outcome_unknown,'text':result.content[0].text,'details':result.details}
async def main():
    results=await asyncio.gather(run_case('120s_complete',120,310,140000),run_case('200s_ceiling',200,125,250000))
    Path('docs/diagnostics/c1-timeout-2026-11-02.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))
asyncio.run(main())
