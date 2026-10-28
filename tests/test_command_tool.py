import asyncio
from pathlib import Path
import os
import shlex
import sys
import tempfile
import time
import unittest

from chagent.domain.messages import ToolCall
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.command import ExecCommandTool
from chagent.tools.context import ToolContext
from chagent.tools.executor import ToolExecutor


@unittest.skipUnless(os.name == "posix", "POSIX command tool")
class CommandToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = ToolContext(self.root)
        self.executor = ToolExecutor(create_builtin_registry(allow_host_commands=True), allow_host_commands=True)

    def python(self, source):
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"

    async def call(self, **arguments):
        return await self.executor.execute(ToolCall("cmd", "exec_command", arguments), context=self.context)

    async def test_host_execution_requires_trusted_enablement(self):
        self.assertIsNone(create_builtin_registry().get("exec_command"))
        executor = ToolExecutor(create_builtin_registry(allow_host_commands=True))
        result = await executor.execute(ToolCall("cmd", "exec_command", {"cmd": "printf should-not-run"}), context=self.context)
        self.assertTrue(result.is_error)
        self.assertIn("disabled", result.content[0].text)

    async def test_cwd_exit_status_stderr_and_output_limit(self):
        (self.root / "sub").mkdir()
        result = await self.call(cmd="pwd; printf error >&2; exit 7", workdir="sub")
        self.assertTrue(result.is_error)
        self.assertEqual(result.details["exit_code"], 7)
        self.assertIn("error", result.details["output"])
        self.assertEqual(Path(result.details["workdir"]), (self.root / "sub").resolve())
        result = await self.call(cmd=self.python("print('x' * 200000)"), max_output_chars=128)
        self.assertFalse(result.is_error)
        self.assertTrue(result.details["truncated"])
        self.assertLess(len(result.details["output"]), 200)
        self.assertTrue((await self.call(cmd="pwd", workdir="..")).is_error)

    async def test_timeout_kills_descendants(self):
        # A surviving child would create this marker after the tool has returned.
        source = "import time; from pathlib import Path; time.sleep(0.4); Path('orphan').write_text('bad')"
        started = time.monotonic()
        result = await self.call(cmd=self.python(source), timeout_ms=50)
        self.assertTrue(result.is_error)
        self.assertTrue(result.outcome_unknown)
        self.assertTrue(result.details["timed_out"])
        self.assertLess(time.monotonic() - started, 2)
        await asyncio.sleep(0.5)
        self.assertFalse((self.root / "orphan").exists())

    async def test_cancellation_drains_output_and_kills_process_group(self):
        ready = asyncio.Event()
        source = "import time; from pathlib import Path; print('ready', flush=True); time.sleep(0.4); Path('cancel-orphan').write_text('bad')"
        task = asyncio.create_task(ExecCommandTool().execute(
            "cmd", {"cmd": self.python(source)}, context=self.context,
            on_update=lambda result: ready.set(),
        ))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await asyncio.sleep(0.5)
        self.assertFalse((self.root / "cancel-orphan").exists())
