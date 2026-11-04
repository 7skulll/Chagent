"""Explicit writable development, independent of optional per-call approval."""
from dataclasses import replace
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock

from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.models.base import Model
from chagent.models.fake import FakeModelClient
from chagent.tools.builtin import WRITE_TOOLS
from chagent.tools.policy import ApprovalPolicy
from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.web.policy import validate_web_runner, validate_web_settings


def development_settings(**changes):
    return Settings(code_mode=True, allow_host_commands=True, sandbox='isolated',
                    execution_profile='trusted-development', **changes)


class DevelopmentTests(unittest.TestCase):
    def runner(self, settings):
        sandbox = Sandbox(SandboxSettings(mode='isolated', writable_workspace=settings.sandbox_writable))
        self.addCleanup(sandbox.close)
        return create_runner(MagicMock(), Model('fake', 'local', 'fake', 8192, 512), FakeModelClient(),
                             code_mode=True, allow_host_commands=True, sandbox=sandbox,
                             allow_workspace_writes=True, code_workspace_writes=settings.trusted_development,
                             process_stdin=False, approval_policy=ApprovalPolicy.from_settings(settings),
                             unified=settings.sandbox_writable)

    def test_profile_is_explicit_and_env_parses_it(self):
        self.assertTrue(Settings().trusted_development)
        cfg = Settings.from_env({'CHAGENT_EXECUTION_PROFILE': 'trusted-development',
            'CHAGENT_CODE_MODE': 'on', 'CHAGENT_ALLOW_HOST_COMMANDS': 'true',
            'CHAGENT_SANDBOX': 'isolated', 'CHAGENT_SANDBOX_WRITABLE': 'true'})
        validate_web_settings(cfg)
        self.assertTrue(cfg.trusted_development)
        self.assertFalse(cfg.readonly_code_available)
        self.assertEqual(cfg.default_mode, 'work')
        with self.assertRaises(ValueError):
            Settings(execution_profile='typo')

    def test_conflicts_fail_without_changing_approval(self):
        good = development_settings()
        for changes in ({'approval':'untrusted'}, {'approval':'on-request'},
                        {'sandbox':'off'}, {'sandbox':'workspace-write'},
                        {'sandbox_network':True}, {'sandbox_writable':False},
                        {'code_mode':False}, {'allow_host_commands':False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(good, **changes)
        self.assertEqual(Settings(execution_profile='configured', approval='untrusted').approval, 'untrusted')

    def test_work_and_code_share_file_writes_without_stdin(self):
        cfg = development_settings()
        runner = self.runner(cfg)
        validate_web_runner(cfg, runner)
        self.assertIs(runner.tool_faces['work'], runner.tool_faces['code'])
        names = lambda mode: {t.name for t in runner.tool_faces[mode].registry.definitions()}
        self.assertEqual(WRITE_TOOLS & names('code'), WRITE_TOOLS)
        self.assertEqual(WRITE_TOOLS & names('work'), WRITE_TOOLS)
        self.assertIn('create_file', names('work'))
        self.assertIn('create_file', names('code'))
        self.assertIn('exec_command', names('work'))
        self.assertIn('run_tests', names('work'))
        self.assertEqual(set(runner.tool_faces['code'].registry.get('process').allowed_actions), {'read','stop'})
        self.assertFalse(runner.scheduler.policy.requires_approval('exec_command', {'cmd':'echo test'}, sandbox='isolated'))

    def test_prompt_claims_execution_only_for_explicit_unified_runner(self):
        cfg = development_settings()
        unified = self.runner(cfg)
        self.assertEqual(unified.prompt_mode_for('work'), 'unified')
        sandbox = unified.sandbox
        work_only = create_runner(MagicMock(), Model('fake', 'local', 'fake', 8192, 512),
                                  FakeModelClient(), sandbox=sandbox, code_mode=False)
        self.assertEqual(work_only.prompt_mode_for('work'), 'work')
        self.assertIsNone(work_only.executor_for('work').registry.get('exec_command'))

    def test_unified_rejects_disabled_code_switch(self):
        sandbox = Sandbox(SandboxSettings(mode='isolated'))
        self.addCleanup(sandbox.close)
        with self.assertRaisesRegex(ValueError, 'CHAGENT_CODE_MODE=on'):
            create_runner(MagicMock(), Model('fake', 'local', 'fake', 8192, 512),
                          FakeModelClient(), sandbox=sandbox, allow_host_commands=True,
                          code_mode=False, unified=True)

    def test_unified_cannot_claim_missing_execution_tools(self):
        sandbox = Sandbox(SandboxSettings(mode='isolated'))
        self.addCleanup(sandbox.close)
        for changes in ({'allow_host_commands': False}, {'allow_workspace_writes': False},
                        {'process_stdin': True}):
            options = {'allow_host_commands': True, 'allow_workspace_writes': True,
                       'process_stdin': False, **changes}
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'Unified execution requires'):
                create_runner(MagicMock(), Model('fake', 'local', 'fake', 8192, 512),
                              FakeModelClient(), sandbox=sandbox, code_mode=True,
                              unified=True, **options)

    def test_runner_mismatches_are_rejected(self):
        cfg = development_settings()
        runner = self.runner(cfg)
        runner.scheduler.policy = ApprovalPolicy('untrusted')
        with self.assertRaisesRegex(ValueError, '审批'):
            validate_web_runner(cfg, runner)
        runner.scheduler.policy = ApprovalPolicy()
        runner.tool_faces['code'].registry.get('exec_command').sandbox = None
        with self.assertRaisesRegex(ValueError, '沙箱'):
            validate_web_runner(cfg, runner)
        runner = self.runner(cfg)
        runner.tool_faces['code'].registry.get('process').allowed_actions = ('read','write','stop')
        with self.assertRaisesRegex(ValueError, 'read/stop'):
            validate_web_runner(cfg, runner)
        runner = self.runner(replace(cfg, execution_profile='configured', sandbox_writable=False))
        with self.assertRaises(ValueError):
            validate_web_runner(cfg, runner)

    def test_workspace_writer_checks_boundaries_and_reports_diff(self):
        from chagent.tools.workspace import WorkspaceFS
        from chagent.tools.filesystem import WriteFileTool, EditFileTool
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fs = WorkspaceFS(root)
            WriteFileTool().handle(fs, {'path':'module.py', 'content':'value = 1\n'})
            result = EditFileTool().handle(fs, {'path':'module.py', 'edits':[{'old_text':'value = 1','new_text':'value = 2'}]})
            self.assertEqual((root/'module.py').read_text(), 'value = 2\n')
            self.assertIn('+value = 2', result.content[0].text)
            with self.assertRaises((ValueError, PermissionError)):
                WriteFileTool().handle(fs, {'path':'../escape.txt','content':'no'})


class DevelopmentPythonTests(unittest.IsolatedAsyncioTestCase):
    async def test_isolated_tests_import_workspace_and_ignore_stale_bytecode(self):
        try:
            from test_sandbox import SEATBELT_READY, SKIP_REASON
        except ImportError:
            from tests.test_sandbox import SEATBELT_READY, SKIP_REASON
        if not SEATBELT_READY:
            self.skipTest(SKIP_REASON)
        import os
        import py_compile
        from uuid import uuid4
        from chagent.tools.context import ToolContext
        from chagent.tools.tests_runner import RunTestsTool
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            module = root/'value.py'
            module.write_text('value = 1\n')
            stamp = module.stat().st_mtime_ns
            py_compile.compile(str(module), doraise=True)
            # Force a valid-looking stale pyc: same size and source timestamp.
            module.write_text('value = 2\n')
            os.utime(module, ns=(stamp, stamp))
            (root/'tests').mkdir()
            (root/'tests/test_value.py').write_text(
                'import unittest\nfrom value import value\nclass TestValue(unittest.TestCase):\n'
                ' def test_value(self): self.assertEqual(value, 2)\n')
            sandbox = Sandbox(SandboxSettings(mode='isolated'))
            self.addCleanup(sandbox.close)
            result = await RunTestsTool(sandbox).execute('test', {'suite':'unittest'}, context=ToolContext(root, run_id=uuid4()))
            self.assertFalse(result.is_error, result.content[0].text)
            self.assertEqual(result.details['counts']['passed'], 1)
