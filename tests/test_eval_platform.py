"""Operator workflow regressions. All executors are synthetic; no paid calls."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from chagent.config import Settings
from chagent.models.settings import ModelSettings
from eval.integrity import publish
from eval.platform import Busy, Platform, lease
from eval.platform_worker import execute
from eval.records import GraderResult, new_trial
from eval.runner import ROOT
from eval.web import PlanRequest, create_app
import test_eval_report as fixtures


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        cases = root / 'cases'
        cases.mkdir()
        source = ROOT / 'eval/cases/project-work-refuse-outside-workspace-host.json'
        (cases / source.name).write_bytes(source.read_bytes())
        self.base_platform = Platform(root / 'results', cases)
        self.pinned = fixtures.trial('pass', arm='baseline').manifest['execution']
        self.pinned = {k: v for k, v in self.pinned.items() if k != 'end_identity_sha256'}
        self.resolver = lambda overrides, arm: ({'DEEPSEEK_API_KEY': 'synthetic'}, Settings(),
            ModelSettings('deepseek', arm.model, 'https://api.deepseek.com', 'synthetic'), deepcopy(self.pinned))
        for name in ('eval.platform.resolved', 'eval.platform_worker.resolved'):
            p = patch(name, side_effect=lambda *args: self.resolver(*args))
            p.start()
            self.addCleanup(p.stop)
        from chagent.users.store import AccountStore
        self.accounts = AccountStore(root / 'accounts')
        self.admin = self.accounts.register('testadmin', 'test-password-12345',
                                           setup_code=self.accounts.setup_path.read_text())
        self.platform = Platform(root / 'results' / 'users' / self.admin['id'], cases,
                                 account_id=self.admin['id'], account_root=self.accounts.root)
        self.client = TestClient(create_app(self.base_platform, accounts=self.accounts))
        self.client.post('/api/accounts/login', json={'username': 'testadmin', 'password': 'test-password-12345'})
        self.addCleanup(self.client.close)
        self.value = PlanRequest(cases=['project-work/refuse-outside-workspace-host@1'], pricing_confirmed=True).model_dump()

    def plan(self, **overrides):
        return self.platform.plan({**self.value, **overrides})

    def test_plan_freezes_protocol_without_starting_a_worker(self):
        with patch('eval.platform.subprocess.Popen') as launch:
            result = self.client.post('/api/plans', json=self.value)
        self.assertEqual(result.status_code, 201, result.text)
        d = result.json()
        self.assertEqual(d['status']['state'], 'planned')
        self.assertEqual(d['report']['schedule']['planned'], 6)
        self.assertEqual(d['trials'], [])
        launch.assert_not_called()
        self.assertNotIn('synthetic', json.dumps(d))
        self.assertTrue((self.platform.directory(d['id']) / 'protocol.json').exists())

    def test_invalid_or_unsupported_case_is_rejected(self):
        self.assertEqual(self.client.post('/api/plans', json={**self.value,
            'cases': ['../../.env']}).status_code, 400)
        self.assertEqual(self.client.post('/api/plans', json={**self.value,
            'repeats': 0}).status_code, 400)

    def test_prices_require_explicit_confirmation(self):
        self.assertEqual(self.client.post('/api/plans', json={**self.value,
            'pricing_confirmed': False}).status_code, 400)
        missing = {k: v for k, v in self.value.items() if k != 'pricing_confirmed'}
        self.assertEqual(self.client.post('/api/plans', json=missing).status_code, 400)

    def test_invalid_input_does_not_echo_extra_secret_fields(self):
        r = self.client.post('/api/plans', json={**self.value, 'api_key': 'private-marker'})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn('private-marker', r.text)

    def test_cross_origin_and_form_posts_are_rejected(self):
        self.assertEqual(self.client.post('/api/plans', json=self.value,
            headers={'Origin': 'https://outside.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/plans', content='cases=a',
            headers={'Content-Type': 'application/x-www-form-urlencoded'}).status_code, 415)
        self.assertEqual(self.client.get('/api/catalog', headers={'Host': 'outside.example'}).status_code, 400)

    def test_page_and_download_are_read_only(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        p = self.plan()
        r = self.client.get(f"/api/jobs/{p['id']}/report")
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r.headers['content-disposition'])
        self.assertEqual(self.client.get('/api/jobs').json()['jobs'][0]['observed'], 0)

    def test_existing_protocol_and_results_are_browsable_but_not_startable(self):
        from eval.prereg import load, freeze
        p = self.plan()
        # A frozen protocol with no platform metadata is an archived experiment.
        folder = self.platform.directory(p['id'])
        (folder / '.platform/status.json').unlink()
        protocol = load(folder)
        freeze(protocol, folder)
        detail = self.platform.detail(p['id'])
        self.assertEqual(detail['status']['state'], 'archived')
        self.assertEqual(self.platform.jobs()[0]['status']['state'], 'archived')
        with self.assertRaises(ValueError):
            self.platform.start(p['id'])

    def test_body_limit(self):
        r = self.client.post('/api/plans', content='x' * 32769,
                             headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 413)

    def test_missing_and_traversal_identifiers_are_rejected(self):
        self.assertEqual(self.client.get('/api/jobs/bad!id').status_code, 400)
        self.assertEqual(self.client.get('/api/jobs/web-20261114-000000000000').status_code, 404)

    def test_global_lease_blocks_a_second_worker(self):
        p = self.plan()
        with lease(self.platform.state / 'worker.lock'), patch('eval.platform.subprocess.Popen') as launch:
            with self.assertRaises(Busy):
                self.platform.start(p['id'])
        launch.assert_not_called()
        self.assertEqual(self.platform.detail(p['id'])['status']['state'], 'planned')

    def test_subprocess_inherits_lease_without_gap_and_orphan_is_not_resumed(self):
        p = self.plan()
        original = subprocess.Popen
        def dummy(command, **kwargs):
            return original([sys.executable, '-c', 'import time; time.sleep(20)'], **kwargs)
        with patch('eval.platform.subprocess.Popen', side_effect=dummy):
            result = self.platform.start(p['id'])
        child = self.platform.children[-1]
        try:
            self.assertEqual(result['status']['state'], 'starting')
            with self.assertRaises(Busy):
                with lease(self.platform.state / 'worker.lock'):
                    self.fail('lease was released before child exit')
            with self.assertRaises(ValueError):
                self.platform.start(p['id'])
            self.assertTrue(self.platform.stop(p['id'])['stop_requested'])
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertEqual(self.platform.detail(p['id'])['status']['state'], 'interrupted')
        with self.assertRaises(ValueError):
            self.platform.start(p['id'])

    def test_worker_launch_failure_is_not_retriable_under_same_id(self):
        p = self.plan()
        with patch('eval.platform.subprocess.Popen', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.platform.start(p['id'])
        self.assertEqual(self.platform.detail(p['id'])['status']['state'], 'interrupted')
        with self.assertRaises(ValueError):
            self.platform.start(p['id'])

    def test_completed_worker_is_not_overwritten_by_a_stale_poll(self):
        from contextlib import contextmanager
        from eval.platform import save
        p = self.plan()
        status = self.platform.directory(p['id']) / '.platform/status.json'
        save(status, {'state': 'running', 'error': None})
        original = lease
        @contextmanager
        def finish_while_polling(path):
            # Poll read "running"; worker now persists completed and releases.
            save(status, {'state': 'completed', 'error': None})
            with original(path) as owned:
                yield owned
        with patch('eval.platform.lease', finish_while_polling):
            self.assertEqual(self.platform.detail(p['id'])['status']['state'], 'completed')
        self.assertEqual(json.loads(status.read_text())['state'], 'completed')

    def test_rehashed_protocol_cannot_detach_from_its_frozen_control_plan(self):
        from eval.manifests import sha256_json
        p = self.plan()
        path = self.platform.directory(p['id']) / 'protocol.json'
        document = json.loads(path.read_text())
        document['thresholds']['improve_points'] = .9
        document['sha256'] = sha256_json({k: v for k, v in document.items() if k != 'sha256'})
        path.write_text(json.dumps(document))
        calls = []
        async def executor(*args):
            calls.append(True)
        result = self.run_worker(p['id'], executor)
        self.assertEqual(calls, [])
        self.assertEqual(result['status']['state'], 'failed')
        self.assertIn('计划与协议不一致', result['status']['error'])

    def test_exact_cap_on_final_slot_is_completed_not_prematurely_stopped(self):
        p = self.plan(cost_cap=.06)
        result = self.run_worker(p['id'], self.synthetic_executor(cost=.01))
        self.assertEqual(len(result['trials']), 6)
        self.assertEqual(result['status']['state'], 'completed')

    def test_executor_without_a_trial_record_cannot_claim_completed(self):
        p = self.plan()
        calls = []
        async def executor(*args):
            calls.append(True)
        result = self.run_worker(p['id'], executor)
        self.assertEqual(calls, [True])
        self.assertEqual(result['status']['state'], 'failed')
        self.assertIn('未保存', result['status']['error'])

    def test_nonfinite_cost_stops_without_a_further_request(self):
        p = self.plan()
        result = self.run_worker(p['id'], self.synthetic_executor(cost=float('nan')))
        self.assertEqual(result['status']['state'], 'cost_unknown')
        self.assertEqual(len(result['trials']), 1)

    def test_damaged_history_does_not_hide_other_experiments(self):
        p, other = self.plan(), self.plan()
        path = self.platform.directory(p['id']) / 'protocol.json'
        path.write_text('{broken')
        response = self.client.get('/api/jobs')
        self.assertEqual(response.status_code, 200, response.text)
        jobs = {j['id']: j for j in response.json()['jobs']}
        self.assertEqual(jobs[p['id']]['status']['state'], 'record_error')
        self.assertEqual(jobs[other['id']]['status']['state'], 'planned')
        self.assertEqual(path.read_text(), '{broken')
        self.assertEqual(self.client.get('/api/jobs/' + other['id']).status_code, 200)

    def test_repeated_requests_share_the_same_account_process_registry(self):
        self.client.get('/api/jobs')
        first = self.client.app.state.platforms[self.admin['id']]
        self.client.get('/api/catalog')
        self.assertIs(self.client.app.state.platforms[self.admin['id']], first)

    def synthetic_executor(self, callback=None, cost=.01):
        async def run(plans, args, root, store, env, trials):
            plan = plans[0]
            bound = {**deepcopy(self.pinned), 'end_identity_sha256': self.pinned['identity_sha256']}
            manifest = {'arm': plan.arm.name, 'repeat': plan.repeat,
                'protocol_sha256': plan.protocol.sha256, 'case': plan.protocol.cases[0],
                'execution': bound, 'model': {'provider': plan.arm.provider,
                    'requested_id': plan.arm.model}, 'harness': {'cleanup_verified': True,
                                                              'client_kind': 'synthetic'}}
            record = new_trial(plan.case, manifest, plan.protocol.experiment,
                trial_id=plan.trial_id, status='completed', usage={'model_calls': 1,
                    'tool_calls': 0, 'wall_seconds': .1, 'cost': {'usd': cost}}).with_graders(
                        tuple(GraderResult(g['id'], 'pass') for g in plan.case.graders))
            store.write(record)
            if callback:
                callback(plan)
        return run

    def run_worker(self, identifier, executor):
        # The real child owns this lease. In-process tests model the same ownership.
        with lease(self.platform.directory(identifier) / '.platform/worker.lock'):
            asyncio.run(execute(self.platform, identifier, executor))
        return self.platform.detail(identifier)

    def test_worker_runs_all_slots_and_report_is_available(self):
        p = self.plan()
        result = self.run_worker(p['id'], self.synthetic_executor())
        self.assertEqual(result['status']['state'], 'completed', result)
        self.assertEqual(len(result['trials']), 6)
        # Synthetic evidence cannot masquerade as a live model comparison.
        self.assertEqual(result['report']['claim'], 'incomparable')
        self.assertEqual(result['report']['budget']['cost']['estimated_usd'], .06)

    def test_stop_between_trials_preserves_completed_result(self):
        p = self.plan()
        def stop(plan):
            publish(self.platform.directory(p['id']) / '.platform/stop.json', {'requested': True})
        result = self.run_worker(p['id'], self.synthetic_executor(stop))
        self.assertEqual(result['status']['state'], 'stopped')
        self.assertEqual(len(result['trials']), 1)
        self.assertEqual(sum(x['missing'] for x in result['report']['missing']), 5)

    def test_admin_demotion_stops_the_next_paid_slot(self):
        p = self.plan()
        replacement = self.accounts.register('replacement', 'test-password-12345')
        self.accounts.update(self.admin['id'], replacement['id'], role='admin')
        def revoke(plan):
            self.accounts.update(replacement['id'], self.admin['id'], role='user')
        result = self.run_worker(p['id'], self.synthetic_executor(revoke))
        self.assertEqual(result['status']['state'], 'permission_revoked')
        self.assertEqual(len(result['trials']), 1)

    def test_unknown_cost_stops_before_another_call(self):
        p = self.plan()
        result = self.run_worker(p['id'], self.synthetic_executor(cost=None))
        self.assertEqual(result['status']['state'], 'cost_unknown')
        self.assertEqual(len(result['trials']), 1)

    def test_cap_stops_without_scheduling_another_slot(self):
        p = self.plan(cost_cap=.01)
        result = self.run_worker(p['id'], self.synthetic_executor(cost=.01))
        self.assertEqual(result['status']['state'], 'budget_exhausted')
        self.assertEqual(len(result['trials']), 1)

    def test_source_drift_before_start_makes_no_call(self):
        p = self.plan()
        self.pinned['identity_sha256'] = '0' * 64
        calls = []
        async def run(*args):
            calls.append(args)
        result = self.run_worker(p['id'], run)
        self.assertEqual(result['status']['state'], 'failed')
        self.assertIn('尚未调用模型', result['status']['error'])
        self.assertEqual(calls, [])

    def test_case_drift_before_start_makes_no_call(self):
        p = self.plan()
        path = next(self.platform.cases.glob('*.json'))
        d = json.loads(path.read_text())
        d['prompt'] += ' changed'
        path.write_text(json.dumps(d))
        calls = []
        async def run(*args):
            calls.append(args)
        result = self.run_worker(p['id'], run)
        self.assertEqual(result['status']['state'], 'failed')
        self.assertEqual(calls, [])

    def test_crash_claim_is_preserved_and_never_automatically_retried(self):
        p = self.plan()
        async def crash(plans, args, root, store, env, trials):
            publish(self.platform.directory(p['id']) / '.claims' / 'slot.claim', {'unknown_spend': True})
            raise RuntimeError('synthetic crash after claiming')
        result = self.run_worker(p['id'], crash)
        self.assertEqual(result['status']['state'], 'failed')
        self.assertTrue((self.platform.directory(p['id']) / '.claims/slot.claim').exists())
        with self.assertRaises(ValueError):
            self.platform.start(p['id'])

    def test_existing_trial_is_not_accepted_as_a_fresh_start(self):
        p = self.plan()
        folder = self.platform.directory(p['id'])
        (folder / '.claims').mkdir()
        called = []
        async def run(*args):
            called.append(True)
        result = self.run_worker(p['id'], run)
        self.assertEqual(result['status']['state'], 'failed')
        self.assertEqual(called, [])

    def test_stop_before_first_slot_makes_zero_calls(self):
        p = self.plan()
        publish(self.platform.directory(p['id']) / '.platform/stop.json', {'requested': True})
        result = self.run_worker(p['id'], self.synthetic_executor())
        self.assertEqual(result['status']['state'], 'stopped')
        self.assertEqual(result['trials'], [])


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DATABASE_URL'),
                     'Set CHAGENT_TEST_DATABASE_URL')
class PlatformDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_eval_runner import TrialRoundTripTests
        await TrialRoundTripTests.asyncSetUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        cases = folder / 'cases'
        cases.mkdir()
        path = ROOT / 'eval/cases/project-work-refuse-outside-workspace-host.json'
        (cases / path.name).write_bytes(path.read_bytes())
        self.platform = Platform(folder / 'results', cases)

    async def drop_schema(self):
        from test_eval_runner import TrialRoundTripTests
        await TrialRoundTripTests.drop_schema(self)

    async def test_frozen_plan_through_real_runner_storage_grading_and_report(self):
        from chagent.models.fake import FakeModelClient
        from eval.runner import run_trial
        env = {'DEEPSEEK_API_KEY': 'synthetic', 'CHAGENT_PROVIDER': 'deepseek'}
        with patch('eval.platform._evaluation_env', return_value=env):
            plan = self.platform.plan(PlanRequest(cases=[
                'project-work/refuse-outside-workspace-host@1'], pricing_confirmed=True).model_dump())
            async def execute_trial(plans, args, root, store, values, trials):
                for slot in plans:
                    record = await run_trial(slot, database=self.database, env=values,
                        client=FakeModelClient('read_file', {'path': '/etc/hosts'}))
                    store.write(record)
            await execute(self.platform, plan['id'], execute_trial)
        detail = self.platform.detail(plan['id'])
        self.assertEqual(detail['status']['state'], 'completed', detail['status'])
        self.assertEqual(len(detail['trials']), 6)
        self.assertTrue(all(t['verdict'] == 'pass' and t['run_id'] for t in detail['trials']))
        self.assertEqual(detail['report']['claim'], 'incomparable')
        self.assertEqual(detail['report']['budget']['cost']['estimated_usd'], 0.)
