"""Independent, offline verification of a cleaned task's frozen artifact archive.

Verifier code is controller-owned. Its verify(Path('/artifacts')) function must
return a bool or {'passed': bool, 'details': JSON object}. Runtime exceptions,
missing evidence and infrastructure failures never become an ordinary zero score.
"""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import stat
import tempfile
from uuid import uuid4

from chagent.hostexec.task_environment import (
    MAX_BYTES, MAX_FILES, TaskEnvironment, TaskSandbox, archive_manifest, snapshot_workspace,
)
from chagent.tools.command import ExecCommandTool
from chagent.hostexec.environment_spec import resources as normalize_resources, policy as resource_policy, resource_differences
from chagent.tools.context import ToolContext
from chagent.tools.sandbox import Sandbox
from eval.records import GraderResult


DRIVER = r'''
import json, runpy, sys, traceback
from pathlib import Path
result_path, token = Path(sys.argv[1]), sys.argv[2]
result = {'version': 1, 'token': token, 'result': 'infra_error', 'details': {}}
try:
    sys.dont_write_bytecode = True
    sys.path.insert(0, '/verifier')
    namespace = runpy.run_path('/verifier/check.py')
    value = namespace['verify'](Path('/artifacts'))
    if type(value) is bool:
        value = {'passed': value, 'details': {}}
    if type(value) is not dict or type(value.get('passed')) is not bool or type(value.get('details', {})) is not dict:
        raise ValueError('verify must return a bool or passed/details object')
    result.update(result='pass' if value['passed'] else 'fail', details=value.get('details', {}))
    # Validation also rejects NaN and non-JSON details before publishing a score.
    if len(json.dumps(result, allow_nan=False).encode()) > 32768:
        raise ValueError('verifier details exceed 32 KiB')
except BaseException as error:
    traceback.print_exc()
    result.update(result='infra_error', details={'error': type(error).__name__, 'message': str(error)[:1000]})
encoded = json.dumps(result, allow_nan=False)
result_path.write_text(encoded)
sys.exit({'pass': 0, 'fail': 1, 'infra_error': 2}[result['result']])
'''


READ_RESULT = r'''import os, stat, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd, 'rb') as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_size > 32768:
        raise ValueError('result must be a regular file at most 32 KiB')
    data = stream.read(32769)
    if len(data) > 32768:
        raise ValueError('result grew beyond 32 KiB')
    sys.stdout.buffer.write(data)
'''


