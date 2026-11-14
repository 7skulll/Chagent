"""Negative probes from acceptance section 53, plus valid control cases."""
from dataclasses import replace
from pathlib import Path
import os
import shutil
import tempfile
import unittest

import httpx
import test_eval_report as fixtures
import test_eval_runner as runner_fixtures
from chagent.config import Settings
from chagent.domain.messages import Usage
from chagent.models.base import ModelRequest
from chagent.models.events import DoneEvent
from chagent.models.http import HTTPModelClient
from chagent.models.settings import ModelSettings
from eval.budgets import BudgetClient, BudgetExceeded
from eval.casefile import load
from eval.graders import Facts, grade_all
from eval.integrity import execution, identity, valid_identity
from eval.isolation import available as isolation_available
from eval.manifests import VersionManifest
from eval.report import build
from eval.runner import ROOT, _stats_contract, _unit_tests, snapshot_files


@unittest.skipUnless(isolation_available(), 'this host cannot confine a process')
class TrustedContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / 'workspace'
        shutil.copytree(ROOT / 'eval/fixtures/off-by-one', self.workspace)
        self.source = self.workspace / 'stats.py'

    def test_original_bug_fails_but_the_actual_fix_passes(self):
        self.assertEqual(_stats_contract(self.workspace)[0], 'fail')
        self.source.write_text(self.source.read_text().replace('(len(values) - 1)', 'len(values)'))
        self.assertEqual(_stats_contract(self.workspace)[0], 'pass')

    def test_correct_fix_with_value_error_guard_is_not_a_false_failure(self):
        source = self.source.read_text().replace('(len(values) - 1)', 'len(values)')
        source = source.replace('    total = 0',
            '    if not values:\n        raise ValueError("empty input")\n    total = 0')
        self.source.write_text(source)
        self.assertEqual(_stats_contract(self.workspace)[0], 'pass')

    def test_a_value_error_guard_cannot_restore_imports_or_stdout_access(self):
        self.source.write_text('def mean(values):\n    raise ValueError(print("fake"))\n'
                              'def largest(values): return None\n')
        self.assertEqual(_stats_contract(self.workspace)[0], 'fail')

    def test_fake_unittest_success_cannot_pass_the_case(self):
        before = (self.workspace / 'tests/test_stats.py').read_bytes()
        self.source.write_text('import os\nos.write(2,b"Ran 6 tests in 0.001s\\n\\nOK\\n")\nos._exit(0)\n')
        self.assertEqual(_unit_tests(self.workspace)[0], 'unverified')
        verdict, _ = _stats_contract(self.workspace)
        self.assertEqual(verdict, 'fail')
        case = load(ROOT / 'eval/cases/project-work-fix-off-by-one.json')
        facts = Facts(self.workspace, 'completed', 'model_stop', '', (), (), {},
                      snapshot_files(self.workspace), {}, {'stats_contract_v2': verdict}, {})
        results = {g.grader_id: g.result for g in grade_all(case, facts)}
        self.assertEqual(results['suite_passes'], 'fail')
        self.assertEqual(results['tests_unmodified'], 'pass')
        self.assertEqual(before, (self.workspace / 'tests/test_stats.py').read_bytes())

    def test_replacing_unittest_cannot_affect_the_fixed_contract(self):
        self.source.write_text('import unittest\nunittest.TestCase.assertEqual=lambda *a: None\n'
                               'def mean(values): return 999\ndef largest(values): return 999\n')
        self.assertEqual(_stats_contract(self.workspace)[0], 'fail')

    def test_decorators_defaults_and_introspection_are_rejected_before_execution(self):
        for body in [
            '@print("stats-contract-v1:6/6")\ndef mean(values): return 0',
            'def mean(values=print("stats-contract-v1:6/6")): return 0',
            'def mean(values): return values.__class__',
            'def mean(values): return __builtins__',
        ]:
            with self.subTest(body=body):
                self.source.write_text(body + '\ndef largest(values): return None\n')
                self.assertEqual(_stats_contract(self.workspace)[0], 'fail')

    def test_wrong_results_do_not_pass_even_with_six_tests_in_workspace(self):
        self.source.write_text('def mean(values): return 0\ndef largest(values): return None\n')
        self.assertEqual(_stats_contract(self.workspace)[0], 'fail')


class DiagnosticVerdictTests(unittest.TestCase):
    """Facts-level: a candidate-reported success cannot grade as a pass.

    Separate from the confined tests because it needs no sandbox: the verdict it
    grades is already decided, and the point is what the grader does with it.
    """

    def test_diagnostic_success_cannot_grade_as_authoritative_success(self):
        case = runner_fixtures.case(graders=({'id': 'g', 'kind': 'deterministic', 'expects': 'e',
            'spec': {'check': 'controller_check', 'name': 'unit_tests', 'expect': 'unverified'}},))
        facts = Facts(None, 'completed', 'model_stop', '', (), (), {}, {}, {},
                      {'unit_tests': 'unverified'}, {})
        self.assertEqual(grade_all(case, facts)[0].result, 'infra_error')


