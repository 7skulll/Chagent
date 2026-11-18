"""Versioned internal command-only calibration. Always synthetic, never a model benchmark."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import UUID

from eval import container_trial as ct, prereg
from eval.integrity import publish
from eval.manifests import sha256_json, prompt_material
from chagent.models.fake import FakeModelClient
from chagent.storage.repositories import RunRepository
from chagent.tools.sandbox import Sandbox

DEFAULT_SUITE = Path(__file__).with_name('smallsets') / 'e4-v1'


def load_suite(directory):
    directory = Path(directory).resolve(strict=True)
    document = json.loads((directory / 'suite.json').read_text())
    if (set(document) != {'version', 'id', 'repeats', 'resources', 'tasks'}
            or document['version'] != 1 or document['repeats'] != 3
            or not re.fullmatch(r'[a-z0-9_-]+', document['id'])
            or not 1 <= len(document['tasks']) <= 8):
        raise ValueError('Unsupported fixed calibration suite')
    names, ids = set(), set()
    for task in document['tasks']:
        if (set(task) != {'name', 'id', 'title', 'prompt', 'artifacts', 'network', 'services', 'controls'}
                or not re.fullmatch(r'[a-z0-9_-]+', task['name']) or task['name'] in names
                or task['id'] in ids or set(task['controls']) != {'correct', 'incorrect'}
                or any(type(c) is not str or not c for c in task['controls'].values())):
            raise ValueError('Invalid or duplicate calibration task')
        names.add(task['name']); ids.add(task['id'])
        for part in ('input', 'verifier'):
            path = directory / task['name'] / part
            if path.is_symlink() or not path.resolve().is_relative_to(directory) or not path.is_dir():
                raise ValueError('Task fixture path is missing or escapes suite')
    files = {}
    for path in sorted(directory.rglob('*')):
        if '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        if path.is_symlink():
            raise ValueError('Calibration suite rejects symlinks')
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return document, files, sha256_json(files)


async def absence(args):
    code, raw = await Sandbox._docker(*args)
    missing = any(term in raw.lower() for term in ('no such object', 'no such container', 'no such volume', 'no such network'))
    missing = missing or (args[0] == 'network' and f'network {args[-1]} not found' in raw.lower())
    if code == 0 or not missing:
        raise ValueError('Cannot confirm calibration resource removal: ' + raw[-200:])


async def inspect_trial(record, task, frozen, *, database):
    """Bind the measured tool/context surface, artifacts and owned resource absence."""
    output = Path(record.manifest['container_task']['evidence'])
    evidence = ct.read_json(output / 'run-evidence.json')
    declarations, prompts = set(), set()
    for call in evidence['calls']:
        request = call['request']
        prompt, tools = prompt_material(request)
        users = [m for m in request.get('messages', []) if m.get('role') == 'user']
        if (not users or users[0].get('content') != task['prompt']
                or request.get('model', {}).get('context_window') != 16384
                or request.get('model', {}).get('max_tokens') != 512
                or [tool['name'] for tool in tools] != ['exec_command'] or request.get('purpose') != 'task'
                or 'Boundary: disposable task container.' not in prompt
                or f"Network mode: {task['network']}." not in prompt):
            raise ValueError('Actual tool/context surface differs from calibration profile')
        declarations.add(sha256_json(tools)); prompts.add(hashlib.sha256(prompt.encode()).hexdigest())
    if len(declarations) != 1 or len(prompts) != 1:
        raise ValueError('Tool/context identity drifted inside a trial')
    if not evidence['calls'] or record.manifest['harness']['client_kind'] != 'synthetic':
        raise ValueError('Calibration requires explicit synthetic calls')
    events = await RunRepository(database).events(UUID(record.run_id))
    before = [e['payload'] for e in events if e['type'] == 'workspace_manifest_before']
    after = [e['payload'] for e in events if e['type'] == 'workspace_manifest_after']
    if len(before) != 1 or len(after) != 1 or not after[0]['diff']['complete']:
        raise ValueError('Missing or incomplete workspace evidence')
    environment = ct.read_json(output / 'task/environment.json')
    verification = ct.read_json(output / 'verification/verification.json')
    result = ct.read_json(output / 'verification/result.json')
    if (environment.get('cleanup') != 'confirmed' or verification.get('cleanup') != 'confirmed'
            or result['grader']['result'] != record.verdict
            or result['score'] != (1 if record.verdict == 'pass' else 0)
            or environment['snapshot'] != frozen['snapshot']
            or environment['environment_spec'] != frozen['environment']):
        raise ValueError('Cleanup, score or frozen execution binding mismatch')
    names = ['chagent-task-' + environment['id']]
    names += ['chagent-task-' + environment['id'] + '-service-' + s['name'] for s in task['services']]
    names += ['chagent-verify-' + verification['id'], 'chagent-verify-' + verification['id'] + '-seed']
    for name in names:
        await absence(('inspect', name))
    volumes = ['chagent-task-' + environment['id'] + suffix for suffix in ('-workspace', '-cache')]
    volumes += ['chagent-task-' + environment['id'] + '-service-' + s['name'] + '-data' for s in task['services']]
    volumes += ['chagent-verify-' + verification['id'] + suffix for suffix in ('-tests', '-artifacts')]
    for name in volumes:
        await absence(('volume', 'inspect', name))
    if task['services']:
        await absence(('network', 'inspect', 'chagent-task-' + environment['id'] + '-network'))
        if (set(environment['service_final_states']) != {s['name'] for s in task['services']}
                or any(v.get('Health', {}).get('Status') != 'healthy' for v in environment['service_final_states'].values())):
            raise ValueError('Service dependency topology was not healthy')
    detail = {'trial_id': record.trial_id, 'case': record.case_id, 'control': record.manifest['arm'],
        'repeat': record.manifest['repeat'], 'verdict': record.verdict,
        'task_id': environment['id'], 'verifier_id': verification['id'],
        'network_id': environment.get('network_identity', {}).get('id'),
        'tools_sha256': next(iter(declarations)), 'prompt_sha256': next(iter(prompts)),
        'context_window': evidence['calls'][0]['request']['model']['context_window'],
        'max_tokens': evidence['calls'][0]['request']['model']['max_tokens'],
        'artifacts': environment['artifact_manifest']['files'], 'diff': after[0]['diff'],
        'cleanup_confirmed': True, 'resources_absent': True}
    publish(output / 'calibration-evidence.json', {'summary': detail, 'events': events})
    detail['evidence_sha256'] = hashlib.sha256((output / 'calibration-evidence.json').read_bytes()).hexdigest()
    detail['evidence'] = str(output / 'calibration-evidence.json')
    return detail


async def run_suite(directory, output, *, database, image=None):
    """Freeze once, then run each known correct/incorrect control three times."""
    suite, files, digest = load_suite(directory)
    output = Path(output).resolve()
    if output.is_relative_to(Path(directory).resolve()):
        raise ValueError('Calibration output must be outside suite sources')
    output.mkdir(parents=True, exist_ok=False)
    image = image or os.environ.get('CHAGENT_EXECUTOR_IMAGE', 'chagent:local')
    image_id = (await ct.TaskEnvironment.docker('image', 'inspect', image, '--format', '{{.Id}}')).strip()
    publish(output / 'suite-binding.json', {'id': suite['id'], 'files': files, 'sha256': digest,
                                           'image_id': image_id, 'profile': ct.PROFILE})
    cases, bindings = [], {}
    for task in suite['tasks']:
        with tempfile.TemporaryDirectory(prefix='chagent-smallset-input-', dir=os.environ.get('CHAGENT_WORKSPACE_ROOT')) as temp:
            staging = Path(temp)
            shutil.copytree(Path(directory) / task['name'] / 'input', staging / 'input')
            services = deepcopy(task['services'])
            for service in services:
                service['image'] = service['image_id'] = image_id
            spec = {'id': task['id'], 'version': 1, 'title': task['title'], 'prompt': task['prompt'],
                'workspace': str(staging / 'input'), 'verifier': str(Path(directory) / task['name'] / 'verifier'),
                'artifacts': task['artifacts'], 'verifier_image': image_id, 'verifier_timeout': 10,
                'environment': {'version': 1, 'image_id': image_id, 'network': task['network'],
                    'resources': suite['resources'], 'verifier_resources': suite['resources'], 'services': services},
                'budget': {'max_turns': 4, 'max_tool_calls': 3, 'wall_seconds': 90, 'max_cost_usd': .01}}
            case, frozen, _ = await ct.freeze_task(spec, output / 'catalog' / task['name'])
            cases.append(case); bindings[case.id] = (task, frozen)
    protocol = prereg.build(experiment='calibration-' + suite['id'], question='Known artifact controls, not model ability',
        hypothesis='Correct controls pass; incorrect controls fail; same frozen inputs reproduce both.',
        cases=cases, arms=[prereg.Arm('correct', 'deepseek', 'synthetic-correct'),
                          prereg.Arm('incorrect', 'deepseek', 'synthetic-incorrect')],
        repeats=3, min_repeats_for_stability=3, order='interleaved',
        budget={'max_turns': 4, 'max_tool_calls': 3, 'wall_seconds': 90, 'total_cost_usd': .01},
        thresholds={'improve_points': .3, 'regress_points': .3}, analysis='Instrument calibration only; no model comparison.',
        exclusions='Keep every planned slot and all infrastructure failures.',
        stopping_rule='Each fixed slot once. Stop on any unexpected outcome/identity/cleanup drift; no red reruns.')
    prereg.freeze(protocol, output / 'results' / protocol.experiment)
    env = {'CHAGENT_PROVIDER': 'deepseek', 'DEEPSEEK_API_KEY': 'synthetic-only-never-network',
           'CHAGENT_CONTEXT_WINDOW': '16384', 'CHAGENT_MAX_TOKENS': '512'}
    observations, errors = [], []
    report = {'kind': 'command-only-instrument-calibration', 'suite_sha256': digest,
        'planned': protocol.slots, 'observed': 0, 'calibration_passed': False, 'paid_calls': 0,
        'profile': ct.PROFILE, 'limitations': ['Synthetic controls are not model ability evidence.',
            'No remote file tools, persistent processes, skills, memory or compaction evaluated.']}
    try:
        for repeat in range(3):
            for case in cases:
                task, frozen = bindings[case.id]
                for arm in protocol.arms:
                    if load_suite(directory)[2] != digest:
                        raise ValueError('Versioned suite changed during calibration')
                    record = await ct.execute_slot(output / 'catalog' / task['name'], protocol, arm, repeat,
                        database=database, root=output / 'results', env=env,
                        client=FakeModelClient('exec_command', {'cmd': task['controls'][arm.name]}))
                    expected = 'pass' if arm.name == 'correct' else 'fail'
                    if record.verdict != expected or record.status != 'completed' or record.usage['cost']['usd'] != 0:
                        raise ValueError(f'{record.trial_id}: expected completed/{expected}, got {record.status}/{record.verdict}')
                    observation = await inspect_trial(record, task, frozen, database=database)
                    prior = [x for x in observations if x['case'] == case.id]
                    for previous in prior:
                        for field in ('tools_sha256', 'prompt_sha256', 'context_window', 'max_tokens'):
                            if previous[field] != observation[field]:
                                raise ValueError('Actual tool/context identity changed across repeats/controls')
                        if previous['control'] == arm.name and previous['artifacts'] != observation['artifacts']:
                            raise ValueError('Artifact content is not reproducible under the same control')
                    observations.append(observation)
        if len({o['task_id'] for o in observations}) != protocol.slots or len({o['verifier_id'] for o in observations}) != protocol.slots:
            raise ValueError('Task or verifier identity was reused')
        network_ids = [o['network_id'] for o in observations if o['network_id']]
        if len(network_ids) != len(set(network_ids)):
            raise ValueError('Service network identity was reused')
        if load_suite(directory)[2] != digest:
            raise ValueError('Versioned suite changed during calibration')
        report['calibration_passed'] = True
    except Exception as error:
        errors.append(f'{type(error).__name__}: {error}')
    raw = ct.build_report(output / 'results', protocol)
    report.update(observed=raw['recorded'], verified_observations=len(observations), missing=raw['missing'],
                  observations=observations, errors=errors, raw_trials=raw)
    publish(output / 'calibration-report.json', report)
    return report
