"""Fixed reserved-task regression experiments over the existing container runner.

Prepare and calibrate before live execution. Completed slots are immutable; a
pending claim, unknown spend or drift prevents additional calls. No scheduler.
"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from math import comb
from pathlib import Path
import shutil
import tempfile
from statistics import NormalDist, median

from eval import container_trial as ct, prereg, report
from eval.cli import _eval_database, _evaluation_env
from eval.integrity import publish
from eval.manifests import sha256_json
from eval.prompts import variant
from chagent.models.fake import FakeModelClient

SUITE = Path(__file__).with_name('holdouts') / 'core-v2'
CANDIDATE = ('Before changing files, inspect the inputs and the full task contract. '
             'Identify edge cases, conflicting or superseded information, and completeness requirements. '
             'Implement the smallest correct solution. Run concrete checks against the saved artifacts, '
             'including boundary cases and negative cases when applicable. Verify that the delivered '
             'files satisfy every requested condition; do not rely on a success message alone.')
ENV = {'CHAGENT_CONTEXT_WINDOW': '32768', 'CHAGENT_MAX_TOKENS': '2048'}


def fingerprint(suite):
    files = {}
    for p in sorted(Path(suite).rglob('*')):
        if '__pycache__' in p.parts or p.suffix == '.pyc':
            continue
        if p.is_symlink():
            raise ValueError('Suite rejects symbolic links')
        if p.is_file():
            files[p.relative_to(suite).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return files


def bound(root):
    binding = ct.read_json(root / 'suite-binding.json')
    if fingerprint(root / 'suite') != binding['files']:
        raise ValueError('Reserved suite has changed')
    protocol = prereg.load(root / binding['experiment'])
    if protocol.sha256 != binding['protocol_sha256']:
        raise ValueError('Protocol differs from reserved suite binding')
    tasks = {}
    for name, entry in binding['tasks'].items():
        path = root / 'catalog' / name
        case, _, digest = ct.load_task(path)
        if case.sha256 != entry['case_sha256'] or digest != entry['task_sha256']:
            raise ValueError('Frozen task binding changed')
        tasks[case.key] = path
    if set(tasks) != {c['id']+'@'+str(c['version']) for c in protocol.cases}:
        raise ValueError('Protocol and reserved catalog differ')
    return binding, protocol, tasks


def protocol_for(experiment, cases, *, calibration=False, cost_cap=8):
    return prereg.build(experiment=experiment,
        question='Which reserved task capabilities improve or regress under the frozen verification prompt?',
        hypothesis='The added verification prompt improves artifact correctness without regressions.',
        cases=cases, arms=([prereg.Arm('correct','deepseek','deepseek-flash'),
                           prereg.Arm('incorrect','deepseek','deepseek-flash')] if calibration else
                          [prereg.Arm('baseline','deepseek','deepseek-flash',prompt=variant('', 'default-v1')),
                           prereg.Arm('candidate','deepseek','deepseek-flash',prompt=variant(CANDIDATE, 'verify-contract-v1'))]),
        repeats=3 if calibration else 20, min_repeats_for_stability=3 if calibration else 20,
        order='interleaved', budget={'max_turns':8,'max_tool_calls':8,'wall_seconds':120,'total_cost_usd':cost_cap},
        thresholds={'improve_points':0.20,'regress_points':0.20},
        analysis='20 independent resets per case/arm, repeat-major interleaving. Wilson/Newcombe 95% exploratory intervals. Promotion directions use Bonferroni simultaneous intervals across four cases (family alpha=.05), threshold 20 percentage points. No pooled heterogeneous-task score. Power is illustrative, not a promise for 20-point effects.',
        exclusions='Keep every planned slot; infra errors, inconclusive and missing remain explicit. No red reruns or outcome-dependent repeats. Controls are synthetic and excluded from live scores.',
        stopping_rule='Each slot once. Stop further calls on pending claim, unknown spend, evidence/identity drift, cleanup failure or exhausted cap. Do not change tasks, prompts, sample size or thresholds after first live trial.',
        pricing={'input_usd_per_mtok':.30,'output_usd_per_mtok':1.20,
                 'source':'https://api-docs.deepseek.com/quick_start/pricing/ verified 2026-11-18; Flash peak USD/M token ceilings, including cache input and reasoning output. Estimates are not invoices.'})


async def prepare(root, suite, image, experiment, cost_cap=8):
    root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(suite, root/'suite')
    document = ct.read_json(root/'suite/suite.json')
    files = fingerprint(root/'suite')
    tasks, cases = {}, []
    for task in document['tasks']:
        services = deepcopy(task['services'])
        for service in services:
            service['image'] = service['image_id'] = image
        spec = {k:task[k] for k in ('id','title','prompt','artifacts')}
        spec.update(version=task.get('version',1), workspace=str(root/'suite'/task['name']/'input'),
            verifier=str(root/'suite'/task['name']/'verifier'), verifier_timeout=15,
            budget={'max_turns':8,'max_tool_calls':8,'wall_seconds':120,'max_cost_usd':.05},
            environment={'image_id':image,'network':task['network'],'services':services,
                         'resources':document['resources'],'verifier_resources':document['resources']})
        with tempfile.TemporaryDirectory(prefix='chagent-reserved-input-') as temp:
            staging = Path(temp)/'input'
            shutil.copytree(spec['workspace'], staging)
            spec['workspace'] = str(staging)
            case, _, digest = await ct.freeze_task(spec, root/'catalog'/task['name'])
        cases.append(case)
        tasks[task['name']] = {'case_sha256':case.sha256,'task_sha256':digest}
    protocol = protocol_for(experiment, cases, cost_cap=cost_cap)
    prereg.freeze(protocol, root/experiment)
    publish(root/'suite-binding.json', {'suite':document['id'],'experiment':experiment,'files':files,
        'suite_sha256':sha256_json(files),'protocol_sha256':protocol.sha256,'tasks':tasks,'runtime_env':ENV,
        'source_suite':str(Path(suite).resolve()),'purpose':document['purpose']})
    publish(root/'design-power.json', design_power())
    print(json.dumps({'prepared':str(root),'planned':protocol.slots,'catalog':str(root/'catalog')}),flush=True)


def design_power(n=20):
    """Exact binomial enumeration for illustrative alternatives, before trials."""
    z=NormalDist().inv_cdf(1-.05/8)
    values=[]
    for p0,p1 in ((.3,.8),(.5,.7),(.8,.8)):
        probability=0.0
        for a in range(n+1):
            for b in range(n+1):
                interval=report.newcombe(report.wilson(a,n,z),report.wilson(b,n,z),a/n,b/n)
                delta={'rate_candidate_minus_baseline':(b-a)/n,'interval':interval}
                if report._direction(delta,{'improve_points':.2,'regress_points':.2}) in ('improved','regressed'):
                    probability += comb(n,a)*p0**a*(1-p0)**(n-a)*comb(n,b)*p1**b*(1-p1)**(n-b)
        values.append({'baseline_rate':p0,'candidate_rate':p1,'direction_detection_probability':probability})
    return {'n_per_arm':n,'family_cases':4,'alpha':.05,'threshold':.2,'illustrative_only':True,'alternatives':values}


def family_deltas(document, *, alpha=.05):
    z = NormalDist().inv_cdf(1-alpha/(2*len(document['cases'])))
    result=[]
    for case in document['cases']:
        a,b = case['arms']['baseline'],case['arms']['candidate']
        interval = report.newcombe(report.wilson(a['pass'],a['denominator'],z),
            report.wilson(b['pass'],b['denominator'],z), a['rate'] or 0, b['rate'] or 0)
        delta = {'rate_candidate_minus_baseline': None if a['rate'] is None or b['rate'] is None else b['rate']-a['rate'], 'interval':interval}
        result.append({'case':case['case'], **delta, 'direction':report._direction(delta, {'improve_points':.2,'regress_points':.2})})
    return result


def render(root):
    binding, protocol, _ = bound(root)
    trials = ct.records(ct.TrialStore(root), protocol)
    diagnostic = ct.build_report(root, protocol)
    document = report.build(protocol, trials)
    if diagnostic['binding_errors'] or diagnostic['unfinished_claims']:
        document['claim'] = 'incomparable'
        document['claim_reason'] = 'container evidence invalid or unfinished permanent claims'
    document['container_diagnostic'] = {k:v for k,v in diagnostic.items() if k!='trials'}
    document['family_comparisons'] = family_deltas(document)
    document['exploratory_claim'] = document['claim']
    complete = not (document['invalid'] or document['missing'] or document['excluded'] or diagnostic['binding_errors'] or diagnostic['unfinished_claims'] or diagnostic['unknown_cost'] or document['budget']['cost']['over_cap'] or document['budget']['violations'])
    document['decision_ready'] = complete and all(cell['denominator']==protocol.repeats for c in document['cases'] for cell in c['arms'].values())
    if document['decision_ready']:
        directions=[d['direction'] for d in document['family_comparisons']]
        document['claim'] = ('regression_detected' if 'regressed' in directions else 'improvement_supported' if 'improved' in directions else 'no_difference_observed')
        document['claim_reason'] = 'Four-case simultaneous intervals and preregistered practical threshold'
    document['scope'] = 'Fixed '+binding['suite']+' tasks, task-command-only-v1, memory/compaction off; prompt mechanism comparison on one floating model alias. No full product or unseen-task generalization claim.'
    lines=['# 固定保留任务两臂回归', '', f"实验：{protocol.experiment}", '', f"完成：{len(trials)}/{protocol.slots}；报告结论：{document['claim']}；证据完整：{document['decision_ready']}", '', '| 能力任务 | 基线成功 | 候选成功 | 差值 | 同时区间 | 判断 |', '|---|---:|---:|---:|---|---|']
    labels={'improved':'改善','regressed':'退步','no_difference_observed':'未观察到明确差异','inconclusive':'无法判定','higher_but_below_threshold':'提高但未达阈值','lower_but_below_threshold':'降低但未达阈值'}
    for case,delta in zip(document['cases'],document['family_comparisons']):
        a,b=case['arms']['baseline'],case['arms']['candidate']
        direction=labels[delta['direction']] if document['decision_ready'] else '证据未齐，不作结论'
        lines.append(f"| {case['case']} | {a['pass']}/{a['denominator']} | {b['pass']}/{b['denominator']} | {delta['rate_candidate_minus_baseline']} | {delta['interval']} | {direction} |")
    lines += ['', '每题20次/臂；同时区间按四题Bonferroni校正，实际差异阈值20个百分点。没有差异证据不代表等效。相同任务反复运行衡量运行波动，不能替代更多独立任务。', '', '| 指标 | 基线 | 候选 |', '|---|---:|---:|']
    for field in ('wall_seconds','model_calls','tool_calls'):
        vals=[[t.usage[field] for t in trials if t.manifest['arm']==arm] for arm in ('baseline','candidate')]
        lines.append(f"| {field} 中位数（描述性） | {median(vals[0]) if vals[0] else None} | {median(vals[1]) if vals[1] else None} |")
    lines += ['', '费用、错误与所有缺失槽位详见 report.json；原始证据位于每个 container-trials 子目录。', '', document['scope']]
    (root/'report.json').write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n')
    (root/'report.md').write_text('\n'.join(lines)+'\n')
    return document


async def execute(root, *, calibration=False):
    binding, live_protocol, tasks = bound(root)
    suite=ct.read_json(root/'suite/suite.json')
    if calibration:
        cases=[ct.load_task(path)[0] for path in tasks.values()]
        protocol=protocol_for(live_protocol.experiment+'-controls',cases,calibration=True)
        prereg.freeze(protocol,root/protocol.experiment)
    else:
        calibration_record=ct.read_json(root/'calibration.json')
        control_protocol=prereg.load(root/(live_protocol.experiment+'-controls'))
        control_trials=ct.records(ct.TrialStore(root),control_protocol)
        control_report=ct.build_report(root,control_protocol)
        if (calibration_record['suite_sha256']!=binding['suite_sha256'] or not calibration_record['passed']
                or len(control_trials)!=control_protocol.slots or control_report['binding_errors']
                or control_report['unknown_cost'] or control_report['unfinished_claims']
                or any(t.verdict!=('pass' if t.manifest['arm']=='correct' else 'fail') for t in control_trials)):
            raise ValueError('Missing or invalid calibration evidence')
        protocol=live_protocol
    env={**_evaluation_env(),**binding['runtime_env']}
    schema='chagent_eval_reserved_'+hashlib.sha256(live_protocol.experiment.encode()).hexdigest()[:12]
    by_id={t['id']:t for t in suite['tasks']}
    # execute_slot enforces known costs, identity anchors, per-request reservations,
    # permanent claims and no rerun. Refuse all occupied plans on a second launch.
    if list((root/protocol.experiment/'.claims').glob('*.claim')):
        raise ValueError('Experiment already started; no automatic resume or reruns')
    async with _eval_database(env.get('CHAGENT_EVAL_DATABASE_URL') or env.get('CHAGENT_DATABASE_URL'),schema) as db:
        for repeat in range(protocol.repeats):
            for entry in protocol.cases:
                for arm in protocol.arms:
                    key=entry['id']+'@'+str(entry['version'])
                    if (root/'STOP').exists():
                        if not calibration: render(root)
                        print('Stopped at slot boundary by operator',flush=True)
                        return
                    fake=FakeModelClient('exec_command',{'cmd':by_id[entry['id']]['controls'][arm.name]}) if calibration else None
                    record=await ct.execute_slot(tasks[key],protocol,arm,repeat,database=db,root=root,env=env,client=fake)
                    print(json.dumps({'trial':record.trial_id,'verdict':record.verdict,'calls':record.usage['model_calls'],'usd':record.usage['cost']['usd'],'seconds':record.usage['wall_seconds']},ensure_ascii=False),flush=True)
                    diagnostic=ct.build_report(root,protocol)
                    if not calibration: render(root)
                    if diagnostic['binding_errors'] or diagnostic['unknown_cost'] or diagnostic['unfinished_claims'] or not record.manifest['harness']['cleanup_verified']:
                        raise ValueError('Stopped: unknown cost, invalid evidence or cleanup failure')
                    if calibration and record.verdict!=('pass' if arm.name=='correct' else 'fail'):
                        raise ValueError('Calibration control mismatch; no live calls allowed')
    if calibration:
        publish(root/'calibration.json',{'passed':True,'suite_sha256':binding['suite_sha256'],'protocol_sha256':protocol.sha256,'recorded':protocol.slots})
    else:
        render(root)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','calibrate','run','report','stop'))
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--suite',type=Path,default=SUITE)
    parser.add_argument('--image')
    parser.add_argument('--cost-cap',type=float,default=8)
    parser.add_argument('--experiment',default='core-v2-prompt-20261118')
    args=parser.parse_args()
    root=args.out.resolve()
    if args.action=='stop':
        (root/'STOP').touch(exist_ok=True)
        return
    if args.action=='prepare':
        if not args.image: parser.error('prepare requires an explicitly prepared immutable --image')
        asyncio.run(prepare(root,args.suite,args.image,args.experiment,args.cost_cap))
    elif args.action=='report':
        d=render(root)
        print(json.dumps({'claim':d['claim'],'decision_ready':d['decision_ready'],'counts':d['counts']}))
    else:
        asyncio.run(execute(root,calibration=args.action=='calibrate'))


if __name__=='__main__':
    main()

# core-v1 live execution withdrawn: rollback.action canonical format was not
# disclosed in the task contract. Preserve old claims; use a revised suite.
