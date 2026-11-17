import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from chagent.hostexec.environment_spec import normalize, resources
from chagent.hostexec.task_environment import TaskEnvironment
from chagent.tools.sandbox import Sandbox
from eval.verification import Verification, freeze_verifier


IMAGE = os.environ.get('CHAGENT_EXECUTOR_IMAGE', 'chagent:local')
LIMITS = {'cpus': .5, 'memory_mb': 256, 'pids': 64, 'tmpfs_mb': 64}
HEALTH = ['/app/.venv/bin/python', '-c', "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080', timeout=1).read()"]
SERVER = """import http.server, os
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.end_headers(); self.wfile.write(os.environ.get('REPLY','42').encode())
 def log_message(self,*args): pass
http.server.HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
"""
FRONTEND = """import http.server, urllib.request
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  answer=urllib.request.urlopen('http://backend:8080',timeout=1).read()
  self.send_response(200); self.end_headers(); self.wfile.write(answer)
 def log_message(self,*args): pass
http.server.HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
"""


def specification():
    return deepcopy({'version': 1, 'network': 'internal', 'resources': LIMITS, 'verifier_resources': LIMITS,
            'services': [
                {'name': 'frontend', 'image': IMAGE, 'command': ['/app/.venv/bin/python', '-c', FRONTEND],
                 'resources': LIMITS, 'depends_on': ['backend'], 'healthcheck': HEALTH, 'startup_timeout': 10},
                {'name': 'backend', 'image': IMAGE, 'command': ['/app/.venv/bin/python', '-c', SERVER],
                 'env': {'REPLY': '42'}, 'resources': LIMITS, 'healthcheck': HEALTH, 'startup_timeout': 10}]})


