"""Run the five fixed, zero-provider-call TB2.2 Docker controls."""

import argparse
import asyncio
from base64 import b64encode
from contextlib import suppress
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory
import shutil

from psycopg.rows import dict_row

from chagent.models.fake import FakeModelClient
from eval.benchmarks.terminal_bench import tb2
from eval.cli import _eval_database
from eval.integrity import publish
from eval.manifests import (
    file_sha256,
    prompt_material,
    sha256_bytes,
    sha256_json,
    sha256_text,
)

FIXTURE_HASH = "360aa0cecc1feef4e060f08bcd9d6d8b092847bde47d880d04fcefcf7bcdfdc1"
MARKER = "TB22_CANCEL"
CONTROL_NAMES = ("positive", "missing", "service", "timeout", "cancel")


class CleanupEvidenceMissing(ValueError):
    pass


def controls(artifact):
    data = artifact.read_bytes()
    if file_sha256(artifact) != FIXTURE_HASH:
        raise ValueError("Positive artifact differs from the TB1 oracle capture")
    encoded = shlex.quote(b64encode(data).decode("ascii"))
    service = """python3 -c 'import os, urllib.request; print(urllib.request.urlopen(os.environ["DISPATCH_EVENT_API_URL"] + "/health", timeout=5).status)'"""
    return {
        "positive": (
            "music-harmony",
            {"cmd": f"printf %s {encoded} | base64 -d > /app/harmony.mxl"},
        ),
        "missing": ("music-harmony", {"cmd": "pwd; test ! -e /app/harmony.mxl"}),
        "service": ("freight-dispatch-shift", {"cmd": service}),
        "timeout": (
            "music-harmony",
            {
                "cmd": "printf TB22_TIMEOUT; sleep 20; printf late > /app/harmony.mxl",
                "timeout_sec": 2,
            },
        ),
        "cancel": (
            "music-harmony",
            {
                "cmd": f"printf {MARKER}; sleep 20; printf late > /app/harmony.mxl",
            },
        ),
    }


async def docker(*args):
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE
    )
    try:
        output, _ = await process.communicate()
    except asyncio.CancelledError:
        with suppress(ProcessLookupError):
            process.terminate()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"Docker evidence command failed: {args[0]}")
    return output.decode()


def is_cancel_start(event, trial_name):
    return (
        event["Type"] == "container"
        and event["Action"].startswith("exec_start:")
        and MARKER in event["Action"]
        and event["Actor"]["Attributes"].get("name")
        == f"{trial_name.lower()}__env-main-1"
    )


