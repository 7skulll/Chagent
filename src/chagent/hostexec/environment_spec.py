"""Controller-owned, bounded task topology. No arbitrary Compose/host options."""
from dataclasses import asdict
from hashlib import sha256
import json
import math
import re

from chagent.tools.container_policy import ContainerPolicy


def resources(value=None):
    value = {} if value is None else value
    if type(value) is not dict or value.keys() - {'cpus', 'memory_mb', 'pids', 'tmpfs_mb'}:
        raise ValueError('Unknown resource fields')
    limits = {'cpus': (2, .1, 16), 'memory_mb': (2048, 128, 32768),
              'pids': (256, 16, 4096), 'tmpfs_mb': (512, 16, 1024)}
    result = {}
    for name, (default, low, high) in limits.items():
        number = value.get(name, default)
        if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError('Resource limit out of range: ' + name)
        if name != 'cpus' and type(number) is not int:
            raise ValueError('Resource limit must be an integer: ' + name)
        result[name] = number
    return result


def policy(value=None):
    spec = resources(value)
    return ContainerPolicy(memory=spec['memory_mb'] * 1024**2,
                           nano_cpus=round(spec['cpus'] * 1e9), pids=spec['pids'],
                           tmp_size=spec['tmpfs_mb'] * 1024**2)


def resource_args(value=None):
    limits = policy(value)
    return ['--memory', str(limits.memory), '--cpus', str(limits.nano_cpus / 1e9),
            '--pids-limit', str(limits.pids), '--tmpfs', f'/tmp:rw,nosuid,size={limits.tmp_size}']


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def command(value, field):
    if (type(value) is not list or not value or len(value) > 128
            or any(type(v) is not str or not v or '\0' in v or len(v) > 32768 for v in value)):
        raise ValueError('Expected nonempty command argv: ' + field)
    return list(value)


def normalize(value=None):
    value = {} if value is None else value
    if type(value) is not dict or value.keys() - {'version', 'image', 'image_id', 'resources', 'verifier_resources', 'network', 'services'}:
        raise ValueError('Unknown environment specification fields')
    if type(value.get('version', 1)) is not int or value.get('version', 1) != 1:
        raise ValueError('Unsupported environment specification version')
    mode = value.get('network', 'none')
    if mode not in ('none', 'bridge', 'internal'):
        raise ValueError('Network must be none, bridge or internal')
    items = value.get('services', [])
    if type(items) is not list or len(items) > 8:
        raise ValueError('At most eight services are supported')
    services, names = [], set()
    for item in items:
        if type(item) is not dict or item.keys() - {'name', 'image', 'image_id', 'command', 'env', 'resources', 'depends_on', 'healthcheck', 'startup_timeout'}:
            raise ValueError('Unknown service fields')
        name, image = item.get('name'), item.get('image')
        if type(name) is not str or not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', name) or name in names or name == 'main':
            raise ValueError('Invalid or duplicate service name')
        if type(image) is not str or not image or len(image) > 512 or '\0' in image:
            raise ValueError('Service image is required')
        if 'image_id' in item and (type(item['image_id']) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', item['image_id'])):
            raise ValueError('Invalid pinned service image ID')
        names.add(name)
        env = item.get('env', {})
        if type(env) is not dict or len(env) > 64 or any(
                type(k) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)
                or type(v) is not str or '\0' in v or len(v) > 8192 for k, v in env.items()):
            raise ValueError('Invalid literal service environment')
        deps = item.get('depends_on', [])
        if type(deps) is not list or any(type(d) is not str for d in deps) or len(set(deps)) != len(deps):
            raise ValueError('Invalid service dependencies')
        startup = item.get('startup_timeout', 30)
        if type(startup) not in (int, float) or not math.isfinite(startup) or not 1 <= startup <= 120:
            raise ValueError('Service startup_timeout must be 1–120 seconds')
        services.append({'name': name, 'image': image, 'command': command(item.get('command'), 'service'),
                         'env': dict(env), 'resources': resources(item.get('resources')),
                         'depends_on': deps, 'healthcheck': command(item.get('healthcheck'), 'healthcheck'),
                         'startup_timeout': startup})
        if 'image_id' in item:
            services[-1]['image_id'] = item['image_id']
    if services and mode == 'none':
        raise ValueError('Services require an internal or bridge task network')
    ordered = []
    pending = list(services)
    while pending:
        ready = [s for s in pending if set(s['depends_on']) <= {s['name'] for s in ordered}]
        if not ready:
            raise ValueError('Unknown or cyclic service dependencies')
        for item in ready:
            ordered.append(item)
            pending.remove(item)
    main = resources(value.get('resources'))
    if (sum(s['resources']['cpus'] for s in ordered) + main['cpus'] > 16
            or sum(s['resources']['memory_mb'] for s in ordered) + main['memory_mb'] > 32768):
        raise ValueError('Aggregate task resource budget exceeds 16 CPU/32 GiB')
    for field in ('image', 'image_id'):
        if field in value and value[field] is not None:
            if type(value[field]) is not str or not value[field] or len(value[field]) > 512 or '\0' in value[field]:
                raise ValueError('Invalid task image reference')
    if value.get('image_id') and not re.fullmatch(r'sha256:[0-9a-f]{64}', value['image_id']):
        raise ValueError('Invalid pinned task image ID')
    return {'version': 1, 'image': value.get('image'), 'image_id': value.get('image_id'), 'resources': main, 'verifier_resources': resources(value.get('verifier_resources')),
            'network': mode, 'services': ordered}


def resource_differences(details, limits):
    host = details.get('HostConfig') or {}
    expected = asdict(policy(limits))
    tmpfs = host.get('Tmpfs') or {}
    options = set(tmpfs.get('/tmp', '').split(','))
    sizes = {f"size={expected['tmp_size']}", f"size={expected['tmp_size'] // 1024**2}m",
             f"size={expected['tmp_size'] // 1024}k"}
    return [field for field, ok in {
        'cpu': host.get('NanoCpus') == expected['nano_cpus'],
        'memory': host.get('Memory') == expected['memory'],
        'pids': host.get('PidsLimit') == expected['pids'],
        'tmpfs': set(tmpfs) == {'/tmp'} and {'rw', 'nosuid'} <= options and bool(options & sizes),
    }.items() if not ok]