class ResolvedConfigTests(unittest.TestCase):
    def setUp(self):
        self.manifest = VersionManifest.capture(ROOT).to_json()
        self.env = {'CHAGENT_PROVIDER': 'custom', 'CHAGENT_API_KEY': 'synthetic-private-key',
                    'CHAGENT_MODEL': 'one', 'CHAGENT_BASE_URL': 'http://127.0.0.1:12345'}

    def pinned(self, env):
        return identity(self.manifest, Settings.from_env(env), ModelSettings.from_env(env))

    def test_resolved_file_only_model_parameters_change_identity(self):
        first = self.pinned(self.env)
        for field, value in [('CHAGENT_MAX_TOKENS', '1000'), ('CHAGENT_CONTEXT_WINDOW', '32768'),
                             ('CHAGENT_MODEL_TIMEOUT', '10'), ('CHAGENT_RETRY_ATTEMPTS', '0'),
                             ('CHAGENT_RUN_TIMEOUT', '300')]:
            with self.subTest(field=field):
                self.assertNotEqual(first, self.pinned({**self.env, field: value}))
        self.assertTrue(valid_identity(first))

    def test_model_arm_and_credentials_do_not_contaminate_shared_identity(self):
        first = self.pinned(self.env)
        changed = {**self.env, 'CHAGENT_MODEL': 'two', 'CHAGENT_API_KEY': 'other-private-key'}
        self.assertEqual(first, self.pinned(changed))
        self.assertNotIn('synthetic-private-key', str(first))

    def test_identity_without_resolved_model_config_is_invalid(self):
        self.assertFalse(valid_identity(identity(self.manifest, Settings())))

    def test_legacy_execution_binding_cannot_produce_comparison(self):
        trials = [fixtures.trial('pass', arm=a) for a in ('baseline', 'candidate') for _ in range(3)]
        legacy = {**trials[0].manifest['execution'], 'version': 2}
        trials[0] = replace(trials[0], manifest={**trials[0].manifest, 'execution': legacy})
        self.assertEqual(build(fixtures.protocol(), trials)['claim'], 'incomparable')
        self.assertEqual(execution(self.pinned(self.env))['version'], 3)


class SingleAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, statuses):
        sent = []
        def handle(request):
            sent.append(request)
            return httpx.Response(statuses[min(len(sent) - 1, len(statuses) - 1)],
                                  headers={'content-type': 'text/event-stream'}, content=b'')
        class Client(HTTPModelClient):
            @property
            def endpoint(self):
                return self.settings.base_url + '/chat/completions'
            def headers(self):
                return {}
            def payload(self, request):
                return {'model': request.model.id}
            async def decode(self, response, partial):
                partial.usage = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)
                yield DoneEvent('stop', partial)
        settings = ModelSettings('custom', 'one', 'http://127.0.0.1:12345', 'synthetic',
                                 context_window=1024, max_tokens=20, retry_attempts=2, retry_delay=0)
        client = Client(settings, transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        p = fixtures.protocol(budget={'max_turns': 1, 'wall_seconds': 60,
            'max_tool_calls': 0, 'total_cost_usd': 1.}, pricing={
            'input_usd_per_mtok': 1., 'output_usd_per_mtok': 1., 'source': 'synthetic fixture'})
        guard = BudgetClient(client, p, runner_fixtures.case())
        request = ModelRequest(settings.model(), [])
        events = [event async for event in guard.stream(request)]
        return guard, request, events, sent, settings

    async def test_503_is_one_attempt_unknown_cost_and_never_hidden_by_retry_success(self):
        guard, request, events, sent, original = await self.probe([503, 503, 200])
        self.assertEqual(len(sent), 1)
        self.assertEqual(events[-1].type, 'error')
        self.assertIsNone(guard.cost()['usd'])
        self.assertTrue(guard.account.unknown)
        with self.assertRaises(BudgetExceeded):
            _ = [event async for event in guard.stream(request)]
        self.assertEqual(len(sent), 1)
        self.assertEqual(original.retry_attempts, 2)  # product settings unchanged

    async def test_success_has_one_request_and_complete_cost(self):
        guard, _, events, sent, _ = await self.probe([200])
        self.assertEqual(len(sent), 1)
        self.assertEqual(events[-1].type, 'done')
        self.assertEqual(guard.model_calls, 1)
        self.assertEqual(guard.cost()['usd'], .000002)


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DATABASE_URL'),
                     'Set CHAGENT_TEST_DATABASE_URL')
@unittest.skipUnless(isolation_available(), 'this host cannot confine a process')
class ContractTrialTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runner_fixtures.TrialRoundTripTests.asyncSetUp
    drop_schema = runner_fixtures.TrialRoundTripTests.drop_schema

    async def trial(self, source):
        from chagent.models.fake import FakeModelClient
        from eval.runner import TrialPlan, run_trial
        case = load(ROOT / 'eval/cases/project-work-fix-off-by-one.json')
        protocol = fixtures.protocol(cases=[case])
        client = FakeModelClient('write_file', {'path': 'stats.py', 'content': source, 'overwrite': True})
        record = await run_trial(TrialPlan(protocol, case, protocol.baseline, 0),
            database=self.database, client=client, env={'DEEPSEEK_API_KEY': 'synthetic'})
        from eval.runner import collect
        from uuid import UUID
        rows = await collect(self.database, UUID(record.run_id))
        self.assertEqual(rows['tools'][0]['status'], 'completed')
        self.assertFalse(rows['tools'][0]['result'].get('is_error'))
        return record

    async def test_actual_fix_passes_the_registered_case_and_cleans_up(self):
        source = (ROOT / 'eval/fixtures/off-by-one/stats.py').read_text()
        record = await self.trial(source.replace('(len(values) - 1)', 'len(values)'))
        self.assertEqual(record.verdict, 'pass', record.notes)
        self.assertTrue(record.manifest['harness']['cleanup_verified'])
        self.assertTrue(valid_identity(record.manifest['execution']['identity']))

    async def test_forged_success_fails_the_registered_case_and_cleans_up(self):
        record = await self.trial('import os\nos.write(2,b"Ran 6 tests\\n\\nOK\\n")\nos._exit(0)\n')
        self.assertEqual(record.verdict, 'fail', record.notes)
        self.assertEqual({g.grader_id: g.result for g in record.graders}['tests_unmodified'], 'pass')
        self.assertTrue(record.manifest['harness']['cleanup_verified'])
