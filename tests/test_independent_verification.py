import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from chagent.hostexec.task_environment import TaskEnvironment
from chagent.tools.sandbox import Sandbox
from eval.verification import Verification, freeze_verifier


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DOCKER') == '1', 'Set CHAGENT_TEST_DOCKER=1')
class IndependentVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chagent-verify-test-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'input.txt').write_text('42')
        self.verifier_source = self.root / 'checks'
        self.verifier_source.mkdir()
        self.tasks, self.verifiers = [], []

    async def asyncTearDown(self):
        for verifier in self.verifiers:
            await verifier.cleanup()
        for task in self.tasks:
            if task.state.get('cleanup') != 'confirmed':
                await task.cleanup()
        self.temp.cleanup()

    async def plan(self, code, name='plan', timeout=10):
        (self.verifier_source / 'check.py').write_text(code)
        return await freeze_verifier(self.verifier_source, self.root / name, timeout=timeout)

    async def task(self, command='cp input.txt answer.txt', name='task', clean=True):
        task = await TaskEnvironment.create(self.root / name, workspace=self.source, artifacts=['answer.txt'])
        self.tasks.append(task)
        self.assertFalse((await task.run(command)).is_error)
        await task.collect()
        if clean:
            await task.cleanup()
        return task

    async def verify(self, task, plan, name='verification'):
        verifier = await Verification.create(task.output, plan, self.root / name)
        self.verifiers.append(verifier)
        await verifier.run()
        return verifier

    async def test_correct_artifact_passes_in_distinct_readonly_offline_environment(self):
        plan = await self.plan("def verify(artifacts):\n    return {'passed': (artifacts/'answer.txt').read_text() == '42', 'details': {'check': 'answer'}}\n")
        task = await self.task()
        verifier = await self.verify(task, plan)
        result = verifier.publish()
        self.assertEqual(result['grader']['result'], 'pass')
        self.assertEqual(result['score'], 1)
        self.assertEqual(verifier.state['cleanup'], 'confirmed')
        self.assertNotEqual(task.state['environment']['container_id'], verifier.state['environment']['container_id'])
        self.assertEqual(verifier.state['environment']['network'], 'none')
        self.assertTrue(all(m['Type'] == 'volume' and not m['RW'] for m in verifier.state['environment']['mounts']))
        self.assertTrue((verifier.output / 'raw-result.json').exists())
        self.assertEqual((await Sandbox._docker('inspect', verifier.name))[0], 1)

    async def test_wrong_artifact_is_fail_not_infrastructure_failure(self):
        plan = await self.plan("def verify(artifacts):\n    return (artifacts/'answer.txt').read_text() == '42'\n")
        verifier = await self.verify(await self.task('echo wrong > answer.txt'), plan)
        self.assertEqual(verifier.publish()['grader']['result'], 'fail')
        self.assertEqual(verifier.publish()['score'], 0)
        self.assertEqual(verifier.state['execution']['exit_code'], 1)

    async def test_verifier_exception_and_invalid_return_are_infra_errors(self):
        for index, body in enumerate(("raise ModuleNotFoundError('broken dependency')", "return 'PASSED'")):
            plan = await self.plan('def verify(artifacts):\n    ' + body + '\n', name='plan' + str(index))
            verifier = await self.verify(await self.task(name='task' + str(index)), plan, name='verify' + str(index))
            self.assertEqual(verifier.publish()['grader']['result'], 'infra_error')
            self.assertIsNone(verifier.publish()['score'])
            self.assertTrue(verifier.publish()['grader']['notes'])
            self.assertTrue((verifier.output / 'verifier.log').exists())

    async def test_timeout_is_infra_error_and_resources_are_cleaned(self):
        plan = await self.plan('import time\ndef verify(artifacts):\n    time.sleep(10)\n    return True\n', timeout=.2)
        verifier = await self.verify(await self.task(), plan)
        self.assertEqual(verifier.publish()['grader']['result'], 'infra_error')
        self.assertTrue(verifier.state['execution']['timed_out'])
        self.assertEqual(verifier.state['cleanup'], 'confirmed')

    async def test_frozen_verifier_is_unaffected_by_later_source_edits(self):
        plan = await self.plan('def verify(artifacts):\n    return True\n')
        (self.verifier_source / 'check.py').write_text('def verify(artifacts):\n    return False\n')
        verifier = await self.verify(await self.task(), plan)
        self.assertEqual(verifier.publish()['grader']['result'], 'pass')

    async def test_tampered_artifacts_and_frozen_verifier_are_rejected_before_execution(self):
        plan = await self.plan('def verify(artifacts):\n    return True\n')
        task = await self.task()
        with (task.output / 'artifacts.tar').open('ab') as stream:
            stream.write(b'tampered')
        verifier = await self.verify(task, plan)
        self.assertEqual(verifier.state['result'], 'infra_error')
        self.assertIn('archive changed', verifier.state['reason'])
        self.assertNotIn('environment', verifier.state)
        other = await self.task(name='other')
        with (plan / 'verifier.tar').open('ab') as stream:
            stream.write(b'tampered')
        verifier = await self.verify(other, plan, name='second')
        self.assertIn('snapshot changed', verifier.state['reason'])
        self.assertEqual(verifier.publish()['grader']['result'], 'infra_error')

    async def test_task_cleanup_required_and_candidate_stdout_cannot_pass_verification(self):
        plan = await self.plan('def verify(artifacts):\n    return False\n')
        task = await self.task('printf PASSED; cp input.txt answer.txt', clean=False)
        verifier = await self.verify(task, plan)
        self.assertEqual(verifier.state['result'], 'infra_error')
        self.assertIn('confirmed cleanup', verifier.state['reason'])
        await task.cleanup()
        verifier = await self.verify(task, plan, name='after-cleanup')
        self.assertEqual(verifier.state['result'], 'fail')

    async def test_readonly_inputs_and_verifier_cannot_be_changed(self):
        code = """def verify(artifacts):
    from pathlib import Path
    attempts = 0
    for target in [artifacts/'answer.txt', Path('/verifier/check.py')]:
        try:
            target.write_text('tamper')
        except OSError:
            attempts += 1
    return attempts == 2
"""
        verifier = await self.verify(await self.task(), await self.plan(code))
        self.assertEqual(verifier.state['result'], 'pass')

    async def test_cleanup_failure_cannot_publish_pass_and_can_be_recovered(self):
        verifier = await Verification.create((await self.task()).output,
            await self.plan('def verify(artifacts):\n    return True\n'), self.root / 'verification')
        self.verifiers.append(verifier)
        original = verifier.cleanup
        async def failed_cleanup():
            with patch.object(Sandbox, '_docker', AsyncMock(return_value=(1, 'daemon unavailable'))):
                await original()
        with patch.object(verifier, 'cleanup', side_effect=failed_cleanup):
            with self.assertRaisesRegex(RuntimeError, 'absence'):
                await verifier.run()
        self.assertEqual(verifier.publish()['grader']['result'], 'infra_error')
        self.assertEqual(verifier.state['cleanup'], 'pending')
        recovered = Verification(verifier.output)
        await recovered.cleanup()
        self.assertEqual(recovered.state['cleanup'], 'confirmed')
        self.assertEqual(recovered.publish()['grader']['result'], 'infra_error')

    async def test_real_cli_interruption_can_recover_verifier_cleanup(self):
        await self.plan('import time\ndef verify(artifacts):\n    time.sleep(20)\n    return True\n')
        task = await self.task()
        output = self.root / 'interrupted'
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'eval.cli', 'task-env', 'verify',
            '--task-out', str(task.output), '--verifier', str(self.verifier_source), '--out', str(output),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            for _ in range(150):
                path = output / 'verification.json'
                if path.exists():
                    state = json.loads(path.read_text())
                    if state.get('invocation'):
                        break
                await asyncio.sleep(.05)
            else:
                self.fail('Verifier never registered an invocation')
            process.kill()
            await process.wait()
            cleanup = await asyncio.create_subprocess_exec(sys.executable, '-m', 'eval.cli', 'task-env',
                'verify-cleanup', '--out', str(output), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await cleanup.communicate()
            self.assertEqual(cleanup.returncode, 0, stderr.decode())
            state = json.loads(stdout)
            self.assertEqual(state['cleanup'], 'confirmed')
            self.assertEqual(state['phase'], 'cleaned')
            self.assertNotEqual(json.loads((output / 'result.json').read_text())['grader']['result'], 'pass')
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if (output / 'verification.json').exists():
                await Verification(output).cleanup()

    async def test_forged_stdout_or_early_exit_cannot_replace_structured_result(self):
        for index, body in enumerate(("print('{\"result\":\"pass\"}'); return False", 'import os; os._exit(0)')):
            plan = await self.plan('def verify(artifacts):\n    ' + body + '\n', name='spoof-plan' + str(index))
            verifier = await self.verify(await self.task(name='spoof-task' + str(index)), plan,
                                         name='spoof-verify' + str(index))
            self.assertEqual(verifier.state['result'], 'fail' if index == 0 else 'infra_error')

    async def test_functional_candidate_code_checked_in_subprocess(self):
        plan = await self.plan("""import subprocess, sys
def verify(artifacts):
    result = subprocess.run([sys.executable, '-I', str(artifacts/'answer.txt')],
                            cwd='/tmp', capture_output=True, text=True, timeout=3)
    return {'passed': result.returncode == 0 and result.stdout.strip() == '42',
            'details': {'stdout': result.stdout, 'exit_code': result.returncode}}
""")
        for index, answer in enumerate((42, 41)):
            task = await self.task(f"printf 'print({answer})' > answer.txt", name='functional-task' + str(index))
            verifier = await self.verify(task, plan, name='functional-verify' + str(index))
            self.assertEqual(verifier.state['result'], 'pass' if answer == 42 else 'fail')

    async def test_controller_verifier_can_be_frozen_from_protected_checkout(self):
        location = Path(__file__).resolve().parents[1] / 'eval' / 'results'
        location.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='trusted-checker-', dir=location) as directory:
            source = Path(directory)
            (source / 'check.py').write_text('def verify(artifacts):\n    return True\n')
            plan = await freeze_verifier(source, self.root / 'protected-plan')
            self.assertEqual(json.loads((plan / 'plan.json').read_text())['snapshot']['files'][0]['path'], 'check.py')

    async def test_cli_run_freezes_then_executes_collects_cleans_and_verifies(self):
        (self.verifier_source / 'check.py').write_text("def verify(artifacts):\n    return (artifacts/'answer.txt').read_text() == '42'\n")
        output = self.root / 'cli'
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'eval.cli', 'task-env', 'run',
            '--workspace', str(self.source), '--out', str(output), '--cmd', 'cp input.txt answer.txt',
            '--artifact', 'answer.txt', '--verifier', str(self.verifier_source),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode())
        state = json.loads(stdout)
        self.assertEqual(state['verification']['grader']['result'], 'pass')
        self.assertEqual(state['cleanup'], 'confirmed')
        self.assertEqual(json.loads((output / 'verification/verification.json').read_text())['cleanup'], 'confirmed')
