"""Frozen internal artifact tasks: one claimed slot, real runner, offline verifier.

Uses existing Protocol/BudgetClient/TrialRecord contracts. No product tools or
platform routing are enabled. A claim survives process death and is never replayed.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from eval import casefile, prereg
from eval.budgets import BudgetClient, CostAccount, known_cost
from eval.integrity import execution, experiment_lock, identity, publish, valid_identity
from eval.manifests import VersionManifest, sha256_json
from eval.records import GraderResult, TrialStore, new_trial
from eval.runner import ROOT, TrialPlan, collect
from eval.verification import DRIVER, Verification, freeze_verifier
from chagent.bootstrap import create_task_runner
from chagent.config import Settings
from chagent.hostexec.environment_spec import normalize
from chagent.hostexec.task_environment import TaskEnvironment, archive_manifest, relative_path, snapshot_workspace
from chagent.models.factory import create_model_client
from chagent.models.fake import FakeModelClient
from chagent.models.settings import ModelSettings
from chagent.storage.repositories import ConversationRepository

PROFILE = 'task-command-only-v1'


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Frozen input must be a regular file: ' + str(path))
    return json.loads(path.read_text())


async def freeze_task(spec, output):
    """Freeze input bytes, trusted verifier, resources and exact image IDs."""
    required = {'id', 'version', 'title', 'prompt', 'workspace', 'artifacts', 'verifier', 'budget'}
    optional = {'environment', 'verifier_image', 'verifier_timeout'}
    if type(spec) is not dict or not required <= spec.keys() or spec.keys() - required - optional:
        raise ValueError('Invalid container task specification fields')
    artifacts = spec['artifacts']
    if type(artifacts) is not list or not artifacts or any(type(a) is not str for a in artifacts):
        raise ValueError('Declare at least one artifact')
    artifacts = [relative_path(a) for a in artifacts]
    if len(set(artifacts)) != len(artifacts) or any(a != b and b.startswith(a + '/') for a in artifacts for b in artifacts):
        raise ValueError('Artifact declarations overlap')
    # Validate task metadata/budget before creating or resolving any resources.
    case = {'id': spec['id'], 'version': spec['version'], 'title': spec['title'],
            'prompt': spec['prompt'], 'capability': 'project-work',
            'authority': {'boundary': 'container', 'execution': 'full', 'gate': 'none'},
            'budget': spec['budget'], 'graders': [{'id': 'independent_verifier', 'kind': 'deterministic',
                'expects': 'Trusted frozen verifier accepts the declared artifact archive',
                'spec': {'check': 'independent_verifier'}}],
            'cleanup': 'Controller collects artifacts, cleans task topology, then verifies and cleans verifier.'}
    casefile.validate(case)
    source = Path(spec['workspace']).resolve(strict=True)
    output = Path(output).resolve()
    verifier_source = Path(spec['verifier']).resolve(strict=True)
    if verifier_source.is_relative_to(source) or source.is_relative_to(verifier_source):
        raise ValueError('Trusted verifier and task input trees must be disjoint')
    if output == source or output.is_relative_to(source):
        raise ValueError('Freeze output must be outside task input')
    output.mkdir(parents=True, exist_ok=False)
    environment = normalize(spec.get('environment'))
    image = environment.get('image_id') or environment.get('image') or os.environ.get('CHAGENT_EXECUTOR_IMAGE', 'chagent:local')
    environment['image'] = environment.get('image') or image
    environment['image_id'] = (await TaskEnvironment.docker('image', 'inspect', image, '--format', '{{.Id}}')).strip()
    for service in environment['services']:
        service['image_id'] = (await TaskEnvironment.docker('image', 'inspect', service.get('image_id') or service['image'], '--format', '{{.Id}}')).strip()
    manifest = snapshot_workspace(source, output / 'snapshot.tar')
    await freeze_verifier(spec['verifier'], output / 'verifier',
        image=spec.get('verifier_image') or environment['image_id'], timeout=spec.get('verifier_timeout', 120),
        resources=environment['verifier_resources'])
    material = {'version': 1, 'profile': PROFILE, 'snapshot': manifest,
                'environment': environment, 'artifacts': artifacts,
                'verifier': read_json(output / 'verifier/plan.json')}
    case['notes'] = 'container-task-v1:' + sha256_json(material)
    publish(output / 'case.json', case)
    document = {**material, 'case_sha256': sha256_json(case)}
    publish(output / 'task.json', {**document, 'sha256': sha256_json(document)})
    return load_task(output)


def load_task(directory):
    directory = Path(directory).resolve(strict=True)
    document = read_json(directory / 'task.json')
    digest = document.pop('sha256', None)
    if digest != sha256_json(document) or document.get('profile') != PROFILE or document.get('version') != 1:
        raise ValueError('Frozen task descriptor changed')
    case = casefile.load(directory / 'case.json')
    if case.sha256 != document['case_sha256']:
        raise ValueError('Frozen task case changed')
    material = {k: v for k, v in document.items() if k != 'case_sha256'}
    if case.notes != 'container-task-v1:' + sha256_json(material):
        raise ValueError('Frozen task inputs do not match case binding')
    if archive_manifest(directory / 'snapshot.tar') != document['snapshot']:
        raise ValueError('Frozen task snapshot changed')
    plan = read_json(directory / 'verifier/plan.json')
    if (plan != document['verifier'] or archive_manifest(directory / 'verifier/verifier.tar') != plan['snapshot']
            or plan['driver_sha256'] != hashlib.sha256(DRIVER.encode()).hexdigest()):
        raise ValueError('Frozen trusted verifier changed')
    return case, document, digest


def resolved(env, arm):
    return replace(ModelSettings.from_env({**env, 'CHAGENT_PROVIDER': arm.provider,
        'CHAGENT_MODEL': arm.model, 'CHAGENT_THINKING': 'off'}), retry_attempts=0)


def pin(model_settings, env):
    manifest = VersionManifest.capture(ROOT)
    settings = Settings.from_env(env)
    value = identity(manifest.to_json(), settings, model_settings)
    if not valid_identity(value):
        raise ValueError('Execution identity cannot be pinned')
    return value


def records(store, protocol):
    return store.read(protocol.experiment, ignore=(prereg.PROTOCOL_FILE,))


async def execute_slot(task_directory, protocol, arm, repeat, *, database, root, env, client=None, expected_identity=None):
    """Claim exactly one preregistered slot. The lock and claim precede paid calls."""
    case, frozen, task_hash = load_task(task_directory)
    plan = TrialPlan(protocol, case, arm, repeat)
    model_settings = resolved(env, arm)
    synthetic = isinstance(client, FakeModelClient)
    if not synthetic and not protocol.pricing:
        raise ValueError('Live container trials require frozen pricing')
    store = TrialStore(root)
    directory = store.directory(protocol.experiment)
    trial_directory = directory / 'container-trials' / plan.trial_id
    if trial_directory.resolve().is_relative_to(Path(task_directory).resolve()):
        raise ValueError('Trial output must be outside frozen task inputs')
    with experiment_lock(directory):
        prereg.freeze(protocol, directory)
        # Read back through the established schema and integrity checks.
        protocol = prereg.load(directory)
        existing = records(store, protocol)
        ids = {trial.trial_id for trial in existing}
        pending = [p for p in (directory / '.claims').glob('*.claim') if p.stem not in ids]
        if pending:
            raise ValueError('Unfinished slot has unknown spend; no rerun or further calls allowed')
        if plan.trial_id in ids:
            raise ValueError('Slot already consumed; no rerun')
        for trial in existing:
            if (trial.manifest.get('protocol_sha256') != protocol.sha256
                    or (trial.case_id == case.id and trial.case_version == case.version
                        and trial.manifest.get('container_task', {}).get('task_sha256') != task_hash)
                    or trial.manifest.get('container_task', {}).get('profile') != PROFILE
                    or trial.manifest.get('harness', {}).get('client_kind') != ('synthetic' if synthetic else 'live')):
                raise ValueError('Prior trial input or client binding mismatch')
        from eval.report import _invalid_trials
        errors = _invalid_trials(protocol, existing)
        if synthetic:
            errors = [e for e in errors if e['reason'] != 'trial is not identified as real model evidence']
        errors += [e for trial in existing for e in evidence_errors(trial)]
        if errors:
            raise ValueError('Prior trials have invalid bindings; no further calls allowed')
        costs = [(trial.usage.get('cost') or {}).get('usd') for trial in existing]
        if any(not known_cost(cost) for cost in costs):
            raise ValueError('Prior trial spend unknown; experiment stopped')
        account = CostAccount(protocol.budget['total_cost_usd'], sum(costs))
        current = pin(model_settings, env)
        if expected_identity is not None and sha256_json(current) != expected_identity:
            raise ValueError('Execution identity differs from frozen platform plan; no call was made')
        publish(directory / 'execution.anchor', {'protocol_sha256': protocol.sha256, **execution(current)}, identical_ok=True)
        publish(directory / '.claims' / (plan.trial_id + '.claim'), {
            'protocol_sha256': protocol.sha256, 'case_sha256': case.sha256,
            'task_sha256': task_hash, 'arm': arm.name, 'repeat': repeat,
            'trial_directory': str(trial_directory.resolve())})
        trial_directory.mkdir(parents=True, exist_ok=False)
        record = await run_container_trial(plan, task_directory, frozen, task_hash,
            database=database, output=trial_directory, env=env, model_settings=model_settings,
            client=client, account=account, before=current)
        store.write(record)
        publish(trial_directory / 'trial.json', record.to_json())
        return record


async def run_container_trial(plan, task_directory, frozen, task_hash, *, database, output,
                              env, model_settings, client, account, before):
    """One input → model → collect → task cleanup → C verification → record."""
    owned = client is None
    client = create_model_client(model_settings) if owned else client
    from eval.prompts import preamble, binding
    expected_preamble = preamble(plan.arm)
    guard = BudgetClient(client, plan.protocol, plan.case, synthetic=isinstance(client, FakeModelClient),
                         account=account, expected_preamble=expected_preamble)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    wall = min(plan.protocol.budget['wall_seconds'], plan.case.budget['wall_seconds'])
    task = verifier = None
    run_id = None
    error = None
    status = 'infra_error'
    end_reason = None  # Candidate run's conclusion; never replaced by measurement failures.
    stage = 'preparation'
    timeout_stage = timeout_scope = budget_violation = None
    deadline = asyncio.timeout(wall)
    grader = GraderResult('independent_verifier', 'inconclusive', notes='No completed verification')
    rows = {'tools': [], 'calls': [], 'approvals': []}
    runner = None
    control = None
    def remember(value):
        nonlocal run_id
        run_id = value
    try:
        async with deadline:
            # A private copy is the only verifier/input source after this point.
            shutil.copytree(task_directory, output / 'inputs')
            _, inputs, copied_hash = load_task(output / 'inputs')
            if copied_hash != task_hash:
                raise ValueError('Frozen inputs changed while copying')
            task = await TaskEnvironment.create(output / 'task', snapshot=output / 'inputs/snapshot.tar',
                artifacts=inputs['artifacts'], spec=inputs['environment'])
            control = tempfile.TemporaryDirectory(prefix='chagent-container-trial-', dir=os.environ.get('CHAGENT_WORKSPACE_ROOT'))
            control_path = Path(control.name).resolve()
            session = await ConversationRepository(database).create_session(control_path)
            runner = create_task_runner(database, model_settings.model(), guard, task=task,
                session_id=session, control_workspace=control_path, max_turns=guard.max_turns,
                timeout=min(model_settings.run_timeout, wall), wall_timeout=wall)
            if expected_preamble is not None:
                runner.context.instructions = expected_preamble
            stage = 'candidate'
            result = await runner.run(session, plan.case.prompt, on_started=remember)
            end_reason = result.end_reason
            if result.end_reason != 'model_stop' or guard.violation or guard.unknown_cost:
                raise RuntimeError('Agent stopped without a complete, budgeted answer')
            stage = 'collection'
            if task.state.get('run_cleanup') != 'confirmed':
                raise RuntimeError('Agent invocation cleanup is not confirmed')
            if time.monotonic() - started > wall:
                raise TimeoutError('Container trial wall budget exhausted')
            # Verify frozen inputs again before exporting or grading any output.
            if load_task(task_directory)[2] != task_hash or load_task(output / 'inputs')[2] != task_hash:
                raise ValueError('Frozen inputs changed during trial')
            await task.collect()
            stage = 'task_cleanup'
            await task.cleanup()
            stage = 'verification_preparation'
            verifier = await Verification.create(task.output, output / 'inputs/verifier', output / 'verification')
            stage = 'verification'
            await verifier.run()
            grader = GraderResult(**verifier.publish()['grader'])
            status = 'completed' if grader.result in ('pass', 'fail') else 'infra_error'
            if status == 'completed':
                stage = 'complete'
    except asyncio.CancelledError:
        status, error = 'cancelled', 'Trial cancelled; no verification authorized'
    except Exception as failure:
        error = f'{type(failure).__name__}: {failure}'[:1000]
        if isinstance(failure, TimeoutError):
            # AgentRunner commits its conclusion before its finally/scratch release.
            # An expired caller deadline during that release is not a failed candidate.
            if stage == 'candidate' and run_id is not None:
                try:
                    async with asyncio.timeout(5):
                        async with database.connection() as connection:
                            cursor = await connection.execute(
                                'SELECT status,end_reason FROM runs WHERE id=%s', (run_id,))
                            terminal = await cursor.fetchone()
                    if terminal is not None and terminal['status'] == 'completed':
                        end_reason = terminal['end_reason']
                        stage = 'candidate_cleanup'
                except Exception:
                    stage = 'candidate_outcome_unknown'
            timeout_stage = stage
            timeout_scope = 'trial_wall' if deadline.expired() else 'operation'
            error = f'{timeout_scope} timeout during {stage}'
        if stage == 'candidate':
            end_reason = ('run_timeout' if isinstance(failure, TimeoutError)
                          else getattr(failure, 'end_reason', end_reason))
            budget_violation = guard.violation or (end_reason if end_reason in {
                'run_timeout', 'turn_limit', 'context_budget_exhausted', 'output_limit'} else None)
        # Setup, export and verification are the instrument, not the candidate.
        status = 'failed' if budget_violation else 'infra_error'
        grader = GraderResult('independent_verifier', 'infra_error' if status == 'infra_error' else 'inconclusive', notes=error)
    finally:
        # Also recover resources when create() failed after persisting its recovery file.
        if task is None and (output / 'task/environment.json').exists():
            task = TaskEnvironment(output / 'task')
        if verifier is None and (output / 'verification/verification.json').exists():
            verifier = Verification(output / 'verification')
        for resource in (task, verifier):
            if resource is not None:
                try:
                    await resource.cleanup()
                except Exception as failure:
                    status, error = 'infra_error', 'Topology cleanup unknown: ' + str(failure)[:500]
        if control is not None:
            control.cleanup()
        if run_id is not None:
            try:
                rows = await collect(database, run_id)
                publish(output / 'run-evidence.json', {'run_id': str(run_id), **rows})
            except Exception as failure:
                status, error = 'infra_error', 'Run evidence unavailable: ' + str(failure)[:500]
        if owned:
            await client.aclose()
    cleanup = (task is not None and task.state.get('cleanup') == 'confirmed'
               and (verifier is None or verifier.state.get('cleanup') == 'confirmed'))
    # Frozen bytes and source execution identity must survive the entire chain.
    after = pin(model_settings, env)
    try:
        if load_task(task_directory)[2] != task_hash or load_task(output / 'inputs')[2] != task_hash:
            raise ValueError('Frozen task input drift')
    except Exception as failure:
        status, error = 'infra_error', str(failure)[:1000]
    if before != after:
        status, error = 'infra_error', 'Execution identity changed during trial'
    if status != 'completed':
        grader = GraderResult('independent_verifier', 'infra_error' if status == 'infra_error' else 'inconclusive', notes=error or '')
    manifest = VersionManifest.capture(ROOT, image_digest=frozen['environment']['image_id'], model={
        'provider': plan.arm.provider, 'requested_id': plan.arm.model, 'api': model_settings.api,
        'endpoint_sha256': hashlib.sha256(model_settings.base_url.encode()).hexdigest()})
    if rows['calls']:
        manifest = manifest.with_request(rows['calls'][0]['request'])
    manifest = manifest.to_json()
    manifest.update(arm=plan.arm.name, repeat=plan.repeat, protocol_sha256=plan.protocol.sha256,
        case={'id': plan.case.id, 'version': plan.case.version, 'sha256': plan.case.sha256},
        execution={**execution(before), 'end_identity_sha256': sha256_json(after)},
        container_task={'profile': PROFILE, 'task_sha256': task_hash,
            'task_id': task.state['id'] if task else None, 'evidence': str(output),
            'snapshot_sha256': frozen['snapshot']['archive_sha256'],
            'verifier_sha256': frozen['verifier']['snapshot']['archive_sha256'],
            'environment_sha256': sha256_json(frozen['environment'])},
        harness={'cleanup_verified': bool(cleanup), 'error': error, 'end_reason': end_reason,
            'stage': stage, 'timeout_stage': timeout_stage, 'timeout_scope': timeout_scope,
            'client_kind': 'synthetic' if guard.synthetic else 'live', 'memory': 'off', 'compaction': 'off',
            'budget_violation': budget_violation})
    if plan.arm.prompt is not None:
        manifest['prompt_arm'] = binding(plan.arm, guard.verified_requests)
    manifest['container_task']['evidence_hashes'] = {
        str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [output / name for name in (
            'inputs/task.json', 'inputs/case.json', 'inputs/snapshot.tar',
            'inputs/verifier/plan.json', 'inputs/verifier/verifier.tar',
            'run-evidence.json', 'task/environment.json', 'task/artifacts.tar',
            'verification/result.json', 'verification/raw-result.json', 'verification/verification.json')]
        if p.is_file() and not p.is_symlink()}
    usage = {'model_calls': guard.model_calls, 'model_call_rows': len(rows['calls']),
        'tool_calls': len(rows['tools']), 'tool_requests': guard.tool_calls,
        'cost': guard.cost(), 'token_known_subtotal': guard.token_totals,
        'tokens': guard.token_totals if guard.usage_calls == guard.model_calls else None,
        'wall_seconds': round(time.monotonic() - started, 3)}
    return new_trial(plan.case, manifest, plan.protocol.experiment, trial_id=plan.trial_id,
        status=status, run_id=str(run_id) if run_id else None, started_at=started_at,
        ended_at=datetime.now(timezone.utc).isoformat(), usage=usage).with_graders((grader,))


def evidence_errors(trial):
    binding = trial.manifest.get('container_task') or {}
    output = Path(binding.get('evidence', ''))
    errors = []
    hashes = binding.get('evidence_hashes')
    if not hashes:
        return [{'trial': trial.trial_id, 'reason': 'missing container evidence hashes'}]
    for name, digest in hashes.items():
        path = output / relative_path(name)
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            errors.append({'trial': trial.trial_id, 'reason': 'container evidence changed: ' + name})
    return errors


def build_report(root, protocol):
    """Keep pending claims and unknown costs visible alongside ordinary records."""
    store = TrialStore(root)
    trials = records(store, protocol)
    directory = store.directory(protocol.experiment)
    completed = {t.trial_id for t in trials}
    pending = [p.stem for p in sorted((directory / '.claims').glob('*.claim')) if p.stem not in completed]
    from eval.report import _invalid_trials
    invalid = _invalid_trials(protocol, trials)
    synthetic_ids = {t.trial_id for t in trials if t.manifest.get('harness', {}).get('client_kind') == 'synthetic'}
    invalid = [e for e in invalid if not (e['trial'] in synthetic_ids and
               e['reason'] == 'trial is not identified as real model evidence')]
    invalid += [e for t in trials for e in evidence_errors(t)]
    invalid_ids = {e['trial'] for e in invalid}
    return {'report_version': 1, 'kind': 'container-trial-diagnostic',
        'binding_errors': invalid,
        'planned': protocol.slots, 'recorded': len(trials), 'unfinished_claims': pending,
        'missing': protocol.slots - len(trials),
        'unknown_cost': bool(pending) or any(not known_cost(t.usage.get('cost', {}).get('usd')) for t in trials),
        'outcomes': {v: sum(('infra_error' if t.trial_id in invalid_ids or '*' in invalid_ids else t.verdict) == v for t in trials) for v in ('pass', 'fail', 'infra_error', 'inconclusive')},
        'trials': [t.to_json() for t in trials],
        'comparison_claim': 'none; diagnostic slots are not a completed two-arm comparison'}


async def cleanup_trial(output):
    """Recover resource cleanup only; never rerun, regrade or erase a slot claim."""
    output = Path(output)
    for cls, path in ((TaskEnvironment, output / 'task'), (Verification, output / 'verification')):
        state_file = 'environment.json' if cls is TaskEnvironment else 'verification.json'
        if (path / state_file).exists():
            await cls(path).cleanup()
