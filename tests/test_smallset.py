"""Versioned calibration inputs and a real-Docker E4 regression entry."""
import asyncio
import json
from types import SimpleNamespace
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from eval.smallset import DEFAULT_SUITE, load_suite, run_suite, inspect_trial, absence
import test_container_trial as fixtures


class SmallsetContractTests(unittest.TestCase):
    def test_fixed_suite_includes_offline_repair_and_service_dependency(self):
        suite, files, digest = load_suite(DEFAULT_SUITE)
        self.assertEqual(suite['repeats'], 3)
        self.assertEqual({t['name'] for t in suite['tasks']}, {'sum', 'repair', 'services'})
        service = next(t for t in suite['tasks'] if t['name'] == 'services')
        self.assertEqual(service['network'], 'internal')
        self.assertEqual(service['services'][0]['depends_on'], ['backend'])
        self.assertEqual(len(digest), 64)
        self.assertIn('suite.json', files)
        self.assertNotIn('controls', {Path(name).name for name in files if '/input/' in name})

    def test_changed_fixture_changes_suite_identity_and_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'suite'
            shutil.copytree(DEFAULT_SUITE, target, ignore=shutil.ignore_patterns('__pycache__'))
            original = load_suite(target)[2]
            (target / 'sum/input/numbers.json').write_text('[1,2,3]')
            self.assertNotEqual(load_suite(target)[2], original)
            (target / 'sum/input/link').symlink_to('/etc/hosts')
            with self.assertRaises(ValueError):
                load_suite(target)

    def test_unknown_fields_and_duplicate_tasks_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'suite'
            shutil.copytree(DEFAULT_SUITE, target, ignore=shutil.ignore_patterns('__pycache__'))
            document = json.loads((target / 'suite.json').read_text())
            document['tasks'].append(document['tasks'][0])
            (target / 'suite.json').write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                load_suite(target)

    def test_wrong_task_prompt_and_context_limits_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            request = {'purpose': 'task', 'model': {'context_window': 16384, 'max_tokens': 512},
                'messages': [{'sections': {'environment': 'Boundary: disposable task container. Network mode: none.'},
                              'content': '', 'tools_added': [{'name': 'exec_command', 'parameters': {}}]},
                             {'role': 'user', 'content': 'wrong task'}]}
            record = SimpleNamespace(manifest={'container_task': {'evidence': str(path)},
                                              'harness': {'client_kind': 'synthetic'}})
            for invalid in ('prompt', 'context'):
                if invalid == 'context':
                    request['messages'][1]['content'] = 'expected task'
                    request['model']['context_window'] = 65536
                (path / 'run-evidence.json').write_text(json.dumps({'calls': [{'request': request}]}))
                with self.assertRaisesRegex(ValueError, 'tool/context'):
                    asyncio.run(inspect_trial(record, {'prompt': 'expected task', 'network': 'none'}, {}, database=None))

    def test_unrelated_docker_errors_do_not_prove_resource_absence(self):
        from unittest.mock import AsyncMock
        with patch('eval.smallset.Sandbox._docker', new=AsyncMock(return_value=(1, 'plugin not found'))):
            with self.assertRaises(ValueError):
                asyncio.run(absence(('network', 'inspect', 'owned-network')))
        with patch('eval.smallset.Sandbox._docker', new=AsyncMock(return_value=(1, 'network owned-network not found'))):
            asyncio.run(absence(('network', 'inspect', 'owned-network')))


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DATABASE_URL') and os.environ.get('CHAGENT_TEST_DOCKER') == '1',
                     'Set CHAGENT_TEST_DATABASE_URL and CHAGENT_TEST_DOCKER=1')
class SmallsetDockerTests(unittest.IsolatedAsyncioTestCase):
    drop_schema = fixtures.ContainerTrialTests.drop_schema
    protocol_for = fixtures.ContainerTrialTests.protocol_for
    cleanup_resources = fixtures.ContainerTrialTests.cleanup_resources

    async def asyncSetUp(self):
        await fixtures.ContainerTrialTests.asyncSetUp(self)
        self.addAsyncCleanup(self.cleanup_calibration)

    async def cleanup_calibration(self):
        from eval.container_trial import cleanup_trial
        for path in self.root.glob('calibration/results/*/container-trials/*'):
            await cleanup_trial(path)

    async def test_fixed_controls_reproduce_on_real_docker_and_verify_cleanup(self):
        report = await run_suite(DEFAULT_SUITE, self.root / 'calibration', database=self.database)
        self.assertTrue(report['calibration_passed'], report['errors'])
        self.assertEqual((report['planned'], report['observed']), (18, 18))
        self.assertEqual(sum(o['verdict'] == 'pass' for o in report['observations']), 9)
        self.assertEqual(sum(o['verdict'] == 'fail' for o in report['observations']), 9)
        self.assertEqual(report['raw_trials']['binding_errors'], [])
        with self.assertRaises(FileExistsError):
            await run_suite(DEFAULT_SUITE, self.root / 'calibration', database=self.database)

    async def test_unexpected_evidence_stops_without_losing_trial_denominator(self):
        with patch('eval.smallset.inspect_trial', new=AsyncMock(side_effect=ValueError('injected evidence mismatch'))):
            report = await run_suite(DEFAULT_SUITE, self.root / 'calibration', database=self.database)
        self.assertFalse(report['calibration_passed'])
        self.assertEqual(report['observed'], 1)
        self.assertEqual(report['verified_observations'], 0)
        self.assertEqual(report['missing'], 17)
        self.assertIn('injected evidence mismatch', report['errors'][0])
