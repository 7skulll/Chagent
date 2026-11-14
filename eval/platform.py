"""Local operator platform: frozen plans, single worker leases, durable status."""
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import uuid4

from eval import casefile, prereg, report
from eval.cli import REPOSITORY, _evaluation_env
from eval.integrity import execution, identity, publish
from eval.manifests import VersionManifest
from eval.records import TrialStore
from eval.runner import PINNED_ENV, assert_axes, fixture_for
from chagent.config import Settings
from chagent.models.settings import ModelSettings


class Busy(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def save(path, value):
    """Only job status is replaceable; protocol and trials remain immutable."""
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        try:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def lease(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open('a')
    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        file.close()
        raise Busy('已有测试正在运行，请等待结束或停止后续槽位') from None
    return file


def resolved(overrides, arm):
    env = {**_evaluation_env(), **overrides, **PINNED_ENV,
           'CHAGENT_MODEL': arm.model, 'CHAGENT_PROVIDER': arm.provider}
    settings = Settings.from_env(env)
    model = replace(ModelSettings.from_env(env), retry_attempts=0)
    pinned = execution(identity(VersionManifest.capture(REPOSITORY).to_json(), settings, model))
    return env, settings, model, pinned


class Platform:
    def __init__(self, root=None, cases=None, *, account_id=None, account_root=None):
        self.root = Path(root or REPOSITORY / 'eval/results')
        self.cases = Path(cases or REPOSITORY / 'eval/cases')
        self.account_id = account_id
        self.account_root = Path(account_root) if account_root else None
        self.state = self.root / '.platform'
        self.state.mkdir(parents=True, exist_ok=True)
        self.children = []

    def directory(self, identifier):
        if (not isinstance(identifier, str) or identifier in {'.', '..'}
                or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', identifier)):
            raise ValueError('invalid platform plan identifier')
        return self.root / identifier

    def catalog(self):
        result = []
        for case in casefile.load_all(self.cases):
            supported = (case.authority.get('boundary') == 'host'
                         and all(g.get('kind') == 'deterministic' and g.get('spec') for g in case.graders))
            result.append({'key': case.key, 'title': case.title, 'capability': case.capability,
                'sha256': case.sha256, 'supported': supported,
                'reason': '' if supported else '尚无可执行的确定性判据或当前边界不适用'})
        return result

    def plan(self, request):
        selected = set(request['cases'])
        cases = [c for c in casefile.load_all(self.cases) if c.key in selected]
        runnable = {c['key'] for c in self.catalog() if c['supported']}
        if len(cases) != len(selected) or not selected or not selected <= runnable:
            raise ValueError('请选择可执行且版本匹配的用例')
        arms = [prereg.Arm('baseline', request['provider'], request['baseline']),
                prereg.Arm('candidate', request['provider'], request['candidate'])]
        if arms[0].model == arms[1].model:
            raise ValueError('基线与候选名称相同；请选择两个明确的对照对象')
        overrides = {'CHAGENT_CONTEXT_WINDOW': str(request['context_window']),
                     'CHAGENT_MAX_TOKENS': str(request['max_tokens'])}
        identities = []
        for arm in arms:
            _, settings, _, pinned = resolved(overrides, arm)
            identities.append(pinned)
            for case in cases:
                assert_axes(case, settings)
                fixture_for(case)
        if identities[0] != identities[1]:
            raise ValueError('两臂的执行配置不一致')
        # Require the backend before any plan can be started, not after paying.
        if any((g.get('spec') or {}).get('name', '').startswith('stats_contract')
               for c in cases for g in c.graders):
            from eval.isolation import available
            if not available():
                raise ValueError('当前主机无法启动控制器沙箱，尚未调用模型')
        identifier = 'web-' + datetime.now(timezone.utc).strftime('%Y%m%d') + '-' + uuid4().hex[:12]
        protocol = prereg.build(experiment=identifier, question=request['question'],
            hypothesis='按固定预算和用例比较两臂，不按试验结果调整重复或阈值。', cases=cases, arms=arms,
            repeats=request['repeats'], min_repeats_for_stability=3, order='interleaved',
            budget={'max_turns': request['max_turns'], 'max_tool_calls': request['max_tool_calls'],
                    'wall_seconds': request['wall_seconds'], 'total_cost_usd': request['cost_cap']},
            thresholds={'improve_points': request['threshold'], 'regress_points': request['threshold']},
            analysis='Wilson95%/Newcombe95%; per-case counts and flips. No equivalence/stability claim from small samples.',
            exclusions='Keep all slots. Infrastructure/undecidable/missing remain explicit; no red reruns.',
            stopping_rule='Each slot once. Stop between trials on user request; stop on unknown cost, cap, version drift. Never auto-resume after a crash.',
            pricing={'input_usd_per_mtok': request['input_rate'],
                     'output_usd_per_mtok': request['output_rate'], 'source': request['price_source']})
        folder = self.directory(identifier)
        prereg.freeze(protocol, folder)
        publish(folder / '.platform' / 'plan.json', {'created_at': now(), 'overrides': overrides,
            'schema': ('chagent_eval_' + self.account_id if self.account_id else 'chagent_eval_platform'),
            'account_id': self.account_id, 'account_root': str(self.account_root) if self.account_root else None,
            'expected_execution': identities[0],
            'protocol_sha256': protocol.sha256, 'pricing_confirmed': request['pricing_confirmed']})
        save(folder / '.platform' / 'status.json', {'state': 'planned', 'created_at': now(),
                                                  'updated_at': now(), 'error': None})
        return self.detail(identifier)

    def start(self, identifier):
        folder = self.directory(identifier)
        if not (folder / 'protocol.json').is_file():
            raise FileNotFoundError('platform plan not found')
        if not (folder / '.platform/status.json').is_file():
            raise ValueError('历史实验只读，请新建计划')
        status = json.loads((folder / '.platform/status.json').read_text())
        if status['state'] != 'planned':
            raise ValueError('此计划已经启动；失败或中断不能重跑同一实验')
        with lease(self.state / 'worker.lock') as global_lease:
            with lease(folder / '.platform/worker.lock') as job_lease:
                # Re-read under the lock; two browser clicks must not start twice.
                status = json.loads((folder / '.platform/status.json').read_text())
                if status['state'] != 'planned':
                    raise ValueError('此计划已经启动')
                status.update(state='starting', updated_at=now())
                save(folder / '.platform/status.json', status)
                try:
                    with (folder / '.platform/worker.log').open('a') as log:
                        child = subprocess.Popen([sys.executable, '-m', 'eval.platform_worker',
                            '--root', str(self.root.resolve()), '--cases', str(self.cases.resolve()),
                            '--id', identifier, '--lease', str(global_lease.fileno()),
                            '--job-lease', str(job_lease.fileno())], cwd=REPOSITORY,
                            env={**os.environ, 'PYTHONPATH': str(REPOSITORY / 'src') + os.pathsep + str(REPOSITORY)},
                            pass_fds=(global_lease.fileno(), job_lease.fileno()),
                            start_new_session=True, stdout=log, stderr=log)
                    self.children = [c for c in self.children if c.poll() is None] + [child]
                except Exception:
                    status.update(state='interrupted', updated_at=now(), error='无法启动测试进程；未自动重试')
                    save(folder / '.platform/status.json', status)
                    raise
        return self.detail(identifier)

    def stop(self, identifier):
        folder = self.directory(identifier)
        state = self.detail(identifier)['status']['state']
        if state not in ('starting', 'running'):
            raise ValueError('当前没有正在运行的测试')
        publish(folder / '.platform/stop.json', {'requested': True}, identical_ok=True)
        return self.detail(identifier)

    def detail(self, identifier):
        folder = self.directory(identifier)
        path = folder / '.platform/status.json'
        if not (folder / 'protocol.json').is_file():
            raise FileNotFoundError('platform plan not found')
        protocol = prereg.load(folder)
        status = (json.loads(path.read_text()) if path.is_file() else
                  {'state': 'archived', 'created_at': protocol.created_at,
                   'updated_at': protocol.created_at, 'error': None})
        if status['state'] in ('starting', 'running'):
            try:
                with lease(folder / '.platform/worker.lock'):
                    # No live worker owns this job; never guess that a paid slot is free.
                    status = json.loads(path.read_text())
                    if status['state'] in ('starting', 'running'):
                        status.update(state='interrupted', updated_at=now(),
                                      error='执行进程已退出；保留槽位与费用证据，不自动续跑')
                        save(path, status)
            except Busy:
                pass
        trials = TrialStore(self.root).read(identifier, ignore=('protocol.json',))
        document = report.build(protocol, trials)
        return {'id': identifier, 'status': status, 'protocol': protocol.to_json(),
            'stop_requested': (folder / '.platform/stop.json').exists(), 'report': document,
            'trials': [{'trial_id': t.trial_id, 'case': f'{t.case_id}@{t.case_version}',
                'arm': t.manifest.get('arm'), 'verdict': t.verdict, 'run_id': t.run_id,
                'usage': t.usage, 'graders': [g.to_json() for g in t.graders], 'notes': t.notes,
                'diagnosis': ('测量失败，先检查基础设施' if t.verdict == 'infra_error' else
                    '预算限制' if t.manifest.get('harness', {}).get('budget_violation') else
                    '任务判据未满足；根因尚未确认' if t.verdict == 'fail' else
                    '证据不足' if t.verdict == 'inconclusive' else '判据通过')} for t in trials]}

    def jobs(self):
        self.children = [child for child in self.children if child.poll() is None]
        jobs = []
        for path in sorted(self.root.glob('*/protocol.json'), reverse=True):
            identifier = path.parent.name
            try:
                item = self.detail(identifier)
            except (OSError, ValueError, KeyError, TypeError) as failure:
                jobs.append({'id': identifier, 'status': {'state': 'record_error',
                    'error': f'记录读取失败（{type(failure).__name__}），原文件已保留',
                    'updated_at': None, 'created_at': None}, 'question': identifier,
                    'observed': None, 'planned': None, 'claim': 'incomparable',
                    'cost': {'estimated_usd': None, 'unknown_trials': None}})
                continue
            jobs.append({'id': identifier, 'status': item['status'],
                'observed': item['report']['schedule']['observed'],
                'planned': item['report']['schedule']['planned'], 'claim': item['report']['claim'],
                'question': item['protocol']['question'],
                'cost': item['report']['budget']['cost']})
        return jobs
