"""Disposable task containers with controller-owned snapshots and artifact export.

Task workspaces and caches are Docker volumes, never writable host binds.
The operator's output directory is the durable recovery record.
"""
import asyncio
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
from uuid import uuid4

from chagent.hostexec import command_worker
from chagent.boundaries import validate_file_identity
from chagent.tools.command import ExecCommandTool
from chagent.tools.container_policy import ContainerPolicy
from chagent.tools.context import ToolContext
from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.tools.workspace import WorkspaceFS

MAX_BYTES = 100 * 1024**2
MAX_FILES = 10000


def relative_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in {'', '.', '..'} for p in value.split('/')):
        raise ValueError('Expected a canonical relative path')
    return str(path)


def archive_manifest(path, *, limit=MAX_BYTES, allowed=None):
    """Validate before extraction; links/devices/traversal/duplicates are rejected."""
    files, names, total = [], set(), 0
    with tarfile.open(path, 'r:') as archive:
        for member in archive:
            name = relative_path(member.name.rstrip('/'))
            if name in names or len(names) >= MAX_FILES:
                raise ValueError('Duplicate archive path or too many entries')
            names.add(name)
            if allowed is not None and not any(name == a or name.startswith(a + '/') for a in allowed):
                raise ValueError('Undeclared artifact in archive')
            if member.mode & ~0o777:
                raise ValueError('Special file permission bits are not accepted')
            if not member.isfile() and not member.isdir():
                raise ValueError('Only regular files and directories may be transferred')
            total += member.size
            if member.size < 0 or total > limit:
                raise ValueError('Archive exceeds transfer budget')
            if member.isfile():
                content = archive.extractfile(member).read()
                files.append({'path': name, 'bytes': len(content), 'sha256': sha256(content).hexdigest()})
    return {'archive_sha256': sha256(path.read_bytes()).hexdigest(), 'files': files, 'bytes': total,
            'entries': sorted(names)}


def snapshot_workspace(workspace, destination):
    fs = WorkspaceFS(Path(workspace))
    total, count = 0, 0
    with tarfile.open(destination, 'w') as archive:
        def walk(parts):
            nonlocal total, count
            with fs.directory(parts) as descriptor:
                for name in sorted(os.listdir(descriptor)):
                    path = '/'.join((*parts, name))
                    count += 1
                    if count > MAX_FILES:
                        raise ValueError('Snapshot contains too many entries')
                    import stat
                    before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    item = tarfile.TarInfo(path)
                    item.mode = before.st_mode & 0o777
                    item.mtime = 0
                    if stat.S_ISDIR(before.st_mode):
                        item.type = tarfile.DIRTYPE
                        archive.addfile(item)
                        walk((*parts, name))
                    elif stat.S_ISREG(before.st_mode):
                        validate_file_identity(before)
                        total += before.st_size
                        if total > MAX_BYTES:
                            raise ValueError('Snapshot exceeds 100 MiB')
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                        with os.fdopen(fd, 'rb') as stream:
                            opened = os.fstat(stream.fileno())
                            validate_file_identity(opened)
                            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                                before.st_ctime_ns) != (opened.st_dev, opened.st_ino, opened.st_size,
                                                       opened.st_mtime_ns, opened.st_ctime_ns):
                                raise ValueError('Snapshot input changed while opening')
                            item.size = before.st_size
                            archive.addfile(item, stream)
                            after = os.fstat(stream.fileno())
                            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                                raise ValueError('Snapshot input changed while reading')
                    else:
                        raise ValueError('Snapshot rejects symlinks and special files: ' + path)
        walk(())
    return archive_manifest(destination)


# Runs with a directory descriptor rooted in the task volume. It never follows
# a candidate-created link, including links in intermediate directories.
EXPORT = r'''
import io, os, stat, sys, tarfile, tempfile
from pathlib import Path
paths, limit = __import__('json').loads(sys.argv[1]), int(sys.argv[2])
total = count = 0
with tempfile.TemporaryFile() as target:
    with tarfile.open(fileobj=target, mode='w') as archive:
        def add(parent, leaf, name):
            global total, count
            before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            count += 1
            if count > 10000: raise ValueError('too many artifacts')
            item = tarfile.TarInfo(name); item.mode = before.st_mode & 0o777
            if stat.S_ISDIR(before.st_mode):
                child = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    item.type = tarfile.DIRTYPE; archive.addfile(item)
                    for entry in sorted(os.listdir(child)): add(child, entry, name + '/' + entry)
                finally: os.close(child)
            elif stat.S_ISREG(before.st_mode):
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, 'rb') as stream:
                    actual = os.fstat(stream.fileno())
                    if not stat.S_ISREG(actual.st_mode): raise ValueError('not a regular file')
                    total += actual.st_size
                    if total > limit: raise ValueError('artifact size limit')
                    item.size = actual.st_size; archive.addfile(item, stream)
                    after = os.fstat(stream.fileno())
                    if (actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise ValueError('artifact changed during collection')
            else: raise ValueError('symlink or special artifact rejected')
        for name in paths:
            fd = os.open('/workspace', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parts = name.split('/')
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd); fd = child
                add(fd, parts[-1], name)
            finally: os.close(fd)
    target.seek(0)
    while chunk := target.read(65536): sys.stdout.buffer.write(chunk)
'''


