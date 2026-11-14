"""One leased platform job. HTTP restarts never replay paid trial claims."""
import argparse
import asyncio
import json
import os
from types import SimpleNamespace

from eval import casefile, prereg
from eval.budgets import known_cost
from eval.cli import _run_plans, _slots
from eval.platform import Platform, now, resolved, save
from eval.records import TrialStore


async def execute(platform, identifier, executor=None):
    executor = executor or _run_plans
    folder = platform.directory(identifier)
    status_path = folder / '.platform/status.json'
    status = json.loads(status_path.read_text())
    status.update(state='running', updated_at=now(), error=None)
    save(status_path, status)
    try:
        protocol = prereg.load(folder)
        plan = json.loads((folder / '.platform/plan.json').read_text())
        if plan.get('protocol_sha256') != protocol.sha256 or plan.get('pricing_confirmed') is not True:
            raise ValueError('冻结计划与协议不一致，尚未调用模型')
        owner_store = None
        if plan.get('account_id'):
            from chagent.users.store import AccountStore
            owner_store = AccountStore(plan['account_root'])
            owner = owner_store.user(plan['account_id'])
            if owner is None or owner['role'] != 'admin':
                raise ValueError('管理员权限已撤销，尚未调用模型')
        env, _, _, pinned = resolved(plan['overrides'], protocol.baseline)
        if pinned != plan['expected_execution']:
            raise ValueError('代码或执行配置已变化；请新建计划，尚未调用模型')
        cases = {c.key: c for c in casefile.load_all(platform.cases)}
        chosen = [cases[f"{c['id']}@{c['version']}"] for c in protocol.cases]
        if prereg.check(protocol, chosen, list(protocol.arms)):
            raise ValueError('用例已变化；请新建计划')
        if any((g.get('spec') or {}).get('name', '').startswith('stats_contract')
               for c in chosen for g in c.graders):
            from eval.isolation import available
            if not available():
                raise ValueError('控制器沙箱不可用，尚未调用模型')
        args = SimpleNamespace(only=[], arm=None, all=True,
            db=env.get('CHAGENT_EVAL_DATABASE_URL') or env.get('CHAGENT_DATABASE_URL'),
            schema=plan['schema'])
        store = TrialStore(platform.root)
        existing = store.read(identifier, ignore=('protocol.json',))
        if existing or (folder / '.claims').exists():
            raise ValueError('该实验已经消费或认领槽位，禁止自动重跑')
        state = 'completed'
        slots = _slots(protocol, cases, args, [])
        for position, trial_plan in enumerate(slots, start=1):
            if owner_store is not None:
                owner = owner_store.user(plan['account_id'])
                if owner is None or owner['role'] != 'admin':
                    state = 'permission_revoked'
                    break
            if (folder / '.platform/stop.json').exists():
                state = 'stopped'
                break
            await executor([trial_plan], args, platform.root, store, env, existing)
            existing = store.read(identifier, ignore=('protocol.json',))
            costs = [t.usage.get('cost', {}).get('usd') for t in existing]
            if any(not known_cost(cost) for cost in costs):
                state = 'cost_unknown'
                break
            # TrialStore order is lexical, not execution order.
            last = next((t for t in existing if t.trial_id == trial_plan.trial_id), None)
            if last is None:
                raise RuntimeError('执行器未保存当前槽位的 trial 记录，已停止后续调用')
            if (sum(costs) >= protocol.budget['total_cost_usd']
                    or (last and last.usage.get('model_calls') == 0
                        and last.manifest.get('harness', {}).get('budget_violation'))):
                state = 'completed' if position == len(slots) else 'budget_exhausted'
                break
            status.update(updated_at=now())
            save(status_path, status)
        status.update(state=state, updated_at=now())
    except Exception as error:
        status.update(state='failed', updated_at=now(), error=f'{type(error).__name__}: {error}'[:500])
    finally:
        save(status_path, status)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--cases', required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--lease', type=int, required=True)
    parser.add_argument('--job-lease', type=int, required=True)
    args = parser.parse_args()
    try:
        # The parent passes *owned* open file descriptions, without an unlock gap.
        os.fstat(args.lease)
        os.fstat(args.job_lease)
        asyncio.run(execute(Platform(args.root, args.cases), args.id))
    finally:
        os.close(args.lease)
        os.close(args.job_lease)


if __name__ == '__main__':
    main()
