"""Readonly code must constrain tools and actual child processes together."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from chagent.boundaries import protected_source, validate_workspace
from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.models.base import Model
from chagent.tools.builtin import create_builtin_registry, WRITE_TOOLS
from chagent.tools.command import ExecCommandTool
from chagent.tools.context import ToolContext
from chagent.tools.executor import ToolExecutor
from chagent.domain.messages import ToolCall
from chagent.tools.processes import ProcessManager, ProcessTool
from chagent.tools.sandbox import Sandbox, SandboxSettings, SandboxPolicy, profile
from chagent.tools.workspace import WorkspaceFS
from chagent.web.uploads import save
try:  # `discover -s tests` puts tests/ itself on the path and imports flat names.
    from test_sandbox import DARWIN, SKIP_REASON
except ImportError:  # `-m unittest tests.test_readonly` imports from the package root.
    from tests.test_sandbox import DARWIN, SKIP_REASON


class ReadonlyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / 'ws'
        self.ws.mkdir()
        self.sb = Sandbox(SandboxSettings(mode='isolated', writable_workspace=False))
        self.addCleanup(self.sb.close)

    def test_config_default_strict_and_transmission(self):
        self.assertTrue(Settings.from_env({}).sandbox_writable)
        cfg = Settings.from_env({'CHAGENT_EXECUTION_PROFILE': 'configured',
                                 'CHAGENT_SANDBOX_WRITABLE': 'false'})
        self.assertFalse(Sandbox.from_settings(cfg).settings.writable_workspace)
        for value in ('', '0', 'off', 'yes', 'typo'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings.from_env({'CHAGENT_SANDBOX_WRITABLE': value})

    def test_profiles_remove_workspace_write_and_distinguish_cache_key(self):
        ro = self.sb.policy(self.ws)
        rw = replace(ro, writable_workspace=True)
        self.assertNotEqual(ro, rw)
        self.assertNotEqual(hash(ro), hash(rw))
        ro_write = profile(ro).split('(allow file-write*', 1)[1]
        self.assertNotIn(str(self.ws), ro_write)
        self.assertIn(str(ro.scratch), ro_write)
        legacy = SandboxPolicy(self.ws, writable_tmp=False, writable_workspace=False)
        self.assertNotIn(str(self.ws), profile(legacy))
        self.assertIn(str(self.ws), profile(replace(legacy, writable_workspace=True)))
        with patch('chagent.tools.sandbox._tmp_root', return_value=self.root):
            with self.assertRaises(PermissionError):
                profile(replace(legacy, writable_tmp=True))

    def test_off_audit_does_not_claim_readonly(self):
        sb = Sandbox(SandboxSettings(writable_workspace=False))
        self.assertTrue(sb.record(sb.policy(self.ws))['workspace_writable'])
        self.assertFalse(self.sb.record(self.sb.policy(self.ws))['workspace_writable'])

    def test_scratch_is_run_owned_and_released_independently(self):
        first, second = uuid4(), uuid4()
        a = self.sb.policy(self.ws, owner=first)
        b = self.sb.policy(self.ws, owner=second)
        self.assertNotEqual(a.scratch, b.scratch)
        self.assertEqual(a, self.sb.policy(self.ws, owner=first))
        self.sb.release(first)
        self.assertFalse(a.scratch.exists())
        self.assertTrue(b.scratch.exists())

    def test_environment_is_shared_by_all_launch_paths(self):
        with patch.dict(os.environ, {'SYNTHETIC_SECRET': 'test'}):
            env = self.sb.environment(self.ws, owner=uuid4())
        self.assertNotIn('SYNTHETIC_SECRET', env)
        self.assertEqual(env['GIT_OPTIONAL_LOCKS'], '0')
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')

    def test_direct_registry_cannot_restore_readonly_code_writes(self):
        code = create_builtin_registry(mode='code', sandbox=self.sb, allow_workspace_writes=True,
                                       allow_host_commands=True, processes=ProcessManager())
        self.assertFalse(WRITE_TOOLS & {t.name for t in code.definitions()})
        work = create_builtin_registry(mode='work', sandbox=self.sb, allow_workspace_writes=True)
        self.assertTrue(WRITE_TOOLS <= {t.name for t in work.definitions()})
        process = code.get('process')
        self.assertEqual(process.parameters['properties']['action']['enum'], ['read', 'stop'])
        self.assertNotIn('close_stdin', process.parameters['properties'])

    def test_source_runtime_and_directory_replacement(self):
        for path in (protected_source(), protected_source().parent, Path('/usr/bin')):
            with self.subTest(path=path), self.assertRaises(ValueError):
                WorkspaceFS(path)
        fs = WorkspaceFS(self.ws)
        self.ws.rename(self.root / 'old')
        self.ws.mkdir()
        with self.assertRaises(PermissionError):
            fs.read('missing')

    def test_hardlinked_text_and_document_are_unreadable(self):
        secret = self.root / 'secret'
        secret.write_text('synthetic-secret')
        for name in ('alias.txt', 'alias.docx'):
            os.link(secret, self.ws / name)
            with self.subTest(name=name), self.assertRaises(PermissionError):
                WorkspaceFS(self.ws).read(name)

    def test_single_face_is_not_a_production_model_escape(self):
        with self.assertRaisesRegex(ValueError, 'deterministic'):
            create_runner(MagicMock(), Model('id', 'provider', 'api', 8192, 128), MagicMock(),
                          single_face=True, code_mode=True, allow_host_commands=True)


class ReadonlyToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve() / 'ws'
        self.ws.mkdir()
        self.ctx = ToolContext(self.ws, run_id=uuid4())

    async def test_stdin_and_eof_rejected_before_manager_lookup(self):
        manager = MagicMock()
        tool = ProcessTool(manager, allowed_actions=('read', 'stop'))
        for args in ({'action': 'write', 'process_id': 'id', 'chars': 'bad'},
                     {'action': 'write', 'process_id': 'id', 'close_stdin': True},
                     {'action': 'read', 'process_id': 'id', 'close_stdin': True},
                     {'action': 'unknown', 'process_id': 'id'}):
            with self.subTest(args=args), self.assertRaises(PermissionError):
                await tool.execute('call', args, context=self.ctx)
        manager.get.assert_not_called()

    async def test_search_never_returns_hardlink_or_symlink_content(self):
        secret = self.ws.parent / 'secret'
        secret.write_text('synthetic-secret')
        os.link(secret, self.ws / 'alias')
        (self.ws / 'link').symlink_to(self.ws.parent, target_is_directory=True)
        executor = ToolExecutor(create_builtin_registry(mode='work'))
        for name, args in (('grep', {'pattern': 'synthetic-secret'}),
                           ('glob', {'pattern': '**/*'}),
                           ('glob', {'pattern': 'link/**/*'})):
            result = await executor.execute(ToolCall('c', name, args), context=self.ctx)
            self.assertFalse(result.is_error, result)
            self.assertNotIn('alias', result.content[0].text)
            self.assertNotIn('secret', (result.details or {}).get('paths', []))
        result = await executor.execute(ToolCall('c', 'grep', {'pattern': 'secret', 'path': 'alias'}), context=self.ctx)
        self.assertTrue(result.is_error)

    async def test_upload_symlink_is_rejected_before_body_consumed(self):
        (self.ws / 'uploads').symlink_to(self.ws.parent, target_is_directory=True)
        consumed = False
        async def body():
            nonlocal consumed
            consumed = True
            yield b'new'
        with self.assertRaises(OSError):
            await save(self.ws, 'file.txt', body())
        self.assertFalse(consumed)
        self.assertFalse((self.ws.parent / 'file.txt').exists())

    async def test_executor_refuses_replaced_root_identity(self):
        info = self.ws.stat()
        ctx = replace(self.ctx, workspace_identity=(info.st_dev, info.st_ino))
        self.ws.rename(self.ws.parent / 'old')
        self.ws.mkdir()
        (self.ws / 'file').write_text('new')
        executor = ToolExecutor(create_builtin_registry(mode='work'))
        result = await executor.execute(ToolCall('c', 'read_file', {'path': 'file'}), context=ctx)
        self.assertTrue(result.is_error)
        self.assertIn('replaced', result.content[0].text)


@unittest.skipUnless(DARWIN, SKIP_REASON)
class ReadonlyKernelTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_positive_negative_and_process_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory).resolve()
            sb = Sandbox(SandboxSettings(mode='isolated', writable_workspace=False))
            self.addCleanup(sb.close)
            ctx = ToolContext(ws, run_id=uuid4())
            manager = ProcessManager(wall_timeout=5)
            tool = ExecCommandTool(sb, manager)
            try:
                result = await tool.execute('read', {'cmd': 'printf ok; test "$GIT_OPTIONAL_LOCKS" = 0'}, context=ctx)
                self.assertFalse(result.is_error, result)
                support = await sb.support(sb.policy(ws, owner=ctx.run_id))
                self.assertTrue(support.available, support)
                self.assertTrue(support.checks['workspace_write_errno'])
                result = await tool.execute('write', {'cmd': 'printf bad > denied'}, context=ctx)
                self.assertTrue(result.is_error)
                self.assertFalse((ws / 'denied').exists())
                result = await tool.execute('tmp', {'cmd': 'printf allowed > "$TMPDIR/ok"; cat "$TMPDIR/ok"'}, context=ctx)
                self.assertFalse(result.is_error, result)
                result = await tool.execute('long', {'cmd': 'printf ready; sleep 1', 'yield_time_ms': 100}, context=ctx)
                self.assertFalse(result.is_error, result)
                self.assertNotIn("action='write'", result.content[0].text)
                process = ProcessTool(manager, allowed_actions=('read', 'stop'))
                params = {'process_id': result.details['process_id']}
                stopped = await process.execute('stop', {'action': 'stop', **params}, context=ctx)
                self.assertFalse(stopped.is_error, stopped)
                from chagent.tools.tests_runner import RunTestsTool
                (ws / 'tests').mkdir()
                (ws / 'tests/test_ro.py').write_text(
                    'import errno,os,tempfile,unittest\n'
                    'class Readonly(unittest.TestCase):\n'
                    ' def test_boundary(self):\n'
                    '  self.assertEqual(os.environ["PYTHONDONTWRITEBYTECODE"], "1")\n'
                    '  with self.assertRaises(PermissionError): open("blocked", "w")\n'
                    '  with tempfile.TemporaryFile() as f: f.write(b"ok")\n')
                result = await RunTestsTool(sb).execute('tests', {'suite': 'unittest'}, context=ctx)
                self.assertFalse(result.is_error, result)
                self.assertFalse((ws / 'tests/__pycache__').exists())
                foreign = replace(ctx, run_id=uuid4())
                with self.assertRaises(LookupError):
                    await process.execute('foreign', {'action': 'read', **params}, context=foreign)
                scratch = sb.policy(ws, owner=ctx.run_id).scratch
            finally:
                await manager.terminate_run(ctx.run_id)
                sb.release(ctx.run_id)
            self.assertFalse(scratch.exists())