class TaskSandbox(Sandbox):
    """Command-only adapter; file tools must never read the source workspace."""
    def __init__(self, task):
        super().__init__(SandboxSettings(containerized=True, host_controller=True))
        self.task = task

    async def wrap(self, argv, workspace, *, owner=None, workdir=None, timeout_seconds=None):
        call = uuid4().hex
        control = f'/tmp/chagent-run-{owner}/calls/{call}'
        relative = (workdir or workspace).relative_to(workspace)
        directory = str(PurePosixPath('/workspace') / relative.as_posix())
        record = {'container': self.task.name, 'invocation': call, 'control_path': control,
                  'environment': self.task.state['environment']}
        self.task.state['invocation'] = record
        self.task.save()
        return (['docker', 'exec', '--workdir', directory, '--env', f'HOME=/tmp/chagent-run-{owner}',
                 '--env', f'TMPDIR=/tmp/chagent-run-{owner}', self.task.name,
                 '/app/.venv/bin/python', '-I', '-c', Path(command_worker.__file__).read_text(),
                 'run', control, str(timeout_seconds), *argv], record)

    async def finish_invocation(self, record):
        try:
            await super().finish_invocation(record)
        finally:
            self.task.state['invocation'] = record
            self.task.save()


class TaskEnvironment:
    def __init__(self, output):
        self.output = Path(output).resolve()
        self.state = json.loads((self.output / 'environment.json').read_text())
        key = self.state['id']
        if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('Invalid task identity')
        self.name = 'chagent-task-' + key
        self.volumes = [self.name + '-workspace', self.name + '-cache']
        self.sandbox = TaskSandbox(self)

    def save(self):
        temporary = self.output / 'environment.tmp'
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(self.output / 'environment.json')

    @staticmethod
    async def docker(*args):
        code, output = await Sandbox._docker(*args, timeout=60)
        if code:
            raise RuntimeError('Docker operation failed: ' + output[-500:])
        return output

    @classmethod
    async def create(cls, output, *, workspace=None, snapshot=None, artifacts=(), network=False):
        output = Path(output).resolve()
        artifacts = [relative_path(a) for a in artifacts]
        if len(set(artifacts)) != len(artifacts) or any(
                a != b and b.startswith(a + '/') for a in artifacts for b in artifacts):
            raise ValueError('Artifact declarations overlap')
        if workspace and (output == Path(workspace).resolve() or output.is_relative_to(Path(workspace).resolve())):
            raise ValueError('Output must be outside the source workspace')
        if bool(workspace) == bool(snapshot):
            raise ValueError('Supply exactly one workspace or frozen snapshot')
        output.mkdir(parents=True, exist_ok=False)
        target = output / 'snapshot.tar'
        state = {'version': 1, 'id': uuid4().hex, 'phase': 'preparing', 'snapshot': None,
                 'artifacts': artifacts, 'network': 'bridge' if network else 'none',
                 'cache': 'private-per-task', 'commands': []}
        (output / 'environment.json').write_text(json.dumps(state))
        task = cls(output)
        key = task.state['id']
        try:
            if workspace:
                frozen = snapshot_workspace(workspace, target)
            else:
                if Path(snapshot).stat().st_size > MAX_BYTES + MAX_FILES * 2048:
                    raise ValueError('Snapshot archive too large')
                shutil.copyfile(snapshot, target)
                frozen = archive_manifest(target)
            task.state['snapshot'] = frozen
            policy = ContainerPolicy()
            image = os.environ.get('CHAGENT_EXECUTOR_IMAGE', 'chagent:local')
            image_id = (await task.docker('image', 'inspect', image, '--format', '{{.Id}}')).strip()
            state = task.state
            state['image_id'] = image_id
            state['policy'] = asdict(policy)
            task.save()
            for volume in task.volumes:
                await task.docker('volume', 'create', '--label', 'chagent.task=' + key, volume)
            await task.docker('run', '-d', '--name', task.name, '--label', 'chagent.task=' + key,
                '--mount', f'type=volume,src={task.volumes[0]},dst=/workspace',
                '--mount', f'type=volume,src={task.volumes[1]},dst=/cache',
                '--workdir', '/workspace', '--network', state['network'],
                '--memory', str(policy.memory), '--cpus', str(policy.nano_cpus / 1e9),
                '--pids-limit', str(policy.pids), '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--read-only', '--tmpfs', f'/tmp:rw,nosuid,size={policy.tmp_size}',
                '--env', 'HOME=/tmp', '--env', 'TMPDIR=/tmp',
                '--env', 'PIP_CACHE_DIR=/cache/pip', '--env', 'npm_config_cache=/cache/npm',
                '--env', 'MAVEN_OPTS=-Dmaven.repo.local=/cache/m2',
                '--entrypoint', '/bin/sleep', image_id, 'infinity')
            raw = json.loads(await task.docker('inspect', task.name, '--format', '{{json .}}'))
            state['environment'] = {'container_id': raw['Id'], 'image_id': raw['Image'],
                                    'created_at': raw['Created'], 'network': state['network'],
                                    'mounts': raw['Mounts'], 'resources': state['policy']}
            # Stream into the writable volume: docker cp may reject a container
            # with a read-only rootfs even when the destination is tmpfs.
            with target.open('rb') as stream:
                seed = await asyncio.create_subprocess_exec('docker', 'exec', '-i', task.name,
                    'tar', '-xf', '-', '-C', '/workspace', stdin=stream,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                try:
                    response, _ = await asyncio.wait_for(seed.communicate(), 60)
                except BaseException:
                    seed.kill()
                    await seed.wait()
                    raise
                if seed.returncode:
                    raise RuntimeError('Snapshot initialization failed: ' + response.decode(errors='replace')[-500:])
            state['phase'] = 'ready'
            task.save()
            return task
        except BaseException as error:
            if (output / 'environment.json').exists():
                task = cls(output)
                task.state['error'] = f'{type(error).__name__}: {error}'[:1000]
                task.state['phase'] = 'preparation_failed'
                task.save()
                await task.cleanup()
            raise

    async def admit(self):
        details = json.loads(await self.docker('inspect', self.name, '--format', '{{json .}}'))
        environment = self.state['environment']
        host = details.get('HostConfig') or {}
        mounts = details.get('Mounts') or []
        policy = ContainerPolicy()
        if (details['Id'] != environment['container_id'] or details['Image'] != environment['image_id']
                or (details.get('Config', {}).get('Labels') or {}).get('chagent.task') != self.state['id']
                or not details.get('State', {}).get('Running')
                or len(mounts) != 2 or any(m.get('Type') != 'volume' for m in mounts)
                or {(m.get('Name'), m.get('Destination'), m.get('RW')) for m in mounts}
                    != {(self.volumes[0], '/workspace', True), (self.volumes[1], '/cache', True)}
                or not host.get('ReadonlyRootfs') or host.get('Privileged') or host.get('CapAdd')
                or set(host.get('CapDrop') or []) != {'ALL'}
                or host.get('PidMode') or host.get('IpcMode') != 'private'
                or host.get('NetworkMode') != self.state['network']
                or host.get('Memory') != policy.memory or host.get('NanoCpus') != policy.nano_cpus
                or host.get('PidsLimit') != policy.pids
                or host.get('PortBindings')
                or set(host.get('Tmpfs') or {}) != {'/tmp'}
                or not {'rw', 'nosuid'} <= set((host.get('Tmpfs') or {}).get('/tmp', '').split(','))
                or not any(s in {'no-new-privileges', 'no-new-privileges:true'}
                           for s in host.get('SecurityOpt') or [])):
            raise RuntimeError('Task container identity or execution policy changed')
        return details

    async def run(self, command, *, timeout=120):
        if self.state['phase'] != 'ready':
            raise RuntimeError('Task environment is not ready')
        if not math.isfinite(timeout) or not .1 <= timeout <= 600:
            raise ValueError('Command timeout must be between .1 and 600 seconds')
        # ExecCommandTool currently accepts up to 30s through its registered API;
        # this controller call also supports longer task commands.
        await self.admit()
        self.state['phase'] = 'executing'
        self.save()
        try:
            # The output may live in the protected agent checkout (eval/results).
            # This empty local path is only command-cwd metadata; it is never
            # mounted into Docker and must not be confused with user input.
            with tempfile.TemporaryDirectory(prefix='chagent-task-control-',
                    dir=os.environ.get('CHAGENT_WORKSPACE_ROOT')) as directory:
                control = Path(directory).resolve()
                result = await ExecCommandTool(self.sandbox).execute(uuid4().hex,
                    {'cmd': command, 'timeout_ms': round(timeout * 1000), 'max_output_chars': 64000},
                    context=ToolContext(control, run_id=uuid4()))
            self.state['commands'].append({'command': command, **result.details})
            self.state['phase'] = 'ready'
            self.save()
            return result
        except BaseException as error:
            self.state['error'] = f'{type(error).__name__}: {error}'[:1000]
            self.state['phase'] = 'execution_interrupted'
            self.save()
            raise

    async def collect(self, *, limit=20 * 1024**2):
        if self.state['phase'] != 'ready':
            raise RuntimeError('Artifacts require a completed invocation')
        if type(limit) is not int or not 0 < limit <= MAX_BYTES:
            raise ValueError('Invalid artifact transfer limit')
        await self.admit()
        self.state['phase'] = 'collecting'
        self.save()
        process = await asyncio.create_subprocess_exec('docker', 'exec', self.name,
            '/app/.venv/bin/python', '-I', '-c', EXPORT, json.dumps(self.state['artifacts']), str(limit),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async def read():
            data = bytearray()
            while chunk := await process.stdout.read(65536):
                data.extend(chunk)
                if len(data) > limit + MAX_FILES * 2048:
                    raise ValueError('Artifact archive exceeds transfer budget')
            return data
        reader = asyncio.create_task(read())
        async def read_errors():
            data = bytearray()
            while chunk := await process.stderr.read(8192):
                data.extend(chunk[:max(0, 64000 - len(data))])
            return bytes(data)
        errors = asyncio.create_task(read_errors())
        try:
            data = await asyncio.wait_for(asyncio.shield(reader), 60)
            await asyncio.wait_for(process.wait(), 5)
            error = await errors
            if process.returncode:
                raise ValueError('Artifact collection failed: ' + error.decode(errors='replace')[-500:])
            archive_path = self.output / 'artifacts.tar'
            archive_path.write_bytes(data)
            manifest = archive_manifest(archive_path, limit=limit, allowed=self.state['artifacts'])
            with tempfile.TemporaryDirectory(dir=self.output) as temporary:
                with tarfile.open(archive_path, 'r:') as archive:
                    archive.extractall(temporary, filter='data')
                destination = self.output / 'artifacts'
                if destination.exists():
                    raise ValueError('Artifacts already collected')
                Path(temporary).rename(destination)
                # TemporaryDirectory tolerates the renamed source being absent.
            self.state.update(phase='collected', artifact_manifest=manifest)
            self.save()
            return manifest
        except BaseException as error:
            self.state['error'] = f'{type(error).__name__}: {error}'[:1000]
            self.state['phase'] = 'collection_failed'
            self.save()
            raise
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            reader.cancel()
            errors.cancel()
            await asyncio.gather(reader, errors, return_exceptions=True)

    async def cleanup(self):
        """Only delete resources with this task's exact identity and label."""
        self.state['cleanup'] = 'pending'
        self.save()
        try:
            code, raw = await Sandbox._docker('inspect', self.name, '--format', '{{json .}}')
            if code == 0:
                details = json.loads(raw)
                if ((details.get('Config', {}).get('Labels') or {}).get('chagent.task') != self.state['id']
                        or (self.state.get('environment') and details['Id'] != self.state['environment']['container_id'])):
                    raise RuntimeError('Refusing to delete container with mismatched owner')
                await self.docker('rm', '-f', details['Id'])
            elif 'no such' not in raw.lower():
                raise RuntimeError('Cannot confirm container absence: ' + raw[-300:])
            for volume in self.volumes:
                code, raw = await Sandbox._docker('volume', 'inspect', volume)
                if code:
                    if 'no such' in raw.lower():
                        continue
                    raise RuntimeError('Cannot confirm volume absence')
                details = json.loads(raw)[0]
                if (details.get('Labels') or {}).get('chagent.task') != self.state['id']:
                    raise RuntimeError('Refusing to delete volume with mismatched owner')
                await self.docker('volume', 'rm', volume)
            self.state['cleanup'] = 'confirmed'
            if self.state['phase'] != 'cleaned':
                self.state['phase_before_cleanup'] = self.state['phase']
            self.state['phase'] = 'cleaned'
            self.save()
        except BaseException:
            self.state['cleanup'] = 'pending'
            self.save()
            raise
