"""E2 end-to-end tests: only synthetic clients, real Docker and isolated DB."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from eval import container_trial as ct, prereg
from chagent.config import Settings
from chagent.models.fake import FakeModelClient
from chagent.storage.database import Database
from chagent.storage.migrate import migrate


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DATABASE_URL') and os.environ.get('CHAGENT_TEST_DOCKER') == '1',
                     'Set CHAGENT_TEST_DATABASE_URL and CHAGENT_TEST_DOCKER=1')
class ContainerTrialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'chagent_eval_test_' + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ['CHAGENT_TEST_DATABASE_URL'], autocommit=True)
        await self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(os.environ['CHAGENT_TEST_DATABASE_URL'],
            options=f'-c search_path={self.schema}')))
        await self.database.connect()
        self.addAsyncCleanup(self.database.close)
        await migrate(self.database)
        self.temp = tempfile.TemporaryDirectory(prefix='chagent-e2-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        (self.source / 'input.txt').write_text('42')
        self.verifier = self.root / 'verifier'; self.verifier.mkdir()
        (self.verifier / 'check.py').write_text("def verify(artifacts):\n    return (artifacts/'answer.txt').read_text().strip() == '42'\n")
        self.spec = {'id': 'container-answer', 'version': 1, 'title': 'Frozen answer',
            'prompt': 'Read input.txt and write answer.txt with the same text.',
            'workspace': str(self.source), 'verifier': str(self.verifier), 'artifacts': ['answer.txt'],
            'budget': {'max_turns': 4, 'max_tool_calls': 3, 'wall_seconds': 60, 'max_cost_usd': .05},
            'verifier_timeout': 10}
        self.taskdir = self.root / 'frozen'
        self.case, _, _ = await ct.freeze_task(self.spec, self.taskdir)
        self.protocol = self.protocol_for(self.case)
        self.env = {'CHAGENT_PROVIDER': 'deepseek', 'DEEPSEEK_API_KEY': 'synthetic-test',
            'CHAGENT_CONTEXT_WINDOW': '16384', 'CHAGENT_MAX_TOKENS': '512'}
        self.addAsyncCleanup(self.cleanup_resources)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        await self.admin.close()

    async def cleanup_resources(self):
        for path in (self.root / 'results').glob('*/container-trials/*'):
            await ct.cleanup_trial(path)
        if os.environ.get('CHAGENT_E2_EVIDENCE_DIR'):
            shutil.copytree(self.root, Path(os.environ['CHAGENT_E2_EVIDENCE_DIR']) / self._testMethodName)

    def protocol_for(self, case, **kwargs):
        return prereg.build(experiment='e2-test', question='Internal diagnostic', hypothesis='Check plumbing',
            cases=[case], arms=[prereg.Arm('baseline', 'deepseek', 'deepseek-flash'),
                               prereg.Arm('candidate', 'deepseek', 'deepseek-flash')],
            repeats=3, min_repeats_for_stability=3, order='interleaved',
            budget={'max_turns': 4, 'max_tool_calls': 3, 'wall_seconds': 60, 'total_cost_usd': .1},
            thresholds={'improve_points': .3, 'regress_points': .3}, analysis='diagnostic only',
            exclusions='keep infrastructure errors', stopping_rule='one selected slot only',
            pricing={'input_usd_per_mtok': .3, 'output_usd_per_mtok': 1.2, 'source': 'synthetic tests only'}, **kwargs)

    async def execute(self, cmd='cd /workspace && cp input.txt answer.txt', repeat=0, client=None):
        return await ct.execute_slot(self.taskdir, self.protocol, self.protocol.baseline, repeat,
            database=self.database, root=self.root / 'results', env=self.env,
            client=client or FakeModelClient('exec_command', {'cmd': cmd}))

    async def test_correct_and_wrong_artifact_and_report(self):
        first = await self.execute()
        second = await self.execute('printf wrong > answer.txt', repeat=1)
        self.assertEqual((first.status, first.verdict), ('completed', 'pass'))
        self.assertEqual((second.status, second.verdict), ('completed', 'fail'))
        self.assertTrue(first.manifest['harness']['cleanup_verified'])
        self.assertEqual(first.usage['model_calls'], 2)
        self.assertEqual(first.usage['cost']['usd'], 0)
        self.assertEqual((self.source / 'input.txt').read_text(), '42')
        report = ct.build_report(self.root / 'results', self.protocol)
        self.assertEqual(report['planned'], 6)
        self.assertEqual(report['missing'], 4)
        self.assertEqual(report['outcomes']['pass'], 1)
        self.assertEqual(report['outcomes']['fail'], 1)
        with self.assertRaisesRegex(ValueError, 'consumed'):
            await self.execute()

    async def test_verifier_exception_is_infra_error(self):
        other = self.root / 'other'
        (self.verifier / 'check.py').write_text("def verify(artifacts):\n    raise ValueError('broken trusted check')\n")
        self.case, _, _ = await ct.freeze_task(self.spec, other)
        self.taskdir = other; self.protocol = self.protocol_for(self.case)
        record = await self.execute()
        self.assertEqual(record.verdict, 'infra_error')
        verification = Path(record.manifest['container_task']['evidence']) / 'verification'
        self.assertTrue((verification / 'raw-result.json').exists())
        self.assertIn('ValueError', (verification / 'verifier.log').read_text())

    async def test_missing_artifact_is_infra_error(self):
        record = await self.execute('cat input.txt')
        self.assertEqual(record.verdict, 'infra_error')
        self.assertTrue(record.manifest['harness']['cleanup_verified'])

    async def test_frozen_input_edits_rejected_before_model(self):
        for name in ('task.json', 'case.json', 'snapshot.tar', 'verifier/plan.json', 'verifier/verifier.tar'):
            with self.subTest(name=name):
                path = self.taskdir / name
                original = path.read_bytes()
                path.write_bytes(b'changed')
                client = FakeModelClient()
                with patch.object(client, 'stream', side_effect=AssertionError('model must not run')):
                    with self.assertRaises((ValueError, __import__('tarfile').TarError)):
                        await self.execute(client=client)
                path.write_bytes(original)
        self.assertFalse((self.root / 'results').exists())
        with self.assertRaisesRegex(ValueError, 'outside frozen task inputs'):
            await ct.execute_slot(self.taskdir, self.protocol, self.protocol.baseline, 0,
                database=self.database, root=self.taskdir / 'nested-output', env=self.env,
                client=FakeModelClient())

    async def test_source_edits_after_freeze_do_not_change_task(self):
        (self.source / 'input.txt').write_text('wrong-source')
        (self.verifier / 'check.py').write_text('raise RuntimeError("wrong-source")')
        record = await self.execute()
        self.assertEqual(record.verdict, 'pass')

    async def test_inflight_input_drift_cannot_pass(self):
        class MutatingClient(FakeModelClient):
            async def stream(inner, request):
                (self.taskdir / 'case.json').write_text('{}')
                async for event in super(MutatingClient, inner).stream(request):
                    yield event
        record = await self.execute(client=MutatingClient('exec_command', {'cmd': 'cp input.txt answer.txt'}))
        self.assertEqual(record.verdict, 'infra_error')

    async def test_tool_budget_prevents_execution(self):
        self.protocol = replace(self.protocol, budget={**self.protocol.budget, 'max_tool_calls': 0}, sha256='')
        # Build through the canonical schema/hash, never patch an unchecked protocol.
        doc = self.protocol.to_json(with_hash=False); doc.pop('protocol_version')
        doc.pop('cases'); doc['arms'] = [prereg.Arm(**a) for a in doc['arms']]
        self.protocol = prereg.build(cases=[self.case], **doc)
        record = await self.execute()
        self.assertEqual(record.verdict, 'fail')
        self.assertEqual(record.usage['tool_calls'], 0)
        self.assertIn('tool call budget', record.manifest['harness']['budget_violation'])

    async def test_unknown_cost_stops_next_slot(self):
        class MissingUsageClient:
            async def stream(inner, request):
                async for event in FakeModelClient('exec_command', {'cmd': 'cp input.txt answer.txt'}).stream(request):
                    yield event
        record = await self.execute(client=MissingUsageClient())
        self.assertIsNone(record.usage['cost']['usd'])
        self.assertNotEqual(record.verdict, 'pass')
        with self.assertRaisesRegex(ValueError, 'unknown'):
            await self.execute(repeat=1, client=MissingUsageClient())

    async def test_cancelled_trial_recorded_without_grading(self):
        class WaitingClient(FakeModelClient):
            async def stream(inner, request):
                entered.set()
                await asyncio.sleep(30)
                async for event in super(WaitingClient, inner).stream(request):
                    yield event
        entered = asyncio.Event()
        running = asyncio.create_task(self.execute(client=WaitingClient()))
        await asyncio.wait_for(entered.wait(), 15)
        running.cancel()
        record = await running
        self.assertEqual(record.status, 'cancelled')
        self.assertNotEqual(record.verdict, 'pass')
        self.assertTrue(record.manifest['harness']['cleanup_verified'])
        with self.assertRaisesRegex(ValueError, 'consumed'):
            await self.execute()

    async def test_cleanup_unknown_cannot_pass(self):
        with patch.object(ct.TaskEnvironment, 'cleanup', side_effect=RuntimeError('simulated daemon down')):
            record = await self.execute()
        self.assertEqual(record.verdict, 'infra_error')
        self.assertFalse(record.manifest['harness']['cleanup_verified'])

    async def test_report_rejects_changed_evidence(self):
        record = await self.execute()
        output = Path(record.manifest['container_task']['evidence'])
        (output / 'task/artifacts.tar').write_bytes(b'tampered')
        report = ct.build_report(self.root / 'results', self.protocol)
        self.assertTrue(report['binding_errors'])
        self.assertEqual(report['outcomes']['pass'], 0)
        with self.assertRaisesRegex(ValueError, 'invalid bindings'):
            await self.execute(repeat=1)

    async def refreeze(self, *, wall=4, verifier_timeout=10):
        self.taskdir = self.root / 'timeout-frozen'
        spec = {**self.spec, 'budget': {**self.spec['budget'], 'wall_seconds': wall},
                'verifier_timeout': verifier_timeout}
        self.case, _, _ = await ct.freeze_task(spec, self.taskdir)
        self.protocol = self.protocol_for(self.case)

    async def assert_instrument_timeout(self, stage, *, outer):
        await self.refreeze()
        async def slow(resource, *args, **kwargs):
            if outer:
                await asyncio.sleep(15)
            raise TimeoutError('injected operation timeout')
        target, method = {'collection': (ct.TaskEnvironment, 'collect'),
            'verification': (ct.Verification, 'run'),
            'verification_preparation': (ct.Verification, 'create'),
            'preparation': (ct.TaskEnvironment, 'create')}[stage]
        context = (patch.object(target, method, new=slow) if method not in {'create'} else
                   patch.object(target, method, new=AsyncMock(side_effect=TimeoutError('injected preparation timeout'))))
        with context:
            record = await self.execute()
        self.assertEqual(record.status, 'infra_error')
        self.assertEqual(record.verdict, 'infra_error')
        self.assertEqual(record.graders[0].result, 'infra_error')
        harness = record.manifest['harness']
        self.assertIsNone(harness['budget_violation'])
        self.assertEqual(harness['timeout_stage'], stage)
        self.assertEqual(harness['timeout_scope'], 'trial_wall' if outer else 'operation')
        self.assertEqual(harness['end_reason'], None if stage == 'preparation' else 'model_stop')
        if stage != 'preparation':
            self.assertTrue(harness['cleanup_verified'])
            from uuid import UUID
            async with self.database.connection() as connection:
                cursor = await connection.execute('SELECT status,end_reason FROM runs WHERE id=%s', (UUID(record.run_id),))
                self.assertEqual(await cursor.fetchone(), {'status': 'completed', 'end_reason': 'model_stop'})
        from eval.report import build
        measured = build(self.protocol, [record])['cases'][0]['arms']['baseline']
        self.assertEqual(measured['denominator'], 0)
        self.assertEqual(measured['fail'], 0)
        self.assertEqual(measured['infra_error'], 1)

    async def test_outer_wall_during_collection_is_not_candidate_failure(self):
        await self.assert_instrument_timeout('collection', outer=True)

    async def test_outer_wall_during_verification_is_not_candidate_failure(self):
        await self.assert_instrument_timeout('verification', outer=True)

    async def test_prepare_timeout_is_infrastructure(self):
        await self.assert_instrument_timeout('preparation', outer=False)

    async def test_verifier_prepare_timeout_preserves_model_stop(self):
        await self.assert_instrument_timeout('verification_preparation', outer=False)

    async def test_operation_timeout_during_collection_is_infrastructure(self):
        await self.assert_instrument_timeout('collection', outer=False)

    async def test_task_cleanup_timeout_is_infrastructure(self):
        original = ct.TaskEnvironment.cleanup
        calls = 0
        async def timeout_once(task):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError('injected cleanup timeout')
            return await original(task)
        with patch.object(ct.TaskEnvironment, 'cleanup', new=timeout_once):
            record = await self.execute()
        self.assertEqual(record.verdict, 'infra_error')
        self.assertTrue(record.manifest['harness']['cleanup_verified'])
        self.assertEqual(record.manifest['harness']['end_reason'], 'model_stop')
        self.assertEqual(record.manifest['harness']['timeout_stage'], 'task_cleanup')
        self.assertIsNone(record.manifest['harness']['budget_violation'])

    async def test_candidate_outer_wall_still_counts_as_budget_failure(self):
        await self.refreeze()
        class WaitingClient(FakeModelClient):
            async def stream(inner, request):
                await asyncio.sleep(15)
                async for event in super(WaitingClient, inner).stream(request):
                    yield event
        record = await self.execute(client=WaitingClient())
        self.assertEqual((record.status, record.verdict), ('failed', 'fail'))
        self.assertEqual(record.manifest['harness']['budget_violation'], 'run_timeout')
        self.assertEqual(record.manifest['harness']['timeout_stage'], 'candidate')
        self.assertTrue(record.manifest['harness']['cleanup_verified'])

    async def test_wall_during_completed_runner_release_is_infrastructure(self):
        from chagent.hostexec.task_runner import TaskExecution
        await self.refreeze()
        original = TaskExecution.release
        async def slow_release(execution, run_id):
            await original(execution, run_id)
            await asyncio.sleep(15)
        with patch.object(TaskExecution, 'release', new=slow_release):
            record = await self.execute()
        self.assertEqual((record.status, record.verdict), ('infra_error', 'infra_error'))
        self.assertEqual(record.manifest['harness']['timeout_stage'], 'candidate_cleanup')
        self.assertEqual(record.manifest['harness']['end_reason'], 'model_stop')
        self.assertIsNone(record.manifest['harness']['budget_violation'])
        self.assertTrue(record.manifest['harness']['cleanup_verified'])

    async def test_reported_cost_violation_still_counts_as_budget_failure(self):
        from chagent.models.events import DoneEvent
        original = ct.BudgetClient.stream
        async def reported_overcharge(guard, request):
            async for event in original(guard, request):
                if isinstance(event, DoneEvent):
                    guard.violation = 'reported charge exceeded the conservative reservation'
                yield event
        with patch.object(ct.BudgetClient, 'stream', new=reported_overcharge):
            record = await self.execute()
        self.assertEqual((record.status, record.verdict), ('failed', 'fail'))
        self.assertIn('reported charge', record.manifest['harness']['budget_violation'])
        self.assertEqual(record.manifest['harness']['end_reason'], 'model_stop')

    async def test_verifier_own_timeout_remains_infrastructure(self):
        (self.verifier / 'check.py').write_text('import time\ndef verify(artifacts):\n    time.sleep(2)\n    return True\n')
        await self.refreeze(wall=60, verifier_timeout=.1)
        record = await self.execute()
        self.assertEqual(record.verdict, 'infra_error')
        self.assertEqual(record.manifest['harness']['end_reason'], 'model_stop')
        self.assertIsNone(record.manifest['harness']['budget_violation'])

    async def test_crashed_process_claim_cleanup_and_no_replay(self):
        frozen_protocol = prereg.freeze(self.protocol, self.root / 'protocol')
        script = '''import asyncio, os
from pathlib import Path
from eval.cli import _eval_database
from eval import container_trial as ct, prereg
from chagent.models.fake import FakeModelClient
async def main():
    protocol = prereg.load(Path(os.environ['E2_PROTOCOL']))
    async with _eval_database(os.environ['CHAGENT_TEST_DATABASE_URL'], os.environ['E2_SCHEMA']) as db:
        await ct.execute_slot(Path(os.environ['E2_TASK']), protocol, protocol.baseline, 0,
            database=db, root=Path(os.environ['E2_RESULTS']),
            env={'CHAGENT_PROVIDER':'deepseek','DEEPSEEK_API_KEY':'synthetic-test',
                 'CHAGENT_CONTEXT_WINDOW':'16384','CHAGENT_MAX_TOKENS':'512'},
            client=FakeModelClient('exec_command', {'cmd':'sleep 25; cp input.txt answer.txt'}))
asyncio.run(main())
'''
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', script, env={**os.environ,
            'E2_PROTOCOL': str(frozen_protocol.parent), 'E2_SCHEMA': self.schema,
            'E2_TASK': str(self.taskdir), 'E2_RESULTS': str(self.root / 'results')},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            for _ in range(600):
                files = list((self.root / 'results').glob('*/container-trials/*/task/environment.json'))
                if files:
                    state = json.loads(files[0].read_text())
                    if state.get('invocation'):
                        break
                if process.returncode is not None:
                    self.fail((await process.stderr.read()).decode())
                await asyncio.sleep(.02)
            else:
                self.fail('Task never started')
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        report = ct.build_report(self.root / 'results', self.protocol)
        self.assertEqual(len(report['unfinished_claims']), 1)
        self.assertTrue(report['unknown_cost'])
        self.assertEqual(report['outcomes']['pass'], 0)
        with self.assertRaisesRegex(ValueError, 'Unfinished'):
            await self.execute()
        output = files[0].parent.parent
        await ct.cleanup_trial(output)
        self.assertEqual(json.loads(files[0].read_text())['cleanup'], 'confirmed')
        self.assertEqual(ct.build_report(self.root / 'results', self.protocol)['unfinished_claims'], report['unfinished_claims'])
