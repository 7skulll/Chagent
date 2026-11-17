"""One policy for creating and admitting managed workspace containers."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class ContainerPolicy:
    version: int = 1
    memory: int = 2 * 1024**3
    nano_cpus: int = 2_000_000_000
    pids: int = 256
    tmp_size: int = 512 * 1024**2

    @property
    def digest(self):
        return sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def identity(self, root: Path):
        return sha256(os.fsencode(root)).hexdigest()

    def create_args(self, root: Path, image: str):
        key = self.identity(root)
        return ['--label', 'chagent.workspace=' + key,
                '--mount', f'type=bind,src={root},dst={root}',
                '--mount', f'type=volume,src=chagent-cache-{key[:20]},dst=/cache',
                '--workdir', str(root), '--publish', '127.0.0.1::18080',
                '--memory', str(self.memory), '--cpus', str(self.nano_cpus / 1e9),
                '--pids-limit', str(self.pids), '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--read-only',
                '--tmpfs', f'/tmp:rw,nosuid,size={self.tmp_size}',
                '--env', 'HOME=/tmp', '--env', 'TMPDIR=/tmp',
                '--env', 'MAVEN_OPTS=-Dmaven.repo.local=/cache/m2',
                '--env', 'PIP_CACHE_DIR=/cache/pip', '--env', 'npm_config_cache=/cache/npm',
                '--entrypoint', '/bin/sleep', image, 'infinity']

    def differences(self, details: dict, root: Path, image: str):
        host, config = details.get('HostConfig', {}), details.get('Config', {})
        mounts = details.get('Mounts') or []
        key = self.identity(root)
        binds = [m for m in mounts if m.get('Type') == 'bind']
        volumes = [m for m in mounts if m.get('Type') == 'volume']
        ports = host.get('PortBindings') or {}
        published = ports.get('18080/tcp') or []
        tmpfs = host.get('Tmpfs') or {}
        options = set(tmpfs.get('/tmp', '').split(','))
        sizes = {f'size={self.tmp_size}', 'size=512m', 'size=524288k'}
        checks = {
            'workspace_label': (config.get('Labels') or {}).get('chagent.workspace') == key,
            'image': config.get('Image') == image,
            'workspace_mount': len(binds) == 1 and binds[0].get('Source') == str(root)
                and binds[0].get('Destination') == str(root) and binds[0].get('RW') is True,
            'cache_volume': len(volumes) == 1 and volumes[0].get('Name') == 'chagent-cache-' + key[:20]
                and volumes[0].get('Destination') == '/cache' and volumes[0].get('RW') is True,
            'mounts': all(m.get('Type') in {'bind', 'volume', 'tmpfs'} for m in mounts)
                and all(m.get('Destination') == '/tmp' for m in mounts if m.get('Type') == 'tmpfs'),
            'readonly_root': host.get('ReadonlyRootfs') is True,
            'privileged': not host.get('Privileged'),
            'capabilities': set(host.get('CapDrop') or []) == {'ALL'} and not host.get('CapAdd'),
            'security_options': any(s in {'no-new-privileges', 'no-new-privileges:true'}
                                    for s in host.get('SecurityOpt') or [])
                and not any('unconfined' in s for s in host.get('SecurityOpt') or []),
            'namespaces': host.get('NetworkMode') == 'bridge' and not host.get('PidMode')
                and host.get('IpcMode') == 'private',
            'memory': host.get('Memory') == self.memory,
            'cpu': host.get('NanoCpus') == self.nano_cpus,
            'pids': host.get('PidsLimit') == self.pids,
            'tmpfs': set(tmpfs) == {'/tmp'} and {'rw', 'nosuid'} <= options and bool(options & sizes),
            'ports': set(ports) == {'18080/tcp'} and len(published) == 1
                and published[0].get('HostIp') == '127.0.0.1',
            'entrypoint': config.get('Entrypoint') == ['/bin/sleep'] and config.get('Cmd') == ['infinity'],
            'user': config.get('User', '') in {'', 'root', '0', '0:0'},
        }
        return [name for name, ok in checks.items() if not ok]

    def manifest(self, details: dict, root: Path):
        host = details.get('HostConfig') or {}
        return {'policy_version': self.version, 'policy_sha256': self.digest,
                'container_id': details.get('Id'), 'image_id': details.get('Image'),
                'image': (details.get('Config') or {}).get('Image'),
                'created_at': details.get('Created'), 'workspace_identity': self.identity(root),
                'resources': {k: host.get(k) for k in ('Memory', 'NanoCpus', 'PidsLimit', 'Tmpfs')},
                'network_mode': host.get('NetworkMode'), 'ports': host.get('PortBindings'),
                'mounts': [{k: m.get(k) for k in ('Type', 'Name', 'Source', 'Destination', 'RW')}
                           for m in details.get('Mounts') or []],
                'user': (details.get('Config') or {}).get('User', '')}
