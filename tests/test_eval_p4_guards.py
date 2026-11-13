"""P4 acceptance regressions: evidence cannot be rewritten or silently credited."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_eval_report as report_fixtures
import test_eval_runner as runner_fixtures
from eval import prereg, report
from eval.budgets import BudgetClient, BudgetExceeded, CostAccount
from eval.integrity import experiment_lock, publish
from eval.records import TrialInvalid, TrialStore
from eval.replay.sandbox import Workspace
from eval.runner import TrialPlan, run_trial, _unit_tests


def complete_trials():
    return [report_fixtures.trial('pass', arm=arm) for arm in ('baseline', 'candidate') for _ in range(3)]


class IntervalRegressions(unittest.TestCase):
    def test_zero_of_three_against_three_of_three_is_not_certain(self):
        self.assertEqual(report.newcombe(report.wilson(0, 3), report.wilson(3, 3), 0., 1.),
                         (.2059, 1.))

    def test_reversing_arms_reverses_interval(self):
        self.assertEqual(report.newcombe(report.wilson(3, 3), report.wilson(0, 3), 1., 0.),
                         (-1., -.2059))


class BindingRegressions(unittest.TestCase):
    def test_wrong_protocol_or_case_hash_never_produces_comparison(self):
        for changes in ({'protocol_sha256': 'wrong'}, {'case': {'id': 'other'}},
                        {'repeat': None}, {'repeat': 3}, {'model': {'requested_id': 'other'}}):
            with self.subTest(changes=changes):
                trials = complete_trials()
                trials[0] = replace(trials[0], manifest={**trials[0].manifest, **changes})
                result = report.build(report_fixtures.protocol(), trials)
                self.assertEqual(result['claim'], 'incomparable')
                self.assertIsNone(result['cases'][0]['delta']['interval'])

    def test_duplicate_identity_or_slot_is_rejected(self):
        trials = complete_trials()
        self.assertEqual(report.build(report_fixtures.protocol(), trials * 2)['claim'], 'incomparable')
        trials[1] = replace(trials[1], manifest={**trials[1].manifest,
                                              'repeat': trials[0].manifest['repeat']})
        self.assertEqual(report.build(report_fixtures.protocol(), trials)['claim'], 'incomparable')

    def test_code_changes_with_valid_digests_are_still_incomparable(self):
        from eval.integrity import execution
        trials = complete_trials()
        identity = dict(trials[0].manifest['execution']['identity'])
        identity['repo'] = {'commit': 'b' * 40, 'dirty': False, 'patch_sha256': None}
        changed = execution(identity)
        changed['end_identity_sha256'] = changed['identity_sha256']
        trials[0] = replace(trials[0], manifest={**trials[0].manifest, 'execution': changed})
        self.assertEqual(report.build(report_fixtures.protocol(), trials)['claim'], 'incomparable')

    def test_behavior_settings_changes_modify_execution_identity(self):
        from chagent.config import Settings
        from eval.integrity import identity
        from eval.manifests import VersionManifest
        manifest = VersionManifest.capture(Path(__file__).resolve().parent.parent).to_json()
        first = identity(manifest, Settings(deferred_tools=True))
        second = identity(manifest, Settings(deferred_tools=False))
        self.assertNotEqual(first, second)
        self.assertNotIn('database_url', json.dumps(first))

    def test_incomplete_identity_cannot_be_self_attested(self):
        from eval.integrity import execution
        trials = complete_trials()
        trials[0] = replace(trials[0], manifest={**trials[0].manifest,
                                              'execution': execution({'repo': {'commit': 'a' * 40}})})
        self.assertEqual(report.build(report_fixtures.protocol(), trials)['claim'], 'incomparable')

    def test_partially_known_cost_is_not_a_known_total(self):
        trials = complete_trials()
        trials[0] = replace(trials[0], usage={**trials[0].usage, 'cost': {'usd': None}})
        result = report.build(report_fixtures.protocol(), trials)
        self.assertIsNone(result['budget']['cost']['estimated_usd'])
        self.assertEqual(result['budget']['cost']['unknown_trials'], 1)
        self.assertEqual(result['claim'], 'inconclusive')

    def test_unverified_cleanup_has_infrastructure_precedence(self):
        trial = complete_trials()[0]
        broken = replace(trial, manifest={**trial.manifest, 'harness': {'cleanup_verified': False}})
        self.assertEqual(broken.verdict, 'infra_error')


class PublicationRegressions(unittest.TestCase):
    def test_changed_protocol_cannot_replace_frozen_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = report_fixtures.protocol()
            path = prereg.freeze(original, root)
            before = path.read_bytes()
            changed = report_fixtures.protocol(thresholds={'improve_points': .9, 'regress_points': .9})
            with self.assertRaises(prereg.ProtocolInvalid):
                prereg.freeze(changed, root)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(prereg.freeze(original, root), path)

    def test_failed_result_cannot_be_overwritten_or_add_an_index_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TrialStore(Path(tmp))
            failed = report_fixtures.trial('fail', arm='baseline')
            path = store.write(failed)
            before = path.read_bytes()
            passed = replace(failed, graders=report_fixtures.trial('pass', arm='baseline').graders)
            with self.assertRaises(TrialInvalid):
                store.write(passed)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len((path.parent / 'index.jsonl').read_text().splitlines()), 1)

    def test_atomic_claim_has_only_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        with tempfile.TemporaryDirectory() as tmp:
            gate = Barrier(2)
            path = Path(tmp) / 'slot.claim'
            def claim(number):
                gate.wait()
                try:
                    publish(path, {'worker': number})
                    return True
                except ValueError:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(claim, n) for n in (1, 2)]
                self.assertEqual(sum(f.result() for f in futures), 1)
            self.assertIn(json.loads(path.read_text())['worker'], (1, 2))

    def test_experiment_lock_refuses_a_second_executor(self):
        with tempfile.TemporaryDirectory() as tmp, experiment_lock(Path(tmp)):
            with self.assertRaises(ValueError):
                with experiment_lock(Path(tmp)):
                    self.fail('acquired a competing lock')


class BudgetRegressions(unittest.IsolatedAsyncioTestCase):
    def guard(self, *, synthetic=False, pricing=None, cap=1., max_tools=4, max_turns=3):
        from chagent.models.fake import FakeModelClient
        class Counted(FakeModelClient):
            calls = 0
            async def stream(self, request):
                self.calls += 1
                async for event in super().stream(request):
                    yield event
        client = Counted('write_file', {'path': 'x', 'content': 'y'})
        p = report_fixtures.protocol(budget={'max_turns': max_turns, 'wall_seconds': 240,
            'max_tool_calls': max_tools, 'total_cost_usd': cap}, pricing=pricing)
        return BudgetClient(client, p, runner_fixtures.case(), synthetic=synthetic), client

    def request(self):
        from chagent.models.base import Model, ModelRequest
        from chagent.domain.messages import UserMessage
        return ModelRequest(Model('one', 'test', 'test', 100, 20), [UserMessage('hi', 0)], max_tokens=10)

    async def consume(self, guard):
        return [e async for e in guard.stream(self.request())]

    async def test_missing_pricing_prevents_even_the_first_call(self):
        guard, client = self.guard()
        with self.assertRaises(BudgetExceeded):
            await self.consume(guard)
        self.assertEqual(client.calls, 0)

    async def test_conservative_reservation_refuses_an_unaffordable_call(self):
        guard, client = self.guard(cap=.1, pricing={'input_usd_per_mtok': 1000.,
            'output_usd_per_mtok': 1000., 'source': 'fixture'})
        with self.assertRaises(BudgetExceeded):
            await self.consume(guard)
        self.assertEqual(client.calls, 0)

    async def test_tool_batch_over_cap_never_reaches_the_executor(self):
        from chagent.models.events import DoneEvent
        guard, client = self.guard(synthetic=True, max_tools=0)
        delivered = []
        with self.assertRaises(BudgetExceeded):
            async for event in guard.stream(self.request()):
                delivered.append(event)
        self.assertFalse(any(isinstance(event, DoneEvent) for event in delivered))
        self.assertEqual(guard.tool_calls, 0)

    async def test_turn_limit_is_checked_before_request(self):
        guard, client = self.guard(synthetic=True, max_turns=1)
        await self.consume(guard)
        with self.assertRaises(BudgetExceeded):
            await self.consume(guard)
        self.assertEqual(client.calls, 1)

    async def test_missing_usage_makes_cost_unknown_and_stops_further_calls(self):
        guard, client = self.guard(pricing={'input_usd_per_mtok': 1.,
            'output_usd_per_mtok': 1., 'source': 'fixture'})
        await self.consume(guard)
        self.assertIsNone(guard.cost()['usd'])
        with self.assertRaises(BudgetExceeded):
            await self.consume(guard)
        self.assertEqual(client.calls, 1)

    async def test_cached_input_is_included_in_cost_accounting(self):
        from chagent.domain.messages import Usage
        from chagent.models.events import DoneEvent
        guard, client = self.guard(pricing={'input_usd_per_mtok': 1.,
            'output_usd_per_mtok': 1., 'source': 'fixture'})
        original = client.stream
        async def usage_stream(request):
            async for event in original(request):
                if isinstance(event, DoneEvent):
                    event.message.usage = Usage(input=0, output=1, cache_read=90,
                                                cache_write=0, total_tokens=91)
                yield event
        client.stream = usage_stream
        await self.consume(guard)
        self.assertAlmostEqual(guard.cost()['usd'], .000091)

    def test_unknown_or_nonfinite_accounting_cannot_open_a_budget(self):
        for cap, spent in ((1., None), (float('nan'), 0.), (1., float('inf'))):
            with self.assertRaises(BudgetExceeded):
                CostAccount(cap, spent)


class OrchestrationRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_claim_survives_crash_and_completed_slot_is_never_rerun(self):
        from contextlib import asynccontextmanager, redirect_stdout
        import io
        from types import SimpleNamespace
        from eval.cli import _run_plans
        from eval.records import new_trial, GraderResult

        @asynccontextmanager
        async def database(*args):
            yield object()

        for crash in (False, True):
            with self.subTest(crash=crash), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = TrialStore(root)
                p = report_fixtures.protocol(pricing={'input_usd_per_mtok': 1.,
                    'output_usd_per_mtok': 1., 'source': 'fixture'})
                c = replace(runner_fixtures.case(), id='demo/case')
                plan = TrialPlan(p, c, p.baseline, 0)
                calls = []
                async def run(plan, **kwargs):
                    calls.append(plan.trial_id)
                    if crash:
                        raise RuntimeError('synthetic crash after claiming the slot')
                    anchor = json.loads((store.directory(p.experiment) / 'execution.anchor').read_text())
                    anchor['end_identity_sha256'] = anchor['identity_sha256']
                    manifest = {'arm': plan.arm.name, 'protocol_sha256': p.sha256,
                        'case': p.cases[0], 'repeat': 0, 'execution': anchor,
                        'model': {'provider': plan.arm.provider, 'requested_id': plan.arm.model},
                        'harness': {'cleanup_verified': True, 'client_kind': 'live'}}
                    return new_trial(c, manifest, p.experiment, trial_id=plan.trial_id,
                        status='completed', usage={'model_calls': 0, 'tool_calls': 0,
                        'wall_seconds': 0, 'cost': {'usd': 0.}}).with_graders(
                            tuple(GraderResult(g['id'], 'pass') for g in c.graders))
                args = SimpleNamespace(db=None, schema='chagent_eval_fixture')
                with patch('eval.cli._eval_database', database), patch('eval.runner.run_trial', run), \
                     redirect_stdout(io.StringIO()):
                    if crash:
                        with self.assertRaises(RuntimeError):
                            await _run_plans([plan], args, root, store, {}, [])
                    else:
                        await _run_plans([plan], args, root, store, {}, [])
                    with self.assertRaises(ValueError):
                        await _run_plans([plan], args, root, store, {}, [])
                    if crash:
                        other_arm = TrialPlan(p, c, p.candidate, 0)
                        with self.assertRaises(ValueError):
                            await _run_plans([other_arm], args, root, store, {}, [])
                self.assertEqual(len(calls), 1)
                self.assertTrue((store.directory(p.experiment) / '.claims' / (plan.trial_id + '.claim')).is_file())

    def test_blocked_and_interleaved_order_match_the_protocol(self):
        from types import SimpleNamespace
        from eval.cli import _slots
        c = replace(runner_fixtures.case(), id='demo/case')
        args = SimpleNamespace(only=None, arm=None, all=True)
        for order, expected in [('blocked', ['baseline'] * 3 + ['candidate'] * 3),
                                ('interleaved', ['baseline', 'candidate'] * 3)]:
            p = report_fixtures.protocol(order=order)
            self.assertEqual([p.arm.name for p in _slots(p, {'demo/case@1': c}, args, [])], expected)


class IsolationRegressions(unittest.TestCase):
    def test_env_reads_writes_network_and_process_launch_are_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            (workspace / 'tests').mkdir(parents=True)
            secret, escaped = root / 'secret.txt', root / 'escaped.txt'
            secret.write_text('synthetic marker only')
            parent_net = os.stat('/proc/self/ns/net').st_ino if Path('/proc/self/ns/net').exists() else 0
            code = f'''import os,socket,subprocess,sys,unittest
from pathlib import Path
class T(unittest.TestCase):
 def test_env(self): self.assertNotIn('P4_SECRET_MARKER',os.environ)
 def test_read(self):
  with self.assertRaises(OSError): Path({str(secret)!r}).read_text()
 def test_write(self):
  with self.assertRaises(OSError): Path({str(escaped)!r}).write_text('bad')
 def test_workspace_readonly(self):
  with self.assertRaises(OSError): Path('modified.txt').write_text('bad')
 def test_network(self):
  if sys.platform.startswith('linux'):
   self.assertNotEqual(os.stat('/proc/self/ns/net').st_ino,{parent_net})
  else:
   with self.assertRaises(PermissionError): socket.create_connection(('127.0.0.1',9),timeout=.1)
 def test_exec(self):
  if sys.platform=='darwin':
   with self.assertRaises(PermissionError): subprocess.run(['/usr/bin/true'],check=True)
  else:
   child=subprocess.run([sys.executable,'-c',"open({str(escaped)!r},'w').write('bad')"],capture_output=True)
   self.assertNotEqual(child.returncode,0)
'''
            (workspace / 'tests' / 'test_limits.py').write_text(code)
            with patch.dict(os.environ, {'P4_SECRET_MARKER': 'synthetic, not a credential'}):
                verdict, detail = _unit_tests(workspace)
            self.assertEqual(verdict, 'pass', detail)
            self.assertFalse(escaped.exists())
            self.assertFalse((workspace / 'modified.txt').exists())

    def test_missing_backend_is_unavailable_not_unsandboxed_fallback(self):
        from eval.isolation import IsolationUnavailable
        with patch('eval.isolation.command', side_effect=IsolationUnavailable('no backend')):
            verdict, _ = _unit_tests(Path('/tmp'))
        self.assertEqual(verdict, 'unavailable')


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DATABASE_URL'), 'Set CHAGENT_TEST_DATABASE_URL')
class TrialGuardRegressions(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runner_fixtures.TrialRoundTripTests.asyncSetUp
    drop_schema = runner_fixtures.TrialRoundTripTests.drop_schema

    async def execute(self, *, tools=4, turns=4, cleanup=False, drift=False):
        from chagent.models.fake import FakeModelClient
        case = runner_fixtures.case()
        arms = (prereg.Arm('baseline', 'deepseek', 'one'), prereg.Arm('candidate', 'deepseek', 'two'))
        protocol = prereg.build(experiment='guard-regression', question='q', hypothesis='h',
            cases=[case], arms=arms, repeats=3, min_repeats_for_stability=3, order='interleaved',
            budget={'max_turns': turns, 'wall_seconds': 60, 'max_tool_calls': tools, 'total_cost_usd': 1},
            thresholds={'improve_points': .2, 'regress_points': .2}, analysis='a', exclusions='e', stopping_rule='s')
        plan = TrialPlan(protocol, case, arms[0], 0)
        original_cleanup = Workspace.cleanup
        def failed_cleanup(workspace):
            return replace(original_cleanup(workspace), verified=False, note='synthetic verification failure')
        import eval.runner as module
        original_manifest = module._manifest
        def changed_manifest(*args):
            result = original_manifest(*args)
            result['repo'] = {'commit': 'b' * 40, 'dirty': False, 'patch_sha256': None}
            return result
        with patch.object(Workspace, 'cleanup', failed_cleanup if cleanup else original_cleanup), \
             patch.object(module, '_manifest', changed_manifest if drift else original_manifest):
            return await run_trial(plan, database=self.database,
                client=FakeModelClient('write_file', {'path': 'note.txt', 'content': 'hi'}),
                env={'DEEPSEEK_API_KEY': 'synthetic', 'CHAGENT_PROVIDER': 'deepseek'})

    async def test_zero_tool_budget_prevents_a_real_tool_call(self):
        trial = await self.execute(tools=0)
        self.assertEqual(trial.usage['tool_calls'], 0)
        self.assertEqual(trial.verdict, 'fail')
        self.assertIn('tool call budget', trial.manifest['harness']['budget_violation'])

    async def test_turn_limit_cannot_pass_even_if_file_grader_passed(self):
        trial = await self.execute(turns=1)
        self.assertEqual(trial.usage['model_calls'], 1)
        self.assertEqual(trial.verdict, 'fail')

    async def test_cleanup_failure_overrides_all_passing_graders(self):
        trial = await self.execute(cleanup=True)
        self.assertFalse(trial.manifest['harness']['cleanup_verified'])
        self.assertEqual(trial.verdict, 'infra_error')
        self.assertEqual(trial.usage['model_calls'], 2)

    async def test_execution_change_during_trial_is_infrastructure(self):
        trial = await self.execute(drift=True)
        self.assertEqual(trial.verdict, 'infra_error')
        self.assertIn('identity changed', trial.manifest['harness']['error'])