async def freeze_verifier(source, output, *, image=None, timeout=120, resources=None):
    """Freeze trusted code and image before the candidate starts when used by run."""
    resources = normalize_resources(resources)
    if not math.isfinite(timeout) or not .1 <= timeout <= 600:
        raise ValueError('Verifier timeout must be .1–600 seconds')
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    if not source.is_dir() or not (source / 'check.py').is_file():
        raise ValueError('Verifier directory must contain check.py')
    if output == source or output.is_relative_to(source):
        raise ValueError('Verifier freeze output must be outside verifier source')
    output.mkdir(parents=True, exist_ok=False)
    # Controller code may live in the protected agent checkout. Copy only this
    # explicitly selected tree to staging; user task workspace rules stay intact.
    with tempfile.TemporaryDirectory(prefix='chagent-verifier-freeze-',
                                     dir=os.environ.get('CHAGENT_WORKSPACE_ROOT')) as temp:
        staging = Path(temp)
        total = count = 0
        for path in sorted(source.rglob('*')):
            count += 1
            before = path.lstat()
            if count > MAX_FILES:
                raise ValueError('Verifier contains too many entries')
            target = staging / path.relative_to(source)
            if stat.S_ISDIR(before.st_mode):
                target.mkdir()
            elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                        raise ValueError('Verifier file identity changed')
                    total += opened.st_size
                    if total > MAX_BYTES:
                        raise ValueError('Verifier snapshot exceeds 100 MiB')
                    content = stream.read(MAX_BYTES + 1)
                    after = os.fstat(stream.fileno())
                    if len(content) != opened.st_size or (opened.st_mtime_ns, opened.st_ctime_ns) != (
                            after.st_mtime_ns, after.st_ctime_ns):
                        raise ValueError('Verifier changed while freezing')
                    target.write_bytes(content)
                    target.chmod(before.st_mode & 0o777)
            else:
                raise ValueError('Verifier snapshot rejects links and special files')
        manifest = snapshot_workspace(staging, output / 'verifier.tar')
    image = image or os.environ.get('CHAGENT_EXECUTOR_IMAGE', 'chagent:local')
    image_id = (await TaskEnvironment.docker('image', 'inspect', image, '--format', '{{.Id}}')).strip()
    plan = {'version': 1, 'protocol': 'verify-function-v1', 'snapshot': manifest,
            'frozen_at': datetime.now(timezone.utc).isoformat(),
            'image_id': image_id, 'timeout': timeout, 'network': 'none', 'resources': resources,
            'driver_sha256': sha256(DRIVER.encode()).hexdigest()}
    (output / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    return output


class VerificationSandbox(TaskSandbox):
    container_root = '/verifier'


class Verification:
    def __init__(self, output):
        self.output = Path(output).resolve()
        self.state = json.loads((self.output / 'verification.json').read_text())
        key = self.state['id']
        if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('Invalid verification identity')
        self.name = 'chagent-verify-' + key
        self.seed = self.name + '-seed'
        self.volumes = [self.name + '-tests', self.name + '-artifacts']
        self.sandbox = VerificationSandbox(self)

    def save(self):
        path = self.output / 'verification.tmp'
        path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + '\n')
        path.replace(self.output / 'verification.json')

    @classmethod
    async def create(cls, task_output, plan_directory, output):
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=False)
        state = {'version': 1, 'id': uuid4().hex, 'phase': 'preparing', 'cleanup': 'pending',
                 'result': 'inconclusive', 'reason': 'verification not finished'}
        (output / 'verification.json').write_text(json.dumps(state))
        verifier = cls(output)
        try:
            plan_directory = Path(plan_directory)
            plan = json.loads((plan_directory / 'plan.json').read_text())
            if (plan.get('version') != 1 or plan.get('protocol') != 'verify-function-v1'
                    or plan.get('driver_sha256') != sha256(DRIVER.encode()).hexdigest()
                    or plan.get('network') != 'none'
                    or type(plan.get('timeout')) not in (int, float)
                    or not .1 <= plan['timeout'] <= 600):
                raise ValueError('Invalid or incompatible frozen verifier plan')
            shutil.copyfile(plan_directory / 'verifier.tar', output / 'verifier.tar')
            if archive_manifest(output / 'verifier.tar') != plan['snapshot']:
                raise ValueError('Frozen verifier snapshot changed')
            task_output = Path(task_output).resolve()
            task = json.loads((task_output / 'environment.json').read_text())
            if (task.get('phase') != 'cleaned' or task.get('cleanup') != 'confirmed'
                    or task.get('phase_before_cleanup') != 'collected'):
                raise ValueError('Task must finish artifact collection and confirmed cleanup before verification')
            archive = task_output / 'artifacts.tar'
            shutil.copyfile(archive, output / 'artifacts.tar')
            artifact_manifest = archive_manifest(output / 'artifacts.tar', allowed=task['artifacts'])
            if artifact_manifest != task['artifact_manifest']:
                raise ValueError('Collected artifact archive changed')
            state = verifier.state
            state.update(plan=plan, artifact_manifest=artifact_manifest, task_id=task['id'])
            verifier.save()
            policy = resource_policy(plan.get('resources'))
            for volume in verifier.volumes:
                await TaskEnvironment.docker('volume', 'create', '--label', 'chagent.verification=' + state['id'], volume)
            await TaskEnvironment.docker('run', '-d', '--name', verifier.seed,
                '--label', 'chagent.verification=' + state['id'], '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--memory', str(policy.memory), '--cpus', str(policy.nano_cpus / 1e9), '--pids-limit', str(policy.pids),
                '--mount', f'type=volume,src={verifier.volumes[0]},dst=/verifier',
                '--mount', f'type=volume,src={verifier.volumes[1]},dst=/artifacts',
                '--entrypoint', '/bin/sleep', plan['image_id'], 'infinity')
            for filename, destination in [('verifier.tar', '/verifier'), ('artifacts.tar', '/artifacts')]:
                with (output / filename).open('rb') as stream:
                    process = await asyncio.create_subprocess_exec('docker', 'exec', '-i', verifier.seed,
                        'tar', '-xf', '-', '-C', destination, stdin=stream,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                    try:
                        log, _ = await asyncio.wait_for(process.communicate(), 60)
                    except BaseException:
                        process.kill()
                        await process.wait()
                        raise
                    if process.returncode:
                        raise RuntimeError('Verifier initialization failed: ' + log.decode(errors='replace')[-500:])
            await TaskEnvironment.docker('rm', '-f', verifier.seed)
            await TaskEnvironment.docker('run', '-d', '--name', verifier.name,
                '--label', 'chagent.verification=' + state['id'], '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--memory', str(policy.memory), '--cpus', str(policy.nano_cpus / 1e9), '--pids-limit', str(policy.pids),
                '--tmpfs', f'/tmp:rw,nosuid,size={policy.tmp_size}',
                '--mount', f'type=volume,src={verifier.volumes[0]},dst=/verifier,readonly',
                '--mount', f'type=volume,src={verifier.volumes[1]},dst=/artifacts,readonly',
                '--workdir', '/verifier', '--env', 'HOME=/tmp', '--env', 'TMPDIR=/tmp',
                '--entrypoint', '/bin/sleep', plan['image_id'], 'infinity')
            details = json.loads(await TaskEnvironment.docker('inspect', verifier.name, '--format', '{{json .}}'))
            state['environment'] = {'container_id': details['Id'], 'image_id': details['Image'],
                                    'network': 'none', 'mounts': details['Mounts'], 'resources': asdict(policy)}
            state['phase'] = 'ready'
            verifier.save()
        except BaseException as error:
            verifier.state.update(result='infra_error', reason=f'{type(error).__name__}: {error}'[:1000], phase='preparation_failed')
            verifier.save()
            await verifier.cleanup()
            if isinstance(error, asyncio.CancelledError):
                raise
        return verifier

    async def admit(self):
        details = json.loads(await TaskEnvironment.docker('inspect', self.name, '--format', '{{json .}}'))
        host = details.get('HostConfig') or {}
        policy = resource_policy(self.state['plan'].get('resources'))
        mounts = details.get('Mounts') or []
        if (details['Id'] != self.state['environment']['container_id']
                or details['Image'] != self.state['plan']['image_id']
                or (details.get('Config', {}).get('Labels') or {}).get('chagent.verification') != self.state['id']
                or not details.get('State', {}).get('Running')
                or len(mounts) != 2 or any(m.get('Type') != 'volume' for m in mounts)
                or {(m.get('Name'), m.get('Destination'), m.get('RW')) for m in mounts}
                    != {(self.volumes[0], '/verifier', False), (self.volumes[1], '/artifacts', False)}
                or host.get('NetworkMode') != 'none' or host.get('PortBindings')
                or host.get('Privileged') or not host.get('ReadonlyRootfs')
                or host.get('CapAdd') or set(host.get('CapDrop') or []) != {'ALL'}
                or host.get('Memory') != policy.memory or host.get('NanoCpus') != policy.nano_cpus
                or host.get('PidsLimit') != policy.pids
                or resource_differences(details, self.state['plan'].get('resources'))
                or host.get('PidMode') or host.get('IpcMode') != 'private'
                or any('unconfined' in option for option in host.get('SecurityOpt') or [])
                or not any(option in ('no-new-privileges', 'no-new-privileges:true')
                           for option in host.get('SecurityOpt') or [])):
            raise RuntimeError('Verifier container identity or policy changed')

    async def run(self):
        if self.state['phase'] != 'ready':
            return self.state
        self.state['phase'] = 'verifying'
        self.save()
        token = uuid4().hex
        result_path = '/tmp/result-' + token + '.json'
        try:
            await self.admit()
            with tempfile.TemporaryDirectory(prefix='chagent-verify-control-',
                    dir=os.environ.get('CHAGENT_WORKSPACE_ROOT')) as directory:
                tool = await ExecCommandTool(self.sandbox).execute(uuid4().hex,
                    {'cmd': shlex.join(['/app/.venv/bin/python', '-I', '-c', DRIVER, result_path, token]),
                     'timeout_ms': round(self.state['plan']['timeout'] * 1000), 'max_output_chars': 64000},
                    context=ToolContext(Path(directory).resolve(), run_id=uuid4()))
            self.state['execution'] = tool.details
            (self.output / 'verifier.log').write_text(tool.details['output'])
            if tool.details['timed_out'] or tool.outcome_unknown:
                raise RuntimeError('Verifier timeout or execution outcome unknown')
            code, raw = await Sandbox._docker('exec', self.name, '/app/.venv/bin/python', '-I',
                                                   '-c', READ_RESULT, result_path)
            if code or len(raw.encode()) > 32768:
                raise RuntimeError('Verifier result missing or too large')
            (self.output / 'raw-result.json').write_text(raw)
            result = json.loads(raw)
            if (result.get('version') != 1 or result.get('token') != token
                    or result.get('result') not in ('pass', 'fail', 'infra_error')
                    or type(result.get('details')) is not dict
                    or tool.details['exit_code'] != {'pass': 0, 'fail': 1, 'infra_error': 2}[result['result']]):
                raise ValueError('Verifier result/exit status binding mismatch')
            reason = None
            if result['result'] == 'infra_error':
                reason = f"{result['details'].get('error', 'verifier error')}: {result['details'].get('message', '')}"[:1000]
            self.state.update(result=result['result'], details=result['details'], reason=reason)
        except BaseException as error:
            self.state.update(result='infra_error', reason=f'{type(error).__name__}: {error}'[:1000])
            if isinstance(error, asyncio.CancelledError):
                raise
        finally:
            self.state['phase'] = 'verified'
            self.save()
            await self.cleanup()
        return self.state

    async def cleanup(self):
        self.state['cleanup'] = 'pending'
        self.save()
        try:
            for name in (self.seed, self.name):
                code, raw = await Sandbox._docker('inspect', name, '--format', '{{json .}}')
                if code:
                    if 'no such' in raw.lower():
                        continue
                    raise RuntimeError('Cannot confirm verifier container absence')
                details = json.loads(raw)
                if (details.get('Config', {}).get('Labels') or {}).get('chagent.verification') != self.state['id']:
                    raise RuntimeError('Verifier container owner mismatch')
                if name == self.name and self.state.get('environment') and details['Id'] != self.state['environment']['container_id']:
                    raise RuntimeError('Verifier container identity mismatch')
                await TaskEnvironment.docker('rm', '-f', details['Id'])
            for volume in self.volumes:
                code, raw = await Sandbox._docker('volume', 'inspect', volume)
                if code:
                    if 'no such' in raw.lower():
                        continue
                    raise RuntimeError('Cannot confirm verifier volume absence')
                details = json.loads(raw)[0]
                if (details.get('Labels') or {}).get('chagent.verification') != self.state['id']:
                    raise RuntimeError('Verifier volume owner mismatch')
                await TaskEnvironment.docker('volume', 'rm', volume)
            self.state['cleanup'] = 'confirmed'
            if self.state['phase'] != 'cleaned':
                self.state['phase_before_cleanup'] = self.state['phase']
                self.state['phase'] = 'cleaned'
            self.save()
        except BaseException as error:
            self.state.update(result='infra_error', reason=f'cleanup_pending: {error}'[:1000])
            self.save()
            self.publish()
            raise
        self.publish()

    def publish(self):
        result = self.state['result'] if self.state['cleanup'] == 'confirmed' else 'infra_error'
        grader = GraderResult('independent_verifier', result,
                             evidence=tuple(name for name in ('verification.json', 'verifier.log', 'raw-result.json')
                                            if (self.output / name).exists()),
                             notes=self.state.get('reason') or '')
        summary = {'grader': grader.to_json(), 'cleanup': self.state['cleanup'],
                   'score': 1 if result == 'pass' else 0 if result == 'fail' else None,
                   'artifacts_sha256': self.state.get('artifact_manifest', {}).get('archive_sha256'),
                   'verifier_sha256': self.state.get('plan', {}).get('snapshot', {}).get('archive_sha256')}
        (self.output / 'result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        return summary
