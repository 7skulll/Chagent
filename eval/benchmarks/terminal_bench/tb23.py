"""Run one frozen, cost-limited live music-harmony diagnostic."""

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path

from psycopg.rows import dict_row

from chagent.config import dotenv
from eval.benchmarks.terminal_bench import tb2, tb22
from eval.cli import _eval_database
from eval.integrity import publish
from eval.manifests import file_sha256, prompt_material, sha256_json, sha256_text


def credential(env_file):
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key and env_file is not None:
        key = dotenv.read(env_file).get("DEEPSEEK_API_KEY")
    if not key or not key.strip():
        raise ValueError("TB2.3 requires DEEPSEEK_API_KEY")
    return key.strip()


async def docker_preflight(plan):
    await tb22.docker("info", "--format", "{{.ServerVersion}}")
    for image in (plan["images"]["main"], plan["images"]["verifier"]):
        details = json.loads(await tb22.docker("image", "inspect", image))[0]
        if details["Os"] + "/" + details["Architecture"] != plan["platform"]:
            raise ValueError("Local Docker image platform differs from the frozen plan")
        digest = image.split("@", 1)[1]
        if not any(ref.endswith("@" + digest) for ref in details["RepoDigests"]):
            raise ValueError("Local Docker image digest differs from the frozen plan")


async def ledger(database, binding, plan, candidate):
    async with database.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                "SELECT status, end_reason FROM runs WHERE id=%s", (binding["run_id"],)
            )
            run = await cursor.fetchone()
            await cursor.execute(
                "SELECT iteration, status, request, response FROM model_calls WHERE run_id=%s ORDER BY iteration",
                (binding["run_id"],),
            )
            models = await cursor.fetchall()
            await cursor.execute(
                "SELECT name, arguments, status, result FROM tool_calls WHERE run_id=%s ORDER BY created_at, call_index, id",
                (binding["run_id"],),
            )
            tools = await cursor.fetchall()
    calls = []
    for row in models:
        _, declarations = prompt_material(row["request"])
        digest = sha256_json(declarations)
        if digest != plan["profile"]["tools_sha256"]:
            raise ValueError(
                "Persisted tool declarations differ from the frozen profile"
            )
        calls.append(
            {
                "iteration": row["iteration"],
                "status": row["status"],
                "request_sha256": sha256_json(row["request"]),
                "tools_sha256": digest,
                "usage": (row["response"] or {}).get("usage"),
            }
        )
    if not calls or any(tool["name"] != "exec_command" for tool in tools):
        raise ValueError("Unexpected live request or tool trace")
    return {"run": run, "model_calls": calls, "tool_calls": tools, "budget": candidate}


async def execute(root, db_url, api_key):
    root = root.resolve()
    plan = tb2.load_plan(root)
    if plan["client"] != "live":
        raise ValueError("TB2.3 requires a frozen live plan")
    if (root / "claim.json").exists():
        raise ValueError("This paid slot has already been claimed")
    await docker_preflight(plan)
    started = datetime.now(UTC).isoformat()
    schema = f"chagent_eval_tb2_{sha256_text(plan['slot_id'])[:16]}"
    async with _eval_database(db_url, schema) as database:
        await tb2.execute(root, database, api_key=api_key)
        summary = tb2.report(root)
        if summary["binding"] is None or summary["candidate"] is None:
            raise ValueError("Harbor ended before a candidate run was recorded")
        data = await ledger(database, summary["binding"], plan, summary["candidate"])
        publish(root / "db-evidence.json", data)
    harbor = tb2.read(root, "harbor-binding.json")
    mode = "live" if summary["raw_verifier_result"] is not None else "live-unscored"
    await tb22.capture_cleanup_events(root, started, harbor["trial_name"], mode)
    proof = tb22.cleanup_evidence(root, plan)
    publish(root / "cleanup-evidence.json", proof)
    raw = root / summary["harbor_result"]
    artifact = raw.parent / "artifacts/app/harmony.mxl"
    outcome = {
        **tb2.claim_identity(plan),
        "binding": summary["binding"],
        "status": summary["status"],
        "official_verifier": summary["raw_verifier_result"],
        "harbor_exception": summary["exception"],
        "candidate": summary["candidate"],
        "cleanup": proof,
        "db_schema": schema,
        "db_evidence_sha256": file_sha256(root / "db-evidence.json"),
        "harbor_result_sha256": summary["harbor_result_sha256"],
        "artifact": {
            "path": str(artifact.relative_to(root)),
            "sha256": file_sha256(artifact),
        },
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    return publish(root / "tb23-evidence.json", outcome)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--task", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--env-file", type=Path)
    run.add_argument("--db")
    run.add_argument("--confirm-paid", action="store_true")
    commands.add_parser("report")
    for command in commands.choices.values():
        command.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        print(tb2.prepare(args.task, args.out, live=True))
    elif args.command == "report":
        print(json.dumps(tb2.report(args.out), indent=2))
    else:
        if not args.confirm_paid:
            parser.error("A paid run requires --confirm-paid")
        os.environ["HARBOR_TELEMETRY"] = "0"
        os.environ["DOCKER_DEFAULT_PLATFORM"] = tb2.require_harbor()["terminal_bench"][
            "prebuilt_platform"
        ]
        print(asyncio.run(execute(args.out, args.db, credential(args.env_file))))


if __name__ == "__main__":
    main()
