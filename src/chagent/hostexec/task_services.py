"""Private task service graph, with health-gated startup and owned cleanup."""
import asyncio
import json
import shlex
from time import monotonic

from chagent.hostexec.environment_spec import resource_args, resource_differences
from chagent.tools.sandbox import Sandbox


class TaskServices:
    def __init__(self, task):
        self.task = task
        self.spec = task.state.get('environment_spec', {})
        self.services = self.spec.get('services', [])
        self.network = task.name + '-network'

    def name(self, service):
        return self.task.name + '-service-' + service['name']

    def volume(self, service):
        return self.name(service) + '-data'

    @property
    def private_network(self):
        return bool(self.services) or self.spec.get('network') == 'internal'

    async def start(self):
        task = self.task
        if self.private_network:
            args = ['network', 'create', '--driver', 'bridge', '--label', 'chagent.task=' + task.state['id']]
            if self.spec['network'] == 'internal':
                args.append('--internal')
            network_id = (await task.docker(*args, self.network)).strip()
            task.state['network_identity'] = {'id': network_id, 'name': self.network,
                                               'internal': self.spec['network'] == 'internal'}
            task.state['network'] = self.network
            task.save()
        task.state['services'] = {}
        task.save()
        for service in self.services:
            name, volume = self.name(service), self.volume(service)
            await task.docker('volume', 'create', '--label', 'chagent.task=' + task.state['id'], volume)
            args = ['run', '-d', '--name', name, '--label', 'chagent.task=' + task.state['id'],
                    '--network', task.state['network'], '--network-alias', service['name'],
                    '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    *resource_args(service['resources']), '--mount', f'type=volume,src={volume},dst=/data',
                    '--health-cmd', shlex.join(service['healthcheck']), '--health-interval', '1s',
                    '--health-timeout', '2s', '--health-retries', '120', '--env', 'HOME=/tmp', '--env', 'TMPDIR=/tmp']
            for key, value in service['env'].items():
                args += ['--env', key + '=' + value]
            args += ['--entrypoint', service['command'][0], service['image_id'], *service['command'][1:]]
            await task.docker(*args)
            deadline = monotonic() + service['startup_timeout']
            while True:
                details = json.loads(await task.docker('inspect', name, '--format', '{{json .}}'))
                task.state['services'][service['name']] = {
                    'container_id': details['Id'], 'image_id': details['Image'],
                    'state': details.get('State'), 'resources': service['resources'],
                    'depends_on': service['depends_on']}
                task.save()
                if not details.get('State', {}).get('Running'):
                    raise RuntimeError('Service exited during startup: ' + service['name'])
                if details.get('State', {}).get('Health', {}).get('Status') == 'healthy':
                    break
                if monotonic() >= deadline:
                    raise RuntimeError('Service health timeout: ' + service['name'])
                await asyncio.sleep(.1)

    async def admit(self):
        task = self.task
        if self.private_network:
            details = json.loads(await task.docker('network', 'inspect', self.network))[0]
            expected = task.state['network_identity']
            if (details['Id'] != expected['id'] or details.get('Internal') != expected['internal']
                    or (details.get('Labels') or {}).get('chagent.task') != task.state['id']):
                raise RuntimeError('Task network identity or policy changed')
            allowed = {s['container_id'] for s in task.state.get('services', {}).values()}
            if task.state.get('environment'):
                allowed.add(task.state['environment']['container_id'])
            if set(details.get('Containers') or {}) - allowed:
                raise RuntimeError('Task network contains foreign endpoints')
        for service in self.services:
            details = json.loads(await task.docker('inspect', self.name(service), '--format', '{{json .}}'))
            host = details.get('HostConfig') or {}
            mounts = details.get('Mounts') or []
            if (details['Id'] != task.state['services'][service['name']]['container_id']
                    or details['Image'] != service['image_id']
                    or (details.get('Config', {}).get('Labels') or {}).get('chagent.task') != task.state['id']
                    or not details.get('State', {}).get('Running')
                    or details.get('State', {}).get('Health', {}).get('Status') != 'healthy'
                    or host.get('NetworkMode') != task.state['network']
                    or set(details.get('NetworkSettings', {}).get('Networks') or {}) != {task.state['network']}
                    or host.get('Privileged') or host.get('CapAdd') or not host.get('ReadonlyRootfs')
                    or set(host.get('CapDrop') or []) != {'ALL'} or host.get('PortBindings')
                    or resource_differences(details, service['resources'])
                    or len(mounts) != 1 or mounts[0].get('Type') != 'volume'
                    or mounts[0].get('Name') != self.volume(service) or mounts[0].get('Destination') != '/data'):
                raise RuntimeError('Service identity, health or policy changed: ' + service['name'])

    async def cleanup_containers(self):
        task = self.task
        for service in reversed(self.services):
            name = self.name(service)
            code, raw = await Sandbox._docker('inspect', name, '--format', '{{json .}}')
            if code:
                if 'no such' in raw.lower():
                    continue
                raise RuntimeError('Cannot confirm service absence')
            details = json.loads(raw)
            known = task.state.get('services', {}).get(service['name'], {})
            if ((details.get('Config', {}).get('Labels') or {}).get('chagent.task') != task.state['id']
                    or known.get('container_id', details['Id']) != details['Id']):
                raise RuntimeError('Refusing service cleanup: owner or identity mismatch')
            task.state.setdefault('service_final_states', {})[service['name']] = details.get('State')
            task.save()
            await task.docker('rm', '-f', details['Id'])

    async def cleanup_network(self):
        if not self.private_network:
            return
        task = self.task
        code, raw = await Sandbox._docker('network', 'inspect', self.network)
        if code:
            if 'no such' in raw.lower() or f'network {self.network} not found' in raw.lower():
                return
            raise RuntimeError('Cannot confirm network absence')
        details = json.loads(raw)[0]
        known = task.state.get('network_identity', {})
        if ((details.get('Labels') or {}).get('chagent.task') != task.state['id']
                or known.get('id', details['Id']) != details['Id']):
            raise RuntimeError('Refusing network cleanup: owner or identity mismatch')
        await task.docker('network', 'rm', self.network)
