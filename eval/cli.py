"""Operator command line for the evaluation contracts.

Eleven commands, each answering one question:

``manifest``  which tree, prompts, tools, skills and model produced a result
``export``    what one run actually recorded, bounded and redacted
``timeline``  when each step ran, how well that is known, and what is missing
``attribute`` which diagnostic labels the records support, and what they do not
``annotate``  record a human verdict with its evidence and its dataset
``labels``    per-label precision/recall, or why it cannot be estimated
``case``      whether a case document still satisfies the published contract
``replay``    whether a recorded call may be replayed, and what happens if it is
``prereg``    what an experiment promises before its first trial
``trial``     run one preregistered slot and grade it
``report``    what the observed trials do and do not support

The CLI is an operator tool, not a product entry point. Every command except
``trial`` only reads: the read paths use a read-only transaction, and nothing
here writes to the product's own database. ``trial`` is the one command that
spends model calls and writes rows, so it runs against a separate evaluation
schema and refuses to start a slot the protocol did not promise.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from eval import annotations, casefile
from eval.replay import cassette as cassettes
from eval.replay import plan as replay_plan
from eval.replay import pure as replay_pure
from eval.replay import sandbox as replay_sandbox
from eval.attribute import AttributeProjection, format_diagnosis, sample_id
from eval.evidence import EvalEvidence
from eval.manifests import VersionManifest
from eval.spans import SpanProjection, format_timeline

REPOSITORY = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("manifest", help="fingerprint a checkout, optionally with a run's request")
    manifest.add_argument("--repo", type=Path, default=REPOSITORY)
    manifest.add_argument("--model", help="provider/model as configured for the trial")
    manifest.add_argument("--image", help="container image digest the trial ran in")
    manifest.add_argument("--run", help="attach the first request of this run id")
    manifest.add_argument("--db", help="database URL (default: CHAGENT_DATABASE_URL)")
    manifest.add_argument("--out", type=Path, help="write JSON here instead of stdout")

    export = commands.add_parser("export", help="export one run's recorded evidence")
    export.add_argument("run_id")
    export.add_argument("--events", type=int, default=200)
    export.add_argument("--payload", action="store_true", help="include flattened payload keys")
    export.add_argument("--db", help="database URL (default: CHAGENT_DATABASE_URL)")
    export.add_argument("--out", type=Path, help="write JSON here instead of stdout")

    case = commands.add_parser("case", help="validate case documents")
    case.add_argument("paths", nargs="+", type=Path)

    timeline = commands.add_parser("timeline", help="project one run into spans and timing accounting")
    timeline.add_argument("run_id")
    timeline.add_argument("--json", action="store_true", help="emit the whole document as JSON")
    timeline.add_argument("--limit", type=int, default=200, help="spans to print in text mode")
    timeline.add_argument("--db", help="database URL (default: CHAGENT_DATABASE_URL)")
    timeline.add_argument("--out", type=Path, help="write JSON here instead of stdout")

    attribute = commands.add_parser("attribute", help="label one run from its recorded evidence")
    attribute.add_argument("run_id")
    attribute.add_argument("--case", type=Path, help="case document, for expectation-dependent reads")
    attribute.add_argument("--annotations", type=Path, default=annotations.LEDGER,
                           help="ledger used to confirm a label; default: eval/annotations/labels.jsonl")
    attribute.add_argument("--json", action="store_true", help="emit the whole document as JSON")
    attribute.add_argument("--explain", action="store_true", help="print rule checks and rejections")
    attribute.add_argument("--db", help="database URL (default: CHAGENT_DATABASE_URL)")
    attribute.add_argument("--out", type=Path, help="write JSON here instead of stdout")

    annotate = commands.add_parser("annotate", help="record one human verdict")
    annotate.add_argument("--sample", required=True, help="run:<uuid> or another stable sample id")
    annotate.add_argument("--label", required=True, choices=sorted(annotations.LABELS))
    annotate.add_argument("--verdict", required=True, choices=annotations.VERDICTS)
    annotate.add_argument("--dataset", required=True, choices=annotations.DATASETS)
    annotate.add_argument("--reviewer", required=True)
    annotate.add_argument("--certainty", default="observed", choices=("observed", "suspected", "confirmed"))
    annotate.add_argument("--evidence", action="append", default=[],
                          help="table:record reference; repeat for each")
    annotate.add_argument("--note", default="")
    annotate.add_argument("--run", help="run id this sample refers to")
    annotate.add_argument("--file", type=Path, default=annotations.LEDGER)

    replay = commands.add_parser(
        "replay", help="decide, execute or verify a replay; exit 3 means the replay ran "
                       "and reported not ok")
    replay.add_argument("action", choices=("plan", "pure", "state", "cassette-record",
                                           "cassette-verify"))
    replay.add_argument("--run", help="run id (plan, cassette-record, cassette-verify)")
    replay.add_argument("--case", type=Path, help="case document supplying the fixture")
    replay.add_argument("--scenario", type=Path, help="stateful scenario JSON")
    replay.add_argument("--cassette", type=Path, help="cassette file to verify")
    replay.add_argument("--vectors", type=Path, help="pure vector file (default: bundled)")
    replay.add_argument("--json", action="store_true", help="emit the whole document as JSON")
    replay.add_argument("--db", help="database URL (default: CHAGENT_DATABASE_URL)")
    replay.add_argument("--out", type=Path, help="write JSON here instead of stdout")

    prereg = commands.add_parser("prereg", help="freeze an experiment protocol before its first trial")
    prereg.add_argument("--spec", type=Path, required=True,
                        help="JSON with the question, hypothesis, arms, repeats, budget and thresholds")
    prereg.add_argument("--case", type=Path, action="append", required=True,
                        help="case document or directory; repeat for each")
    prereg.add_argument("--out", type=Path, help="results directory (default: eval/results/<experiment>)")
    prereg.add_argument("--json", action="store_true", help="emit the frozen protocol as JSON")

    trial = commands.add_parser("trial", help="run preregistered trials and grade them")
    trial.add_argument("--protocol", type=Path, required=True,
                       help="results directory or its protocol.json")
    trial.add_argument("--cases", type=Path, default=REPOSITORY / "eval" / "cases")
    trial.add_argument("--arm", help="only this arm (default: every arm)")
    trial.add_argument("--case", dest="only", action="append", default=[],
                       help="only this case key, e.g. id@version; repeat for each")
    trial.add_argument("--all", action="store_true", help="run every slot that is still empty")
    trial.add_argument("--out", type=Path, help="results root (default: eval/results)")
    trial.add_argument("--db", help="evaluation database URL (default: CHAGENT_EVAL_DATABASE_URL)")
    trial.add_argument("--schema", default="chagent_eval", help="evaluation schema name")
    trial.add_argument("--json", action="store_true", help="emit each trial record as JSON")

    report = commands.add_parser("report", help="compare the arms of one experiment")
    report.add_argument("--protocol", type=Path, required=True)
    report.add_argument("--out", type=Path, help="results root (default: eval/results)")
    report.add_argument("--json", action="store_true")
    report.add_argument("--out-json", type=Path, help="write the whole report here")

    labels = commands.add_parser("labels", help="report per-label precision and recall")
    labels.add_argument("--diagnoses", type=Path, required=True,
                        help="file or directory of attribute documents")
    labels.add_argument("--annotations", type=Path, default=annotations.LEDGER)
    labels.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        return {"manifest": _manifest, "export": _export, "timeline": _timeline,
                "attribute": _attribute, "annotate": _annotate, "labels": _labels,
                "case": _case, "replay": _replay, "prereg": _prereg,
                "trial": _trial, "report": _report}[args.command](args)
    except (casefile.CaseInvalid, annotations.AnnotationInvalid) as error:
        print(f"invalid input: {error}", file=sys.stderr)
        return 1
    except (LookupError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _manifest(args) -> int:
    model = None
    if args.model:
        provider, _, model_id = args.model.partition("/")
        if not provider or not model_id:
            raise ValueError("--model must be provider/model")
        model = {"provider": provider, "model": model_id, "source": "argument"}
    manifest = VersionManifest.capture(args.repo, model=model, image_digest=args.image)
    if args.run:
        fingerprint = run(_attach(args.db, args.run, manifest))
        manifest = fingerprint
    _emit(manifest.to_json(), args.out)
    return 0


async def _attach(url, run_id, manifest: VersionManifest) -> VersionManifest:
    async with _database(url) as database:
        fingerprint = await EvalEvidence(database).request_fingerprint(run_id)
    if manifest.model is None and fingerprint.get("model"):
        manifest = replace(manifest, model=fingerprint["model"])
    return manifest.with_fingerprint(fingerprint)


def _export(args) -> int:
    document = run(_export_run(args.db, args.run_id, args.events, args.payload))
    _emit(document, args.out)
    return 0


async def _export_run(url, run_id, events, include_payload) -> dict:
    async with _database(url) as database:
        return await EvalEvidence(database).export_run(
            run_id, events=events, include_payload=include_payload)


def _timeline(args) -> int:
    document = run(_timeline_run(args.db, args.run_id))
    if args.out or args.json:
        _emit(document, args.out)
        return 0
    sys.stdout.write(format_timeline(document, limit=max(1, args.limit)))
    return 0


async def _timeline_run(url, run_id) -> dict:
    async with _database(url) as database:
        return await SpanProjection(database).project(run_id)


def _case(args) -> int:
    cases = []
    for path in args.paths:
        cases.extend(casefile.load_all(path) if path.is_dir() else [casefile.load(path)])
    for case in cases:
        print(casefile.describe(case))
    print(f"{len(cases)} case(s) valid")
    return 0


def _attribute(args) -> int:
    case = None
    if args.case:
        loaded = casefile.load(args.case)
        case = {**loaded.to_json(), "sha256": loaded.sha256}
    confirmations = _confirmations(args.annotations, sample_id(args.run_id))
    document = run(_diagnose(args.db, args.run_id, case, confirmations))
    if args.out or args.json:
        _emit(document, args.out)
        return 0
    sys.stdout.write(format_diagnosis(document, explain=args.explain))
    return 0


def _confirmations(path: Path, sample: str) -> list[dict]:
    """Pass all latest verdicts, including retractions and reviewer conflicts."""
    return [record.to_json() for record in annotations.latest(annotations.load(path))
            if record.sample_id == sample]


async def _diagnose(url, run_id, case, confirmations) -> dict:
    async with _database(url) as database:
        return await AttributeProjection(database).diagnose(
            run_id, case=case, confirmations=confirmations)


def _annotate(args) -> int:
    record = annotations.append({
        "sample_id": args.sample, "label": args.label, "verdict": args.verdict,
        "dataset": args.dataset, "reviewer": args.reviewer, "certainty": args.certainty,
        "evidence": args.evidence, "note": args.note, "run_id": args.run,
    }, args.file)
    print(f"{record.sample_id} {record.label} {record.verdict} "
          f"({record.dataset}, {record.reviewer} v{record.review_version})")
    return 0


def _labels(args) -> int:
    diagnoses = _load_diagnoses(args.diagnoses)
    report = annotations.evaluate(annotations.load(args.annotations), diagnoses)
    if args.json:
        _emit(report, None)
        return 0
    sys.stdout.write(annotations.format_report(report))
    return 0


def _load_diagnoses(path: Path) -> list[dict]:
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    documents = []
    for file in files:
        document = json.loads(file.read_text(encoding="utf-8"))
        if "findings" not in document or "sample_id" not in document:
            raise ValueError(f"{file} is not an attribution document")
        documents.append(document)
    if not documents:
        raise ValueError(f"no attribution documents in {path}")
    return documents


def _replay(args) -> int:
    if args.action == "pure":
        results = replay_pure.run(replay_pure.load_vectors(args.vectors or replay_pure.VECTORS_PATH))
        document = replay_pure.report(results)
        if args.out or args.json:
            _emit(document, args.out)
        else:
            sys.stdout.write(replay_pure.format_results(results))
        return 0 if document["passed"] == document["vectors"] else 3

    if args.action == "plan":
        if not args.run:
            raise ValueError("replay plan needs --run")
        case = replay_plan.load_case(args.case) if args.case else None
        document = run(_plan_run(args.db, args.run, case))
        if args.out or args.json:
            _emit(document, args.out)
        else:
            sys.stdout.write(replay_plan.format_plan(document))
        return 0

    if args.action == "state":
        if not args.scenario:
            raise ValueError("replay state needs --scenario")
        document = replay_sandbox.run_scenario(replay_sandbox.load_scenario(args.scenario))
        if args.out or args.json:
            _emit(document, args.out)
        else:
            sys.stdout.write(replay_sandbox.format_scenario(document))
        return 0 if document["ok"] else 3

    if args.action == "cassette-record":
        if not args.run:
            raise ValueError("replay cassette-record needs --run")
        if not args.out:
            raise ValueError("replay cassette-record needs --out")
        document = run(_record(args.db, args.run, args.out))
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
        return 0

    if not args.cassette or not args.run:
        raise ValueError("replay cassette-verify needs --cassette and --run")
    document = run(_verify(args.db, args.cassette, args.run))
    if args.out or args.json:
        _emit(document, args.out)
    else:
        sys.stdout.write(cassettes.format_verify(document))
    return 0 if document["ok"] else 3


async def _plan_run(url, run_id, case) -> dict:
    async with _database(url) as database:
        return await replay_plan.ReplayPlanning(database).plan(run_id, case=case)


async def _record(url, run_id, destination: Path) -> dict:
    async with _database(url) as database:
        source = cassettes.CassetteSource(database)
        rows = await source.rows(run_id)
        workspace = await source.workspace(run_id)
    recorded = cassettes.record(rows, workspace=workspace)
    path = recorded.save(destination)
    return {"path": str(path), "entries": len(recorded.entries),
            "warnings": list(recorded.warnings)}


async def _verify(url, path: Path, run_id) -> dict:
    recorded = cassettes.Cassette.load(path)
    async with _database(url) as database:
        requests = await cassettes.CassetteSource(database).requests(run_id)
    return await cassettes.verify(recorded, requests)


RESULTS = REPOSITORY / "eval" / "results"


def _protocol_dir(path: Path) -> Path:
    path = Path(path)
    return path if path.is_dir() else path.parent


def _prereg(args) -> int:
    from eval import prereg

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    cases = []
    for path in args.case:
        cases.extend(casefile.load_all(path) if path.is_dir() else [casefile.load(path)])
    arms = [prereg.Arm(**arm) for arm in spec.pop("arms")]
    protocol = prereg.build(cases=cases, arms=arms, **spec)
    directory = args.out or (RESULTS / protocol.experiment)
    frozen = prereg.freeze(protocol, directory)
    if args.json:
        _emit(protocol.to_json(), None)
    else:
        sys.stdout.write(prereg.describe(protocol))
        print(f"frozen at {frozen}")
    return 0


def _trial(args) -> int:
    from eval import prereg
    from eval.records import TrialStore

    directory = _protocol_dir(args.protocol)
    protocol = prereg.load(directory)
    root = args.out or RESULTS
    store = TrialStore(root)
    cases = {f"{case.id}@{case.version}": case
             for case in casefile.load_all(args.cases)}
    problems = [f"{key}: not loaded from {args.cases}"
                for key in (f"{entry['id']}@{entry['version']}" for entry in protocol.cases)
                if key not in cases]
    problems += prereg.check(protocol, list(cases.values()), list(protocol.arms))
    problems = [problem for problem in problems if "not preregistered" not in problem]
    if problems:
        raise ValueError("protocol does not describe this checkout: " + "; ".join(problems))

    env = _evaluation_env()
    trials = store.read(protocol.experiment, ignore=(prereg.PROTOCOL_FILE,))
    plans = _slots(protocol, cases, args, trials)
    if not plans:
        print("nothing to run: every selected slot already has its trial(s)")
        return 0
    run(_run_plans(plans, args, root, store, env, trials))
    return 0


def _slots(protocol, cases, args, trials) -> list:
    """Every slot the protocol promises that has no trial yet, in run order.

    Order is repeat-major when the protocol says ``interleaved``: arm A, arm B,
    next case, and only then the second repeat. Provider latency drifts across
    an hour, and running one arm entirely before the other would put that drift
    on the comparison instead of inside it.
    """
    from eval import prereg
    from eval.runner import TrialPlan

    declared = [f"{entry['id']}@{entry['version']}" for entry in protocol.cases]
    if args.only:
        unknown = sorted(set(args.only) - set(declared))
        if unknown:
            raise ValueError("not in the protocol: " + ", ".join(unknown))
        declared = [key for key in declared if key in args.only]
    arms = [arm for arm in protocol.arms if not args.arm or arm.name == args.arm]
    if args.arm and not arms:
        raise ValueError(f"no arm named {args.arm!r}")

    used = prereg.slots_used(protocol, trials)
    plans = []
    repeats = range(protocol.repeats)
    schedule = (((repeat, key, arm) for repeat in repeats for key in declared for arm in arms)
                if protocol.order == 'interleaved' else
                ((repeat, key, arm) for arm in arms for key in declared for repeat in repeats))
    for repeat, key, arm in schedule:
        if used.get((key, arm.name), 0) > repeat:
            continue
        plans.append(TrialPlan(protocol, cases[key], arm, repeat))
    return plans if args.all else plans[:1]


async def _run_plans(plans, args, root, store, env, trials) -> None:
    from eval import runner, prereg

    from eval.budgets import CostAccount, known_cost
    from eval.integrity import experiment_lock, execution, identity, publish
    from eval.manifests import VersionManifest
    from chagent.config import Settings

    protocol = plans[0].protocol
    if not protocol.pricing:
        raise ValueError("real experiments require preregistered pricing; no calls were made")
    directory = store.directory(protocol.experiment)
    # Hold the lock across the run. Durable claims survive crashes and cannot be
    # retried under the same denominator, even if no final record was written.
    with experiment_lock(directory):
        prereg.freeze(protocol, directory)
        existing = store.read(protocol.experiment, ignore=(prereg.PROTOCOL_FILE,))
        completed_ids = {trial.trial_id for trial in existing}
        pending_claims = [claim.stem for claim in (directory / '.claims').glob('*.claim')
                          if claim.stem not in completed_ids]
        if pending_claims:
            raise ValueError('unfinished trial claims have unknown spend; no further calls were made')
        from eval.report import _invalid_trials
        if _invalid_trials(protocol, existing):
            raise ValueError('existing trials have invalid bindings; no further calls were made')
        costs = [(trial.usage.get('cost') or {}).get('usd') for trial in existing]
        if any(not known_cost(cost) for cost in costs):
            raise ValueError("prior cost is unknown; experiment stopped without further calls")
        account = CostAccount(protocol.budget['total_cost_usd'], sum(costs))
        async with _eval_database(args.db, args.schema) as database:
            for position, plan in enumerate(plans, start=1):
                settings = Settings.from_env({**env, **runner.PINNED_ENV,
                                             'CHAGENT_MODEL': plan.arm.model,
                                             'CHAGENT_PROVIDER': plan.arm.provider})
                pinned = execution(identity(VersionManifest.capture(REPOSITORY).to_json(), settings))
                if any(record.manifest.get('execution', {}).get('identity_sha256') != pinned['identity_sha256']
                       for record in existing):
                    raise ValueError('execution version differs from existing trials; no call was made')
                publish(directory / 'execution.anchor',
                        {'protocol_sha256': protocol.sha256, **pinned}, identical_ok=True)
                if any(record.trial_id == plan.trial_id for record in existing):
                    raise ValueError(f"{plan.trial_id} already has a result; no rerun")
                publish(directory / '.claims' / (plan.trial_id + '.claim'),
                        {'protocol_sha256': protocol.sha256, 'case_sha256': plan.case.sha256,
                         'arm': plan.arm.name, 'repeat': plan.repeat})
                print(f"[{position}/{len(plans)}] {plan.trial_id}", flush=True)
                record = await runner.run_trial(plan, database=database, env=env,
                    cost_account=account, expected_identity=pinned['identity_sha256'])
                store.write(record)
                existing.append(record)
                print(runner.format_trial(record), flush=True)
                if account.unknown or account.spent >= account.cap:
                    print("stopped: cost is unknown or budget reached", file=sys.stderr)
                    return


class _eval_database:
    """A separate schema, so an experiment never looks like the user's own history.

    Trials insert sessions, runs, tool calls and approvals. Doing that in the
    product's schema would mix 24 synthetic runs into the history a person reads,
    and the two are cleaned up on different schedules. The schema is created and
    migrated once, then reused, because a trial's ``run_id`` must keep pointing
    at its records after the process exits.
    """

    def __init__(self, url: str | None, schema: str) -> None:
        self.url = url
        self.schema = schema

    async def __aenter__(self):
        from psycopg import AsyncConnection, sql
        from psycopg.conninfo import make_conninfo

        from chagent.config import Settings
        from chagent.storage.database import Database
        from chagent.storage.migrate import migrate

        base = self.url or Settings.from_env().database_url
        admin = await AsyncConnection.connect(base, autocommit=True)
        try:
            await admin.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(self.schema)))
        finally:
            await admin.close()
        settings = replace(Settings.from_env(),
                           database_url=make_conninfo(base, options=f"-c search_path={self.schema}"))
        database = Database(settings)
        await database.connect()
        self.database = database
        await migrate(database)
        return database

    async def __aexit__(self, *exception) -> None:
        await self.database.close()


def _report(args) -> int:
    from eval import prereg, report
    from eval.records import TrialStore

    directory = _protocol_dir(args.protocol)
    protocol = prereg.load(directory)
    trials = TrialStore(args.out or RESULTS).read(protocol.experiment,
                                                  ignore=(prereg.PROTOCOL_FILE,))
    document = report.build(protocol, trials)
    if args.out_json:
        _emit(document, args.out_json)
    if args.json:
        _emit(document, None)
    else:
        sys.stdout.write(report.format_report(document))
    return 0 if document["claim"] in {"improvement_supported", "no_difference_observed",
                                      "regression_detected"} else 3


def _evaluation_env() -> dict:
    """The environment a trial runs in: the file as a default layer, the shell on top."""
    from chagent.config import dotenv

    values = dotenv.read(REPOSITORY / ".env") if (REPOSITORY / ".env").exists() else {}
    return {**values, **os.environ}


def _emit(document: dict, destination: Path | None) -> None:
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if destination is None:
        sys.stdout.write(text)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    print(str(destination))


def run(coroutine):
    return asyncio.run(coroutine)


class _database:
    """Open a pool for one command and always close it."""

    def __init__(self, url: str | None) -> None:
        self.url = url

    async def __aenter__(self):
        from chagent.config import Settings
        from chagent.storage.database import Database

        settings = Settings.from_env()
        if self.url:
            settings = replace(settings, database_url=self.url)
        self.database = Database(settings)
        await self.database.connect()
        return self.database

    async def __aexit__(self, *exception) -> None:
        await self.database.close()


if __name__ == "__main__":
    raise SystemExit(main())
