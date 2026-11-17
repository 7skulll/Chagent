"""Admission rejects policy drift; real Docker tests use only disposable resources."""
import asyncio
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from chagent.tools.container_policy import ContainerPolicy
from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.tools.command import ExecCommandTool
from chagent.tools.context import ToolContext


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ContainerPolicy()
        self.root = Path('/tmp/policy-workspace')
        key = self.policy.identity(self.root)
        self.details = {
            'Id': 'container-id', 'Image': 'sha256:actual', 'Created': 'now',
            'Config': {'Image': 'chagent:local', 'Labels': {'chagent.workspace': key},
                       'Entrypoint': ['/bin/sleep'], 'Cmd': ['infinity'], 'User': ''},
            'State': {'Running': True},
            'Mounts': [{'Type': 'bind', 'Source': str(self.root), 'Destination': str(self.root), 'RW': True},
                       {'Type': 'volume', 'Name': 'chagent-cache-' + key[:20], 'Destination': '/cache', 'RW': True}],
            'HostConfig': {'ReadonlyRootfs': True, 'NetworkMode': 'bridge', 'IpcMode': 'private',
                           'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges'],
                           'Memory': self.policy.memory, 'NanoCpus': self.policy.nano_cpus,
                           'PidsLimit': self.policy.pids, 'Tmpfs': {'/tmp': 'rw,nosuid,size=512m'},
                           'PortBindings': {'18080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '12345'}]}}}

    def test_policy_accepts_expected_inspect_and_rejects_each_drift(self):
        self.assertEqual(self.policy.differences(self.details, self.root, 'chagent:local'), [])
        for section, field, value, reason in [
            ('HostConfig', 'NanoCpus', 64_000_000_000, 'cpu'),
            ('HostConfig', 'Memory', 0, 'memory'),
            ('HostConfig', 'PidsLimit', -1, 'pids'),
            ('HostConfig', 'Tmpfs', {}, 'tmpfs'),
            ('HostConfig', 'CapAdd', ['SYS_ADMIN'], 'capabilities'),
            ('HostConfig', 'NetworkMode', 'host', 'namespaces'),
            ('HostConfig', 'ReadonlyRootfs', False, 'readonly_root'),
            ('Config', 'Labels', {}, 'workspace_label'),
            ('Config', 'Entrypoint', ['/bin/sh'], 'entrypoint'),
        ]:
            with self.subTest(reason=reason):
                details = deepcopy(self.details)
                details[section][field] = value
                self.assertIn(reason, self.policy.differences(details, self.root, 'chagent:local'))
        self.details['Mounts'][1]['Name'] = 'another-workspace-cache'
        self.assertIn('cache_volume', self.policy.differences(self.details, self.root, 'chagent:local'))

    def test_creation_and_manifest_share_limits_and_actual_image(self):
        args = self.policy.create_args(self.root, 'custom:tag')
        self.assertEqual(args[args.index('--memory') + 1], str(self.policy.memory))
        self.assertEqual(args[args.index('--cpus') + 1], str(self.policy.nano_cpus / 1e9))
        manifest = self.policy.manifest(self.details, self.root)
        self.assertEqual(manifest['image_id'], 'sha256:actual')
        self.assertEqual(manifest['policy_sha256'], self.policy.digest)

    def test_failed_cleanup_blocks_scratch_release(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
            sandbox._run_containers['run'] = 'container'
            with patch.object(sandbox, '_docker', AsyncMock(return_value=(1, 'missing worker'))):
                with self.assertRaisesRegex(RuntimeError, 'cleanup_pending'):
                    await sandbox.finish_invocation({'container': 'container', 'invocation': 'call', 'control_path': '/tmp/call'})
                with self.assertRaisesRegex(RuntimeError, 'cleanup_pending'):
                    await sandbox.release('run')
        asyncio.run(check())


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DOCKER') == '1', 'Set CHAGENT_TEST_DOCKER=1')
class DockerInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chagent-boundary-')
        self.root = Path(self.tmp.name).resolve()
        self.sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
        self.name = await self.sandbox.ensure_container(self.root)
        self.context = ToolContext(self.root, run_id=uuid4())
        self.tool = ExecCommandTool(self.sandbox)

    async def asyncTearDown(self):
        await self.sandbox._docker('rm', '-f', self.name)
        key = self.sandbox.container_policy.identity(self.root)
        await self.sandbox._docker('volume', 'rm', 'chagent-cache-' + key[:20])
        self.tmp.cleanup()

    async def call(self, command, timeout=5000):
        return await self.tool.execute('probe', {'cmd': command, 'timeout_ms': timeout}, context=self.context)

    async def test_actual_environment_and_policy_reuse(self):
        result = await self.call('printf ok')
        self.assertFalse(result.is_error, result)
        env = result.details['boundary']['environment']
        self.assertTrue(env['container_id'])
        self.assertTrue(env['image_id'].startswith('sha256:'))
        self.assertEqual(await self.sandbox.ensure_container(self.root), self.name)
        await self.sandbox._docker('update', '--cpus', '3', self.name)
        with self.assertRaisesRegex(RuntimeError, 'cpu'):
            await self.sandbox.ensure_container(self.root)

    async def test_cancel_stops_detached_descendant_and_preserves_unrelated_process(self):
        # This sibling represents the independently managed persistent website.
        await self.sandbox._docker('exec', '-d', self.name, 'sh', '-c',
            f'sleep 2; echo alive > {self.root}/site-alive')
        command = f'setsid sh -c "sleep 1; echo leaked > {self.root}/leak" >/dev/null 2>&1 & echo ready > ready; sleep 20'
        task = asyncio.create_task(self.call(command, 25000))
        for _ in range(100):
            if (self.root / 'ready').exists():
                break
            await asyncio.sleep(.05)
        self.assertTrue((self.root / 'ready').exists())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(2.2)
        self.assertFalse((self.root / 'leak').exists())
        self.assertTrue((self.root / 'site-alive').exists())
        self.assertFalse(self.sandbox._cleanup_pending)

    async def test_timeout_and_normal_exit_reap_background_children(self):
        for timeout, ending in [(500, 'sleep 20'), (5000, 'true')]:
            with self.subTest(timeout=timeout):
                result = await self.call(
                    f'setsid sh -c "sleep 1; echo leaked > {self.root}/leak" >/dev/null 2>&1 & {ending}', timeout)
                self.assertEqual(result.details['timed_out'], timeout == 500)
                await asyncio.sleep(1.2)
                self.assertFalse((self.root / 'leak').exists())

    async def test_lost_supervisor_reports_unknown_and_blocks_reuse(self):
        from chagent.domain.messages import ToolCall
        from chagent.tools.executor import ToolExecutor
        from chagent.tools.registry import ToolRegistry
        registry = ToolRegistry()
        registry.register(self.tool)
        executor = ToolExecutor(registry, allow_host_commands=True)
        result = await executor.execute(ToolCall('lost', 'exec_command',
            {'cmd': 'kill -KILL "$PPID"', 'timeout_ms': 5000}), context=self.context)
        self.assertTrue(result.is_error)
        self.assertTrue(result.outcome_unknown)
        with self.assertRaisesRegex(RuntimeError, 'cleanup_pending'):
            await self.sandbox.ensure_container(self.root)

    async def test_test_runner_records_confirmed_environment(self):
        from chagent.tools.tests_runner import RunTestsTool
        tests = self.root / 'tests'
        tests.mkdir()
        (tests / 'test_example.py').write_text(
            'import unittest\nclass Example(unittest.TestCase):\n    def test_ok(self): self.assertEqual(2 + 2, 4)\n')
        result = await RunTestsTool(self.sandbox).execute('suite', {'suite': 'unittest', 'timeout_ms': 5000},
                                                        context=self.context)
        self.assertFalse(result.is_error, result)
        self.assertTrue(result.details['boundary']['cleanup_confirmed'])
        self.assertTrue(result.details['boundary']['environment']['image_id'])

    async def test_stop_before_worker_start_prevents_launch(self):
        argv, record = await self.sandbox.wrap(['sh', '-c', 'echo bad > started'], self.root,
                                               owner=self.context.run_id)
        await self.sandbox.finish_invocation(record)
        proc = await asyncio.create_subprocess_exec(*argv)
        self.assertEqual(await proc.wait(), 0)
        self.assertFalse((self.root / 'started').exists())

    async def test_real_website_survives_command_cleanup_and_can_stop_when_pending(self):
        import httpx
        from chagent.tools.site import SiteTool
        site = SiteTool(self.sandbox, None)
        (self.root / 'index.html').write_text('boundary-website')
        with patch.object(site, '_save', AsyncMock()):
            started = await site.execute('start', {'action': 'start',
                'command': 'python3 -m http.server 8081', 'port': 8081}, context=self.context)
            self.assertFalse(started.is_error, started)
            url = started.details['url']
            task = asyncio.create_task(self.call('echo ready > ready; sleep 20', 25000))
            for _ in range(100):
                if (self.root / 'ready').exists():
                    break
                await asyncio.sleep(.05)
            self.assertTrue((self.root / 'ready').exists())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, 'boundary-website')
            self.sandbox._cleanup_pending.add(self.name)
            stopped = await site.execute('stop', {'action': 'stop'}, context=self.context)
            self.assertFalse(stopped.is_error)
            self.assertEqual((await site._state(self.name))['state'], 'stopped')

    @unittest.skipUnless(os.environ.get('CHAGENT_TEST_DATABASE_URL'), 'Set CHAGENT_TEST_DATABASE_URL')
    async def test_restart_blocks_unknown_calls_and_rebuild_clears_pending(self):
        from psycopg import AsyncConnection, sql
        from psycopg.conninfo import make_conninfo
        from chagent.config import Settings
        from chagent.storage.database import Database
        from chagent.storage.migrate import migrate
        schema = 'chagent_boundary_' + uuid4().hex
        admin = await AsyncConnection.connect(os.environ['CHAGENT_TEST_DATABASE_URL'], autocommit=True)
        await admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        database = Database(Settings(database_url=make_conninfo(os.environ['CHAGENT_TEST_DATABASE_URL'],
                                                               options=f'-c search_path={schema}')))
        try:
            await database.connect()
            await migrate(database)
            workspace_id, session_id = uuid4(), uuid4()
            async with database.connection() as connection:
                await connection.execute('INSERT INTO workspaces(id,root_path) VALUES (%s,%s)',
                                         (workspace_id, str(self.root)))
                await connection.execute('INSERT INTO sessions(id,workspace_id) VALUES (%s,%s)',
                                         (session_id, workspace_id))
                await connection.execute("INSERT INTO runs(id,session_id,status) VALUES (%s,%s,'running')",
                                         (self.context.run_id, session_id))
                await connection.execute("INSERT INTO workspace_containers(workspace_id,container_name,image_id,state) "
                                         "VALUES (%s,%s,'fixture','ready')", (workspace_id, self.name))
            self.sandbox.database = database
            result = await self.call('printf persisted')
            self.assertFalse(result.is_error)
            async with database.connection() as connection:
                cursor = await connection.execute('SELECT environment,cleanup_confirmed FROM container_invocations')
                row = await cursor.fetchone()
                self.assertTrue(row['cleanup_confirmed'])
                self.assertTrue(row['environment']['image_id'].startswith('sha256:'))
            with self.assertRaisesRegex(RuntimeError, 'in use|active run'):
                await self.sandbox.rebuild_container(self.root, workspace_id, database)
            _, record = await self.sandbox.wrap(['true'], self.root, owner=self.context.run_id)
            async with database.connection() as connection:
                await connection.execute("UPDATE runs SET status='cancelled',end_reason='model_aborted' WHERE id=%s", (self.context.run_id,))
            restarted = Sandbox(self.sandbox.settings)
            restarted.database = database
            with self.assertRaisesRegex(RuntimeError, 'unfinished invocation'):
                await restarted.ensure_container(self.root)
            await restarted.finish_invocation(record)
            self.assertEqual(await restarted.ensure_container(self.root), self.name)
            with patch.object(restarted, '_docker', AsyncMock(return_value=(1, 'unavailable'))):
                with self.assertRaisesRegex(RuntimeError, 'cleanup_pending'):
                    await restarted.finish_invocation(record)
            another = Sandbox(self.sandbox.settings)
            another.database = database
            with self.assertRaisesRegex(RuntimeError, 'cleanup_pending'):
                await another.ensure_container(self.root)
            self.assertEqual(await another.rebuild_container(self.root, workspace_id, database), self.name)
        finally:
            self.sandbox.database = None
            await database.close()
            await admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            await admin.close()
