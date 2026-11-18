"""E3 operator workflow: real Docker, runner, verifier, worker and HTTP; no paid calls."""
from copy import deepcopy
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import AsyncMock, patch

from psycopg import sql
from fastapi.testclient import TestClient
from chagent.models.fake import FakeModelClient
from chagent.users.store import AccountStore
from eval import container_trial as ct
from eval.platform import Platform, lease
from eval.platform_worker import execute
from eval.web import PlanRequest, create_app
import test_container_trial as fixtures


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DATABASE_URL') and os.environ.get('CHAGENT_TEST_DOCKER') == '1',
                     'Set CHAGENT_TEST_DATABASE_URL and CHAGENT_TEST_DOCKER=1')
class ContainerPlatformTests(unittest.IsolatedAsyncioTestCase):
    protocol_for = fixtures.ContainerTrialTests.protocol_for
    drop_schema = fixtures.ContainerTrialTests.drop_schema
    cleanup_resources = fixtures.ContainerTrialTests.cleanup_resources

    async def asyncSetUp(self):
        await fixtures.ContainerTrialTests.asyncSetUp(self)
        self.catalog = self.root / 'catalog'
        shutil.copytree(self.taskdir, self.catalog / 'answer')
        self.accounts = AccountStore(self.root / 'accounts')
        self.admin_user = self.accounts.register('e3admin', 'test-password-12345',
            setup_code=self.accounts.setup_path.read_text())
        self.base = Platform(self.root / 'results', self.root / 'empty-host-cases', container_tasks=self.catalog)
        self.platform = Platform(self.base.root / 'users' / self.admin_user['id'], self.base.cases,
            account_id=self.admin_user['id'], account_root=self.accounts.root, container_tasks=self.catalog)
        self.client = TestClient(create_app(self.base, accounts=self.accounts))
        self.addCleanup(self.client.close)
        self.client.post('/api/accounts/login', json={'username': 'e3admin', 'password': 'test-password-12345'})
        self.env.update(CHAGENT_EVAL_DATABASE_URL=os.environ['CHAGENT_TEST_DATABASE_URL'])
        self.env_patch = patch('eval.platform._evaluation_env', return_value=self.env)
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        self.request = PlanRequest(cases=[self.case.key], pricing_confirmed=True,
            context_window=16384, max_tokens=512).model_dump()
        self.addAsyncCleanup(self.drop_worker_schema)
        self.addAsyncCleanup(self.cleanup_platform_tasks)

    async def drop_worker_schema(self):
        await self.admin.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(
            sql.Identifier('chagent_eval_' + self.admin_user['id'])))

    async def cleanup_platform_tasks(self):
        for path in self.platform.root.glob('*/container-trials/*'):
            await ct.cleanup_trial(path)

    def plan(self):
        response = self.client.post('/api/plans', json=self.request)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def worker(self, plan, after=None, *, missing_usage=False):
        original = ct.execute_slot
        async def synthetic(*args, **kwargs):
            arm = args[2]
            command = 'cd /workspace; cp input.txt answer.txt' if arm.name == 'baseline' else 'printf wrong > /workspace/answer.txt'
            fake = FakeModelClient('exec_command', {'cmd': command})
            if missing_usage:
                class MissingUsage:
                    async def stream(self, request):
                        async for event in fake.stream(request):
                            yield event
                fake = MissingUsage()
            record = await original(*args, **kwargs, client=fake)
            if after:
                after(record)
            return record
        with patch.object(ct, 'execute_slot', side_effect=synthetic):
            with lease(self.platform.directory(plan['id']) / '.platform/worker.lock'):
                await execute(self.platform, plan['id'])
        return self.client.get('/api/jobs/' + plan['id']).json()

    async def test_catalog_plan_worker_and_report_full_chain(self):
        rows = self.client.get('/api/catalog').json()['cases']
        self.assertTrue(rows[0]['supported'])
        self.assertEqual(rows[0]['execution']['tools'], ['exec_command'])
        p = self.plan()
        self.assertEqual(p['backend'], 'container-task-v1')
        self.assertEqual(p['report']['schedule']['planned'], 6)
        # Operator catalog changes after freeze do not change the paid plan.
        (self.catalog / 'answer/case.json').write_text('{}')
        result = await self.worker(p)
        self.assertEqual(result['status']['state'], 'completed', result['status'])
        self.assertEqual(len(result['trials']), 6)
        self.assertEqual(sum(t['verdict'] == 'pass' for t in result['trials']), 3)
        self.assertEqual(sum(t['verdict'] == 'fail' for t in result['trials']), 3)
        self.assertTrue(all(t['task_id'] and t['tool_profile'] == 'task-command-only-v1' for t in result['trials']))
        self.assertEqual(result['report']['claim'], 'incomparable')  # Synthetic != paid evidence.
        self.assertEqual(result['report']['budget']['cost']['estimated_usd'], 0)
        self.assertEqual(self.client.get('/api/jobs/' + p['id'] + '/report').status_code, 200)
        self.assertEqual(self.client.post('/api/jobs/' + p['id'] + '/start', json={}).status_code, 400)

    async def test_stop_blocks_later_slots(self):
        p = self.plan()
        def stop(record):
            response = self.client.post('/api/jobs/' + p['id'] + '/stop', json={})
            self.assertEqual(response.status_code, 200)
        result = await self.worker(p, stop)
        self.assertEqual(result['status']['state'], 'stopped')
        self.assertEqual(len(result['trials']), 1)

    async def test_demotion_blocks_later_slots_and_http_access(self):
        p = self.plan()
        replacement = self.accounts.register('replacement', 'test-password-12345')
        self.accounts.update(self.admin_user['id'], replacement['id'], role='admin')
        def revoke(record):
            self.accounts.update(replacement['id'], self.admin_user['id'], role='user')
        await self.worker(p, revoke)
        result = self.platform.detail(p['id'])
        self.assertEqual(result['status']['state'], 'permission_revoked')
        self.assertEqual(len(result['trials']), 1)
        self.assertEqual(self.client.get('/api/jobs').status_code, 401)

    async def test_unknown_cost_stops_after_one_slot(self):
        p = self.plan()
        result = await self.worker(p, missing_usage=True)
        self.assertEqual(result['status']['state'], 'cost_unknown', result['status'])
        self.assertEqual(len(result['trials']), 1)
        self.assertIsNone(result['report']['budget']['cost']['estimated_usd'])

    async def test_frozen_drift_and_missing_image_preflight_make_no_calls(self):
        for missing_image in (False, True):
            p = self.plan()
            called = AsyncMock()
            if not missing_image:
                frozen = next((self.platform.directory(p['id']) / '.platform/tasks').glob('*'))
                (frozen / 'case.json').write_text('{}')
            with patch.object(ct, 'execute_slot', called):
                if missing_image:
                    with patch('eval.container_catalog.preflight', side_effect=ValueError('missing image')):
                        await execute(self.platform, p['id'])
                else:
                    await execute(self.platform, p['id'])
            called.assert_not_called()
            self.assertEqual(self.platform.detail(p['id'])['status']['state'], 'failed')

    async def test_pending_claim_preserves_missing_and_unknown_cost(self):
        p = self.plan()
        from eval.integrity import publish
        publish(self.platform.directory(p['id']) / '.claims/crashed.claim', {'unknown': True})
        await execute(self.platform, p['id'])
        d = self.platform.detail(p['id'])
        self.assertEqual(d['status']['state'], 'failed')
        self.assertEqual(d['container']['unfinished_claims'], ['crashed'])
        self.assertEqual(d['report']['budget']['cost']['unknown_trials'], 1)
        self.assertIsNone(d['report']['budget']['cost']['estimated_usd'])
        self.assertEqual(len(d['trials']), 0)

    async def test_other_admin_cannot_read_or_start_owner_plan(self):
        p = self.plan()
        second = self.accounts.register('secondadmin', 'test-password-12345')
        self.accounts.update(self.admin_user['id'], second['id'], role='admin')
        with TestClient(create_app(self.base, accounts=self.accounts)) as client:
            client.post('/api/accounts/login', json={'username': 'secondadmin', 'password': 'test-password-12345'})
            self.assertEqual(client.get('/api/jobs/' + p['id']).status_code, 404)
            self.assertEqual(client.post('/api/jobs/' + p['id'] + '/start', json={}).status_code, 404)
            self.assertEqual(client.get('/api/jobs').json()['jobs'], [])

    async def test_catalog_missing_images_unavailable_and_plan_rejected(self):
        with patch('eval.container_catalog.preflight', side_effect=ValueError('missing image')):
            rows = self.client.get('/api/catalog').json()['cases']
            self.assertFalse(rows[0]['supported'])
            self.assertIn('missing image', rows[0]['reason'])
            self.assertEqual(self.client.post('/api/plans', json=self.request).status_code, 400)

    async def test_mixed_tool_profiles_are_rejected(self):
        self.base.cases.mkdir()
        source = Path(__file__).resolve().parent.parent / 'eval/cases/project-work-refuse-outside-workspace-host.json'
        shutil.copyfile(source, self.base.cases / source.name)
        payload = deepcopy(self.request)
        payload['cases'].append('project-work/refuse-outside-workspace-host@1')
        response = self.client.post('/api/plans', json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn('分别创建', response.json()['error'])
