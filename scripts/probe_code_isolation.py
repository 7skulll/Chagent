"""Run the shipped isolated backend, saving no credentials or command output."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.tools.command import ExecCommandTool
from chagent.tools.context import ToolContext
from chagent.tools.processes import ProcessManager
from chagent.tools.tests_runner import RunTestsTool
from uuid import uuid4


async def main(output):
    with tempfile.TemporaryDirectory(prefix='chagent-code-acceptance-') as directory:
        workspace = Path(directory).resolve()
        sandbox = Sandbox(SandboxSettings(mode='isolated'))
        support = await sandbox.support(sandbox.policy(workspace))
        result = {'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
                  'support': asdict(support), 'tools': {}}
        if support.available:
            context = ToolContext(workspace, run_id=uuid4())
            manager = ProcessManager(wall_timeout=10)
            tool = ExecCommandTool(sandbox, manager)
            for name, cmd in [('shell', 'printf shell-ok'), ('git', 'git init -q . && git status --porcelain'),
                              ('environment', 'test -d "$HOME" && test "$HOME" != /Users/macbook && test -z "$DEEPSEEK_API_KEY" && test -z "$PYTHONPATH"')]:
                run = await tool.execute('probe', {'cmd': cmd}, context=context)
                result['tools'][name] = {'passed': not run.is_error, 'exit_code': run.details['exit_code']}
            (workspace / 'tests').mkdir()
            (workspace / 'tests/test_smoke.py').write_text('import unittest\nclass Smoke(unittest.TestCase):\n def test_sum(self): self.assertEqual(2+2,4)\n')
            run = await RunTestsTool(sandbox).execute('test', {'suite': 'unittest'}, context=context)
            result['tools']['run_tests'] = {'passed': not run.is_error, 'exit_code': run.details['exit_code']}
            run = await tool.execute('long', {'cmd': 'printf started; sleep 0.3; printf finished', 'yield_time_ms': 10}, context=context)
            pid = run.details.get('process_id')
            if pid:
                record = manager._records[pid]
                await asyncio.wait_for(record.finished.wait(), 5)
                text, _ = record.page(0)
                result['tools']['session_process'] = {'passed': record.exit_code == 0 and text == 'startedfinished'}
                await manager.terminate_run(context.run_id)
        result['passed'] = support.available and all(row['passed'] for row in result['tools'].values())
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.output)) else 1)