async def cancel_after_start(root, started):
    process = await asyncio.create_subprocess_exec(
        "docker",
        "events",
        "--since",
        started,
        "--filter",
        "type=container",
        "--format",
        "{{json .}}",
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(60):
            while line := await process.stdout.readline():
                event = json.loads(line)
                if (root / "harbor-binding.json").is_file() and is_cancel_start(
                    event,
                    tb2.read(root, "harbor-binding.json")["trial_name"],
                ):
                    return event
        raise RuntimeError(
            "Docker event stream ended before the candidate command started"
        )
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        await process.wait()


def resources(events, trial_name, name):
    prefix = trial_name.lower() + "__"
    events = [
        e for e in events if e["Actor"]["Attributes"].get("name", "").startswith(prefix)
    ]
    created = {
        e["Actor"]["ID"]: e
        for e in events
        if e["Action"] == "create" and e["Type"] in {"container", "network", "volume"}
    }
    removed = {
        (e["Type"], e["Actor"]["ID"]): e["timeNano"]
        for e in events
        if e["Action"] in {"destroy", "remove"}
    }
    if not created or any(
        removed.get((event["Type"], identifier), -1) <= event["timeNano"]
        for identifier, event in created.items()
    ):
        raise CleanupEvidenceMissing(
            "Docker trial resource destruction evidence is missing"
        )
    containers = [e for e in created.values() if e["Type"] == "container"]
    interrupted = name in {"timeout", "cancel"}
    networks = 1 if interrupted else 2
    if name == "live-unscored":
        networks = (
            2
            if any(
                "__verifier__" in event["Actor"]["Attributes"]["name"]
                for event in containers
            )
            else 1
        )
    if (
        len(containers) != networks + (name == "service")
        or sum(e["Type"] == "network" for e in created.values()) != networks
    ):
        raise ValueError("Unexpected trial environment or verifier topology")
    result = {"destroyed": {id: event["Type"] for id, event in created.items()}}
    if interrupted:
        main = containers[0]["Actor"]["ID"]
        stopped = [
            event["timeNano"]
            for event in events
            if event["Actor"]["ID"] == main and event["Action"] == "stop"
        ]
        archives = [
            event["timeNano"]
            for event in events
            if event["Actor"]["ID"] == main and event["Action"] == "archive-path"
        ]
        marker = "TB22_TIMEOUT" if name == "timeout" else MARKER
        command_started = next(
            (
                event["timeNano"]
                for event in events
                if event["Actor"]["ID"] == main
                and event["Action"].startswith("exec_start:")
                and marker in event["Action"]
            ),
            None,
        )
        if (
            command_started is None
            or not stopped
            or (archives and min(archives) < min(stopped))
            or not (0 < min(stopped) - command_started < 20_000_000_000)
        ):
            raise ValueError("Main stop before Harbor collection was not established")
        result.update(main_stop_ns=min(stopped), command_started_ns=command_started)
    elif name == "service":
        healthy = next(
            event["timeNano"]
            for event in events
            if event["Action"] == "health_status: healthy"
            and event["Actor"]["Attributes"].get("com.docker.compose.service")
            == "event-feed"
        )
        main = next(
            event["timeNano"]
            for event in events
            if event["Action"] == "start"
            and event["Actor"]["Attributes"]["name"].endswith("__env-main-1")
        )
        if healthy >= main:
            raise ValueError(
                "Main service started before the official dependency was healthy"
            )
        result.update(event_feed_healthy_ns=healthy, main_started_ns=main)
    return result


async def capture_cleanup_events(root, started, trial_name, name, *, wait=10):
    path = root / "docker-events.ndjson"
    pending = None
    try:
        async with asyncio.timeout(wait):
            while True:
                finished = datetime.now(UTC).isoformat()
                data = await docker(
                    "events",
                    "--since",
                    started,
                    "--until",
                    finished,
                    "--format",
                    "{{json .}}",
                )
                path.write_text(data)
                try:
                    return resources(
                        [json.loads(line) for line in data.splitlines()],
                        trial_name,
                        name,
                    )
                except CleanupEvidenceMissing as error:
                    pending = error
                await asyncio.sleep(0.25)
    except TimeoutError:
        if pending is not None:
            raise pending from None
        raise CleanupEvidenceMissing(
            "Docker event capture exceeded its deadline"
        ) from None


def cleanup_evidence(root, plan):
    harbor = tb2.read(root, "harbor-binding.json")
    name = plan.get("control", {}).get("name")
    if plan["client"] == "live":
        recorded = tb2.summarize_result(root, plan)
        name = (
            "live" if recorded["raw_verifier_result"] is not None else "live-unscored"
        )
    elif name not in CONTROL_NAMES:
        raise ValueError("Cleanup evidence does not match a frozen control slot")
    if harbor["slot_id"] != plan["slot_id"]:
        raise ValueError("Cleanup evidence does not match a frozen control slot")
    path = root / "docker-events.ndjson"
    data = path.read_bytes()
    return {
        **tb2.claim_identity(plan),
        "harbor_trial_id": harbor["harbor_trial_id"],
        "events_sha256": sha256_bytes(data),
        "resources": resources(
            [json.loads(line) for line in data.splitlines()], harbor["trial_name"], name
        ),
    }


def validate_ledger(evidence, plan):
    tool = evidence["tool"]
    hashes = evidence["request_tool_sha256"]
    if not hashes or any(
        digest != plan["profile"]["tools_sha256"] for digest in hashes
    ):
        raise ValueError("Stored request has the wrong tool declaration")
    if (
        tool["name"] != "exec_command"
        or sha256_json(tool["arguments"]) != plan["control"]["arguments_sha256"]
    ):
        raise ValueError("Stored terminal arguments differ from the frozen control")


async def ledger(database, root, plan):
    binding = tb2.read(root, "run-binding.json")
    async with database.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                "SELECT status, end_reason FROM runs WHERE id=%s", (binding["run_id"],)
            )
            run = await cursor.fetchone()
            await cursor.execute(
                "SELECT name, status, arguments, result FROM tool_calls WHERE run_id=%s",
                (binding["run_id"],),
            )
            tools = await cursor.fetchall()
            await cursor.execute(
                "SELECT request FROM model_calls WHERE run_id=%s ORDER BY iteration",
                (binding["run_id"],),
            )
            models = await cursor.fetchall()
    hashes = [sha256_json(prompt_material(row["request"])[1]) for row in models]
    if len(tools) != 1:
        raise ValueError("Expected one terminal call")
    evidence = {"run": run, "tool": tools[0], "request_tool_sha256": hashes}
    validate_ledger(evidence, plan)
    publish(root / "db-evidence.json", evidence)
    return evidence


