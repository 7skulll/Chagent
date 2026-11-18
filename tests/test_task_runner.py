"""E1: real AgentRunner and disposable Docker task, no paid model calls."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from chagent.agent.runner import RunFailedError
from chagent.bootstrap import create_task_runner
from chagent.config import Settings
from chagent.domain.messages import AssistantMessage, TextContent, ToolCall
from chagent.hostexec.task_environment import TaskEnvironment
from chagent.models.base import Model
from chagent.models.fake import FakeModelClient
from chagent.models.events import StartEvent, DoneEvent
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import ConversationRepository
from chagent.tools.context import ToolContext


class ScriptedClient(FakeModelClient):
    def __init__(self, commands):
        self.commands = iter(commands)
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        command = next(self.commands, None)
        message = AssistantMessage(content=[ToolCall('call_' + uuid4().hex, 'exec_command', command)]
            if command else [TextContent('done')], api=request.model.api, provider=request.model.provider,
            model=request.model.id, timestamp=0, stop_reason='toolUse' if command else 'stop')
        yield StartEvent(partial=message)
        yield DoneEvent(reason=message.stop_reason, message=message)


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DATABASE_URL') and os.environ.get('CHAGENT_TEST_DOCKER') == '1',
                     'Set CHAGENT_TEST_DATABASE_URL and CHAGENT_TEST_DOCKER=1')
class TaskRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'chagent_test_' + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ['CHAGENT_TEST_DATABASE_URL'], autocommit=True)
        await self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(os.environ['CHAGENT_TEST_DATABASE_URL'],
            options=f'-c search_path={self.schema}')))
        await self.database.connect()
        self.addAsyncCleanup(self.database.close)
        await migrate(self.database)
        self.temp = tempfile.TemporaryDirectory(prefix='chagent-e1-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        (self.source / 'input.txt').write_text('frozen')
        self.control = self.root / 'control'; self.control.mkdir()
        self.session = await ConversationRepository(self.database).create_session(self.control)
        self.task = await TaskEnvironment.create(self.root / 'evidence', workspace=self.source, artifacts=['input.txt'])
        self.addAsyncCleanup(self.cleanup_task)
        self.model = Model('fake-task', 'local-test', 'fake', 32768, 512)

    async def cleanup_task(self):
        await self.task.cleanup()
        destination = os.environ.get('CHAGENT_E1_EVIDENCE_DIR')
        if destination:
            import json
            import shutil
            target = Path(destination) / self._testMethodName
            shutil.copytree(self.task.output, target)
            binding = self.task.state.get('runner_binding')
            if binding:
                events = await self.database_events(binding['run_id'])
                (target / 'run-events.json').write_text(json.dumps(events, default=str, indent=2))

    async def database_events(self, run_id):
        from uuid import UUID
        from chagent.storage.repositories import RunRepository
        return await RunRepository(self.database).events(UUID(run_id))

    async def drop_schema(self):
        await self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        await self.admin.close()

    def runner(self, commands, **kwargs):
        return create_task_runner(self.database, self.model, ScriptedClient(commands), task=self.task,
                                  session_id=self.session, control_workspace=self.control, **kwargs)

    async def test_read_modify_read_and_remote_evidence(self):
        runner = self.runner([{'cmd': 'cat input.txt'}, {'cmd': 'printf changed > input.txt'}, {'cmd': 'cat input.txt'}])
        self.assertEqual([t.name for t in runner.tools.registry.definitions()], ['exec_command'])
        result = await runner.run(self.session, 'read, change, read')
        commands = self.task.state['commands']
        self.assertEqual(len(commands), 3)
        self.assertIn('frozen', commands[0]['output'])
        self.assertIn('changed', commands[2]['output'])
        self.assertTrue(all(c['run_id'] == str(result.run_id) and c['tool_call_id'] for c in commands))
        self.assertEqual((self.source / 'input.txt').read_text(), 'frozen')
        self.assertEqual(list(self.control.iterdir()), [])
        self.assertEqual(self.task.state['run_cleanup'], 'confirmed')
        events = await runner.runs.events(result.run_id)
        after = next(e['payload'] for e in events if e['type'] == 'workspace_manifest_after')
        self.assertEqual(after['diff']['modified'][0]['path'], 'input.txt')
        self.assertEqual(after['task_id'], self.task.state['id'])
        self.assertFalse(any(e['type'] == 'network_monitor_started' for e in events))
        self.assertIn('Network mode: none', runner.client.requests[0].messages[0].text())
        await self.task.collect()
        self.assertEqual((self.task.output / 'artifacts/input.txt').read_text(), 'changed')

    async def test_wrong_session_and_consumed_task_rejected(self):
        runner = self.runner([])
        other = await runner.conversations.create_session(self.control)
        with self.assertRaises(RunFailedError):
            await runner.run(other, 'wrong')
        self.assertEqual(runner.client.requests, [])
        await runner.run(self.session, 'correct')
        again = self.runner([])
        with self.assertRaises(RunFailedError):
            await again.run(self.session, 'reuse')
        self.assertEqual(again.client.requests, [])

    async def test_wrong_command_context_rejected(self):
        runner = self.runner([])
        execution = runner.task_execution
        await execution.bind(self.session, uuid4(), self.control)
        tool = runner.tools.registry.get('exec_command')
        with self.assertRaises(PermissionError):
            await tool.execute('bad', {'cmd': 'touch forbidden'},
                context=ToolContext(self.control, session_id=self.session, run_id=uuid4()))
        self.assertEqual(self.task.state['commands'], [])
        with self.assertRaises(PermissionError):
            await tool.execute('bad-task', {'cmd': 'touch forbidden'},
                context=ToolContext(self.control, session_id=self.session, run_id=execution.run_id,
                                    task_id='wrong-task'))
        with self.assertRaises(PermissionError):
            await self.task.run('touch forbidden')
        for name in ('read_file', 'write_file', 'host_exec', 'site', 'process', 'read_skill'):
            self.assertIsNone(runner.tools.registry.get(name))

    async def test_command_timeout_reaped(self):
        runner = self.runner([{'cmd': 'sleep 20; touch late', 'timeout_ms': 200}])
        await runner.run(self.session, 'timeout')
        self.assertTrue(self.task.state['commands'][0]['timed_out'])
        self.assertTrue(self.task.state['commands'][0]['boundary']['cleanup_confirmed'])
        self.assertEqual(self.task.state['run_cleanup'], 'confirmed')

    async def test_cancel_and_wall_timeout_are_not_success(self):
        runner = self.runner([{'cmd': 'sleep 20; touch late'}], wall_timeout=2)
        with self.assertRaises(RunFailedError) as error:
            await runner.run(self.session, 'wall timeout')
        self.assertEqual(error.exception.end_reason, 'run_timeout')
        self.assertEqual(self.task.state['run_cleanup'], 'confirmed')
        events = await runner.runs.events(error.exception.run_id)
        self.assertTrue(any(e['type'] == 'task_run_released' for e in events))

    async def test_cancel_records_interruption(self):
        runner = self.runner([{'cmd': 'sleep 20; touch late'}])
        run = asyncio.create_task(runner.run(self.session, 'cancel'))
        for _ in range(500):
            if self.task.state['phase'] == 'executing' and self.task.state.get('invocation'):
                break
            await asyncio.sleep(.02)
        else:
            self.fail('Invocation did not start')
        runner.cancel(runner.task_execution.run_id)
        with self.assertRaises(asyncio.CancelledError):
            await run
        self.assertEqual(self.task.state['phase'], 'execution_interrupted')
        self.assertEqual(self.task.state['run_cleanup'], 'confirmed')
        self.assertTrue(self.task.state['invocation']['cleanup_confirmed'])

    async def test_cleanup_failure_stays_unknown(self):
        runner = self.runner([])
        original = runner.task_execution.release
        async def failing_release(run_id):
            with patch.object(self.task, 'docker', side_effect=RuntimeError('simulated daemon failure')):
                await original(run_id)
        with patch.object(runner.task_execution, 'release', side_effect=failing_release):
            result = await runner.run(self.session, 'finish')
        self.assertEqual(self.task.state['run_cleanup'], 'unknown')
        events = await runner.runs.events(result.run_id)
        self.assertTrue(any(e['type'] == 'task_cleanup_unknown' for e in events))
        with self.assertRaises(RuntimeError):
            await self.task.collect()
