"""Operator command line for the evaluation contracts.

Seven commands, each answering one question:

``manifest``  which tree, prompts, tools, skills and model produced a result
``export``    what one run actually recorded, bounded and redacted
``timeline``  when each step ran, how well that is known, and what is missing
``attribute`` which diagnostic labels the records support, and what they do not
``annotate``  record a human verdict with its evidence and its dataset
``labels``    per-label precision/recall, or why it cannot be estimated
``case``      whether a case document still satisfies the published contract

The CLI is an operator tool, not a product entry point: it reads the database
with a read-only transaction and never runs an agent.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys

from eval import annotations, casefile
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

    labels = commands.add_parser("labels", help="report per-label precision and recall")
    labels.add_argument("--diagnoses", type=Path, required=True,
                        help="file or directory of attribute documents")
    labels.add_argument("--annotations", type=Path, default=annotations.LEDGER)
    labels.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        return {"manifest": _manifest, "export": _export, "timeline": _timeline,
                "attribute": _attribute, "annotate": _annotate, "labels": _labels,
                "case": _case}[args.command](args)
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
