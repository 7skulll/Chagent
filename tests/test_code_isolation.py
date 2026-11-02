"""Isolated code execution: never treat a skipped backend as a passing boundary."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from chagent.tools.sandbox import (Sandbox, SandboxSettings, DisabledBackend, SeatbeltBackend,
                                   isolated_environment, isolated_profile)
from chagent.tools.command import ExecCommandTool
from chagent.tools.context import ToolContext
from chagent.tools.processes import ProcessManager
from test_sandbox import DARWIN, SKIP_REASON


class IsolationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.sandbox = Sandbox(SandboxSettings(mode='isolated'))
        self.addCleanup(self.sandbox.close)

    def test_profile_and_environment_are_closed_by_default(self):
        policy = self.sandbox.policy(self.workspace)
        text = isolated_profile(policy)
        self.assertIn('(deny default)', text)
        self.assertNotIn('(allow default)', text)
        self.assertNotIn('(allow file-read*)', text)
        self.assertIn('(literal "/")', text)
        self.assertNotIn('(subpath "/")', text)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'synthetic', 'PYTHONPATH': '/outside', 'DYLD_INSERT_LIBRARIES': '/outside'}):
            env = self.sandbox.environment(self.workspace)
        for name in ('DEEPSEEK_API_KEY', 'PYTHONPATH', 'DYLD_INSERT_LIBRARIES'):
            self.assertNotIn(name, env)
        self.assertNotEqual(env['HOME'], str(Path.home()))
        self.assertEqual(Path(env['HOME']).stat().st_mode & 0o777, 0o700)
        self.assertEqual(env, isolated_environment(policy))

    def test_network_or_disabled_backend_cannot_weaken_isolation(self):
        with self.assertRaises(ValueError):
            SandboxSettings(mode='isolated', network=True)
        with self.assertRaises(ValueError):
            Sandbox(SandboxSettings(mode='isolated'), DisabledBackend())

    def test_source_runtime_and_existing_hardlinks_are_rejected(self):
        from chagent.web.workspaces import protected_source
        for path in (protected_source(), Path('/usr/bin')):
            with self.assertRaises((ValueError, PermissionError)):
                self.sandbox.policy(path)
        original = self.workspace / 'first'
        original.write_text('synthetic')
        os.link(original, self.workspace / 'second')
        with self.assertRaisesRegex(PermissionError, 'hard-linked'):
            self.sandbox.policy(self.workspace)

    def test_missing_backend_fails_closed(self):
        self.sandbox.backend = SeatbeltBackend('/missing/chagent-sandbox')
        with self.assertRaises(PermissionError):
            asyncio.run(self.sandbox.wrap(['/bin/echo', 'must-not-start'], self.workspace))

    def test_model_code_face_requires_isolation(self):
        from chagent.bootstrap import _tool_faces
        with self.assertRaisesRegex(ValueError, 'isolated'):
            _tool_faces(None, code_mode=True, allow_host_commands=True, sandbox=None,
                        processes=ProcessManager(), web=None, include_echo=False)


@unittest.skipUnless(DARWIN, SKIP_REASON)
class IsolatedExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.sandbox = Sandbox(SandboxSettings(mode='isolated'))
        self.addCleanup(self.sandbox.close)
        self.context = ToolContext(self.workspace, run_id=uuid4())

    async def test_positive_and_negative_controls_are_required(self):
        support = await self.sandbox.support(self.sandbox.policy(self.workspace))
        self.assertTrue(support.available, support)
        self.assertGreaterEqual(len(support.checks), 13)
        self.assertTrue(all(support.checks.values()), support)

    async def test_one_shot_test_runner_and_session_process_share_isolation(self):
        from chagent.tools.tests_runner import RunTestsTool
        manager = ProcessManager(wall_timeout=5)
        tool = ExecCommandTool(self.sandbox, manager)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "synthetic-only", "PYTHONPATH": "/synthetic-outside"}):
            run = await tool.execute('one', {'cmd': 'test -z "$DEEPSEEK_API_KEY" && test -z "$PYTHONPATH" && printf clean'}, context=self.context)
        self.assertFalse(run.is_error, run)
        self.assertIn('clean', run.content[0].text)
        run = await tool.execute('long', {'cmd': 'printf before; sleep 0.2; printf after', 'yield_time_ms': 10}, context=self.context)
        record = manager._records[run.details['process_id']]
        try:
            await asyncio.wait_for(record.finished.wait(), 3)
            self.assertEqual(record.exit_code, 0)
            self.assertEqual(record.page(0)[0], 'beforeafter')
        finally:
            await manager.terminate_run(self.context.run_id)
        (self.workspace / 'tests').mkdir()
        (self.workspace / 'tests/test_env.py').write_text(
            'import os, unittest\nclass Env(unittest.TestCase):\n def test_env(self): self.assertNotIn("PYTHONPATH", os.environ)\n')
        run = await RunTestsTool(self.sandbox).execute('tests', {'suite': 'unittest'}, context=self.context)
        self.assertFalse(run.is_error, run)