async def execute_control(root, task_path, name, args, db_url):
    source = (
        {"kind": "tb1-oracle-artifact", "artifact_sha256": FIXTURE_HASH}
        if name == "positive"
        else None
    )
    tb2.prepare(
        task_path,
        root,
        control={
            "name": name,
            "arguments_sha256": sha256_json(args),
            "fixture": source,
        },
    )
    plan = tb2.load_plan(root)
    schema = f"chagent_eval_tb2_{sha256_text(plan['slot_id'])[:16]}"
    started = datetime.now(UTC).isoformat()
    cancel_event = None
    async with _eval_database(db_url, schema) as database:
        trial = asyncio.create_task(
            tb2.execute(root, database, client=FakeModelClient("exec_command", args))
        )
        if name == "cancel":
            try:
                cancel_event = await cancel_after_start(root, started)
                trial.cancel()
                try:
                    await trial
                except asyncio.CancelledError:
                    pass
                else:
                    raise RuntimeError("Harbor job did not propagate cancellation")
            finally:
                if not trial.done():
                    trial.cancel()
                    await asyncio.gather(trial, return_exceptions=True)
        else:
            await trial
        database_evidence = await ledger(database, root, plan)
    harbor = tb2.read(root, "harbor-binding.json")
    await capture_cleanup_events(root, started, harbor["trial_name"], name)
    proof = cleanup_evidence(root, plan)
    candidate = tb2.read(root, "candidate.json")
    raw_path = root / "harbor" / "tb2" / harbor["trial_name"] / "result.json"
    raw = json.loads(raw_path.read_text())
    if raw["id"] != harbor["harbor_trial_id"]:
        raise ValueError("Unexpected trial identity in raw result")
    interrupted = name in {"timeout", "cancel"}
    cleanup = proof["resources"]
    reward = raw["verifier_result"]
    artifact = raw_path.parent / "artifacts/app/harmony.mxl"
    if interrupted:
        expected_reason = (
            "cancelled" if name == "cancel" else "timeout_or_backend_error"
        )
        if (
            reward is not None
            # An empty collection alone does not establish that a command stopped.
            # capture_cleanup_events already checked the command and stop timestamps.
            or artifact.exists()
            or raw["exception_info"] is None
            or candidate["execution"]["interruption"]
            != {"reason": expected_reason, "main_stop": "returned"}
        ):
            raise ValueError("Interrupted command did not fail closed")
        if name == "cancel" and database_evidence["run"]["status"] != "cancelled":
            raise ValueError("Cancelled Chagent run has the wrong terminal status")
    else:
        expected = 1.0 if name == "positive" else 0.0
        if (
            tb2.report(root)["status"] != "observed"
            or raw["exception_info"] is not None
            or reward["rewards"]["reward"] != expected
        ):
            raise ValueError("Official raw reward or outcome differs from the control")
        if name == "positive" and file_sha256(artifact) != FIXTURE_HASH:
            raise ValueError("Collected positive artifact differs from the fixture")
        if name == "missing" and artifact.exists():
            raise ValueError(
                "Missing-artifact control unexpectedly produced an artifact"
            )
        if name == "service" and (
            database_evidence["tool"]["result"]["details"]["exit_code"] != 0
            or "200" not in database_evidence["tool"]["result"]["content"][0]["text"]
        ):
            raise ValueError(
                "Declared service endpoint was not reached from the main environment"
            )
    if candidate["candidate_cost"]["usd"] != 0:
        raise ValueError("Expected zero synthetic cost")
    publish(root / "cleanup-evidence.json", proof)
    return {
        "slot_id": plan["slot_id"],
        "plan_sha256": plan["sha256"],
        "trial": harbor,
        "candidate": candidate,
        "db": {"schema": schema, "run": database_evidence["run"]},
        "reward": reward,
        "cleanup": cleanup,
        "raw_result_sha256": file_sha256(raw_path),
        "events_sha256": file_sha256(root / "docker-events.ndjson"),
        "db_evidence_sha256": file_sha256(root / "db-evidence.json"),
        **({"cancel_exec_start_ns": cancel_event["timeNano"]} if cancel_event else {}),
    }


async def run(args):
    suite = controls(args.artifact)
    args.out.mkdir(parents=True)
    evidence = {}
    for name, (task, command) in suite.items():
        result = await execute_control(
            args.out / name, args.published / task, name, command, args.db
        )
        evidence[name] = result
        publish(args.out / f"{name}-evidence.json", result)
        print(f"{name}: {result['reward']}, cleanup verified", flush=True)
    verify_missing_evidence(args.out / "positive")
    publish(
        args.out / "tb22-evidence.json",
        {"controls": evidence, "missing_evidence_rejected": True},
    )


def verify_missing_evidence(positive_slot):
    with TemporaryDirectory(prefix="chagent-tb22-evidence-") as scratch:
        target = Path(scratch) / "slot"
        shutil.copytree(positive_slot, target)
        raw = target / tb2.read(target, "result.json")["harbor_result"]
        raw.unlink()
        try:
            tb2.report(target)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("Report accepted missing raw result evidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--db", default=os.environ.get("CHAGENT_EVAL_DATABASE_URL"))
    args = parser.parse_args()
    args.out = args.out.resolve()
    os.environ["DOCKER_DEFAULT_PLATFORM"] = tb2.require_harbor()["terminal_bench"][
        "prebuilt_platform"
    ]
    os.environ["HARBOR_TELEMETRY"] = "0"
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
