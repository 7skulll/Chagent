"""Operator command line for the evaluation contracts.

Four commands, each answering one question:

``manifest``  which tree, prompts, tools, skills and model produced a result
``export``    what one run actually recorded, bounded and redacted
``timeline``  when each step ran, how well that is known, and what is missing
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

from eval import casefile
from eval.evidence import EvalEvidence
from eval.manifests import CaseInvalid, VersionManifest
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

    args = parser.parse_args(argv)
    try:
        return {"manifest": _manifest, "export": _export, "timeline": _timeline,
                "case": _case}[args.command](args)
    except CaseInvalid as error:
        print(f"invalid case: {error}", file=sys.stderr)
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