class TopologyValidationTests(unittest.TestCase):
    def test_dependency_order_and_resource_validation(self):
        spec = normalize(specification())
        self.assertEqual([s['name'] for s in spec['services']], ['backend', 'frontend'])
        self.assertEqual(spec['resources']['cpus'], .5)
        for invalid in ({'cpus': float('nan')}, {'memory_mb': -1}, {'pids': True},
                        {'tmpfs_mb': 2048}, {'unknown': 1}, {'memory_mb': 256.0}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                resources(invalid)

    def test_invalid_graph_and_host_capabilities_are_rejected(self):
        variants = []
        bad = specification(); bad['services'][1]['depends_on'] = ['frontend']; variants.append(bad)
        bad = specification(); bad['services'][0]['depends_on'] = ['missing']; variants.append(bad)
        bad = specification(); bad['services'][0]['name'] = 'backend'; variants.append(bad)
        bad = specification(); bad['network'] = 'none'; variants.append(bad)
        bad = specification(); bad['services'][0]['privileged'] = True; variants.append(bad)
        bad = specification(); bad['volumes'] = ['/tmp:/host']; variants.append(bad)
        bad = specification(); bad['resources']['cpus'] = 16; variants.append(bad)
        for index, value in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(ValueError):
                normalize(value)


@unittest.skipUnless(os.environ.get('CHAGENT_TEST_DOCKER') == '1', 'Set CHAGENT_TEST_DOCKER=1')
class TaskTopologyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chagent-topology-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'input.txt').write_text('fixture')
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if task.state.get('cleanup') != 'confirmed':
                await task.cleanup()
        self.temp.cleanup()

    async def create(self, name, spec=None, snapshot=None):
        task = await TaskEnvironment.create(self.root / name,
            workspace=None if snapshot else self.source, snapshot=snapshot,
            artifacts=['answer.txt'], spec=spec if spec is not None else specification())
        self.tasks.append(task)
        return task

    async def test_two_services_resource_limits_and_independent_verification(self):
        checks = self.root / 'checks'; checks.mkdir()
        (checks / 'check.py').write_text("def verify(artifacts):\n    return (artifacts/'answer.txt').read_bytes() == b'42'\n")
        plan = await freeze_verifier(checks, self.root / 'plan', resources=LIMITS)
        task = await self.create('full')
        result = await task.run("python3 -c \"import urllib.request; print(urllib.request.urlopen('http://frontend:8080').read().decode(),end='')\" > answer.txt")
        self.assertFalse(result.is_error, result)
        for field, expected in (('Memory', 256 * 1024**2), ('NanoCpus', 500000000), ('PidsLimit', 64)):
            raw = json.loads(await task.docker('inspect', task.name, '--format', '{{json .}}'))
            self.assertEqual(raw['HostConfig'][field], expected)
        self.assertTrue(task.state['network_identity']['internal'])
        self.assertEqual(list(task.state['services']), ['backend', 'frontend'])
        await task.collect()
        await task.cleanup()
        verifier = await Verification.create(task.output, plan, self.root / 'verified')
        try:
            await verifier.run()
            self.assertEqual(verifier.state['result'], 'pass')
            self.assertEqual(verifier.state['environment']['resources']['memory'], 256 * 1024**2)
        finally:
            await verifier.cleanup()
        self.assertEqual((self.source / 'input.txt').read_text(), 'fixture')
        self.assertNotEqual((await Sandbox._docker('network', 'inspect', task.services.network))[0], 0)

    async def test_frozen_spec_replay_private_network_and_cross_task_isolation(self):
        first = await self.create('one')
        second = await self.create('two', spec=first.state['environment_spec'], snapshot=first.output / 'snapshot.tar')
        self.assertEqual(first.state['environment_spec_sha256'], second.state['environment_spec_sha256'])
        self.assertNotEqual(first.state['network_identity']['id'], second.state['network_identity']['id'])
        remote = json.loads(await first.docker('inspect', first.services.name(first.services.services[-1]), '--format', '{{json .}}'))
        ip = remote['NetworkSettings']['Networks'][first.state['network']]['IPAddress']
        code = f"import socket\ns=socket.socket(); s.settimeout(.5)\ntry:\n s.connect(('{ip}',8080))\nexcept OSError:\n print('isolated')\nelse:\n raise RuntimeError('foreign service reachable')"
        result = await second.run('python3 -c ' + __import__('shlex').quote(code))
        self.assertFalse(result.is_error, result)
        self.assertIn('isolated', result.details['output'])

    async def test_service_health_failure_rolls_back_entire_graph(self):
        spec = specification()
        spec['services'] = [spec['services'][1]]
        spec['services'][0].update(healthcheck=['/bin/false'], startup_timeout=1)
        output = self.root / 'unhealthy'
        with self.assertRaisesRegex(RuntimeError, 'health timeout'):
            await TaskEnvironment.create(output, workspace=self.source, spec=spec)
        task = TaskEnvironment(output)
        self.assertEqual(task.state['cleanup'], 'confirmed')
        self.assertIn('health timeout', task.state['error'])
        self.assertNotEqual((await Sandbox._docker('network', 'inspect', task.services.network))[0], 0)
        self.assertTrue(task.state['service_final_states'])

    async def test_service_policy_drift_blocks_new_commands(self):
        task = await self.create('drift')
        name = task.services.name(task.services.services[0])
        await task.docker('update', '--cpus', '1', name)
        with self.assertRaisesRegex(RuntimeError, 'Service identity'):
            await task.run('true')
        self.assertIn('Service identity', task.state['admission_error'])

    async def test_foreign_network_endpoint_is_not_deleted_and_cleanup_can_recover(self):
        task = await self.create('foreign')
        code, identifier = await Sandbox._docker('run', '-d', '--network', task.state['network'],
            '--entrypoint', '/bin/sleep', task.state['image_id'], 'infinity')
        self.assertEqual(code, 0)
        try:
            with self.assertRaisesRegex(RuntimeError, 'foreign endpoints'):
                await task.admit()
            with self.assertRaises(RuntimeError):
                await task.cleanup()
            self.assertEqual(task.state['cleanup'], 'pending')
            self.assertEqual((await Sandbox._docker('inspect', identifier.strip()))[0], 0)
        finally:
            await Sandbox._docker('rm', '-f', identifier.strip())
        recovered = TaskEnvironment(task.output)
        await recovered.cleanup()
        task.state = recovered.state
        self.assertEqual(task.state['cleanup'], 'confirmed')

    async def test_network_modes_and_internal_network_without_services(self):
        for mode in ('none', 'bridge', 'internal'):
            task = await self.create('mode-' + mode, spec={'network': mode, 'resources': LIMITS})
            self.assertFalse((await task.run('true')).is_error)
            raw = json.loads(await task.docker('inspect', task.name, '--format', '{{json .}}'))
            self.assertEqual(raw['HostConfig']['NetworkMode'], task.state['network'])
            if mode == 'internal':
                self.assertTrue(task.state['network_identity']['internal'])
            else:
                self.assertEqual(task.state['network'], mode)
        bridge = specification()
        bridge['network'] = 'bridge'
        task = await self.create('bridge-services', spec=bridge)
        self.assertFalse(task.state['network_identity']['internal'])
        self.assertNotEqual(task.state['network'], 'bridge')

    async def test_real_controller_interruption_recovers_services_volumes_and_network(self):
        spec_file = self.root / 'interrupt-spec.json'
        spec_file.write_text(json.dumps(specification()))
        output = self.root / 'interrupted'
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'eval.cli', 'task-env', 'run',
            '--workspace', str(self.source), '--out', str(output), '--cmd', 'touch answer.txt; sleep 20',
            '--artifact', 'answer.txt', '--environment-spec', str(spec_file),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            for _ in range(150):
                state_file = output / 'environment.json'
                if state_file.exists():
                    state = json.loads(state_file.read_text())
                    if state.get('invocation'):
                        code, _ = await Sandbox._docker('exec', 'chagent-task-' + state['id'], 'test', '-f', '/workspace/answer.txt')
                        if code == 0:
                            break
                await asyncio.sleep(.1)
            else:
                self.fail('Multi-service task did not enter execution')
            process.kill()
            await process.wait()
            cleanup = await asyncio.create_subprocess_exec(sys.executable, '-m', 'eval.cli', 'task-env', 'cleanup',
                '--out', str(output), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await cleanup.communicate()
            self.assertEqual(cleanup.returncode, 0, stderr.decode())
            self.assertEqual(json.loads(stdout)['cleanup'], 'confirmed')
            recovered = TaskEnvironment(output)
            self.assertNotEqual((await Sandbox._docker('network', 'inspect', recovered.services.network))[0], 0)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if (output / 'environment.json').exists():
                await TaskEnvironment(output).cleanup()

    async def test_cli_spec_freeze_full_chain_and_health_failure_exit(self):
        checks = self.root / 'checks'; checks.mkdir()
        (checks / 'check.py').write_text("def verify(artifacts):\n    return (artifacts/'answer.txt').read_bytes().strip() == b'42'\n")
        spec_file = self.root / 'spec.json'
        spec_file.write_text(json.dumps(specification()))
        output = self.root / 'cli'
        command = "python3 -c \"import urllib.request; print(urllib.request.urlopen('http://frontend:8080').read().decode())\" > answer.txt"
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'eval.cli', 'task-env', 'run',
            '--workspace', str(self.source), '--out', str(output), '--cmd', command,
            '--artifact', 'answer.txt', '--environment-spec', str(spec_file), '--verifier', str(checks),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode())
        state = json.loads(stdout)
        self.assertEqual(state['verification']['grader']['result'], 'pass')
        self.assertEqual(state['cleanup'], 'confirmed')
        self.assertTrue((output / 'environment-spec.json').exists())
        self.assertEqual(state['verifier_plan']['resources'], LIMITS)
        bad = specification()
        bad['services'] = [bad['services'][1]]
        bad['services'][0].update(healthcheck=['/bin/false'], startup_timeout=1)
        spec_file.write_text(json.dumps(bad))
        failed = self.root / 'cli-failed'
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'eval.cli', 'task-env', 'run',
            '--workspace', str(self.source), '--out', str(failed), '--cmd', 'true',
            '--environment-spec', str(spec_file), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await process.communicate()
        self.assertEqual(process.returncode, 2, stderr.decode())
        self.assertEqual(json.loads((failed / 'environment.json').read_text())['cleanup'], 'confirmed')
