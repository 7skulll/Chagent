"""Recompute the TB2.2 diagnostic summary offline from archived slot evidence."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re

from eval.benchmarks.terminal_bench import tb2, tb22
from eval.integrity import publish
from eval.manifests import file_sha256, sha256_json


def audit(root):
    archived = tb2.read(root, "tb22-evidence.json")
    if (
        set(archived["controls"]) != set(tb22.CONTROL_NAMES)
        or not archived["missing_evidence_rejected"]
    ):
        raise ValueError(
            "Expected five controls and the recorded missing-evidence assertion"
        )
    slots = {}
    seen = {
        key: set()
        for key in ("slot_id", "harbor_trial_id", "session_id", "run_id", "resource_id")
    }
    resource_counts = Counter({"container": 0, "network": 0, "volume": 0})
    sources = tb2.read_plan(root / "positive").get("execution_sources_sha256")
    for name in tb22.CONTROL_NAMES:
        slot = root / name
        control = archived["controls"][name]
        plan = tb2.read_plan(slot)
        report = tb2.report(slot)
        if (
            plan["control"]["name"] != name
            or plan.get("execution_sources_sha256") != sources
        ):
            raise ValueError(f"Control identity differs: {name}")
        evidence = tb22.cleanup_evidence(slot, plan)
        if (
            evidence["resources"] != control["cleanup"]
            or evidence["events_sha256"] != control["events_sha256"]
        ):
            raise ValueError(f"Docker cleanup differs: {name}")
        if report["cleanup"] != "verified":
            raise ValueError(f"No verified archived cleanup: {name}")
        binding = tb2.read(slot, "run-binding.json")
        for key in ("slot_id", "harbor_trial_id", "session_id", "run_id"):
            if binding[key] in seen[key]:
                raise ValueError(f"Duplicate {key}: {name}")
            seen[key].add(binding[key])
        for identifier, kind in evidence["resources"]["destroyed"].items():
            if identifier in seen["resource_id"]:
                raise ValueError(f"Duplicate Docker resource: {name}")
            seen["resource_id"].add(identifier)
            resource_counts[kind] += 1
        rebuilt = tb2.summarize_result(slot, plan)
        if (
            rebuilt["binding"] != binding
            or rebuilt["harbor_trial_id"] != evidence["harbor_trial_id"]
        ):
            raise ValueError(f"Harbor and Chagent bindings differ: {name}")
        database = tb2.read(slot, "db-evidence.json")
        tb22.validate_ledger(database, plan)
        if (
            rebuilt["harbor_result_sha256"] != control["raw_result_sha256"]
            or file_sha256(slot / "db-evidence.json") != control["db_evidence_sha256"]
            or rebuilt["raw_verifier_result"] != control["reward"]
            or database["run"] != control["db"]["run"]
            or rebuilt["candidate"] != control["candidate"]
            or tb2.claim_identity(plan)
            != {key: control[key] for key in ("slot_id", "plan_sha256")}
        ):
            raise ValueError(f"Recorded slot evidence differs: {name}")
        candidate = rebuilt["candidate"]
        if plan["client"] != "synthetic" or candidate["candidate_cost"]["usd"] != 0:
            raise ValueError(f"Slot does not have synthetic zero-cost evidence: {name}")
        exception = rebuilt["exception"]
        proof = evidence["resources"]
        slots[name] = {
            **control,
            "binding": binding,
            "profile_sha256": plan["profile"]["tools_sha256"],
            "harbor_exception_type": exception["exception_type"] if exception else None,
        }
        if "main_stop_ns" in proof:
            slots[name]["seconds_command_start_to_main_stop"] = round(
                (proof["main_stop_ns"] - proof["command_started_ns"]) / 1e9, 3
            )
    tb22.verify_missing_evidence(root / "positive")
    return {
        "controls": slots,
        "missing_evidence_rejected": archived["missing_evidence_rejected"],
        "paid_candidate_model_calls": 0,
        "local_artifacts": str(root),
        "result_sha256": file_sha256(root / "tb22-evidence.json"),
        "unique_destroyed_resources": dict(resource_counts),
        "source_sha256": sources,
    }


def audit_smoke(root):
    slot = root / "smoke"
    plan = tb2.read_plan(slot)
    result = tb2.report(slot)
    candidate = result["candidate"]
    harbor = tb2.read(slot, "harbor-binding.json")
    database = tb2.read(root, "db-evidence.json")
    events = root / "docker-events.ndjson"
    resources = tb22.resources(
        [json.loads(line) for line in events.read_text().splitlines()],
        harbor["trial_name"],
        "missing",
    )
    tool = database["tool_calls"][0]
    declarations = database["model_requests"]
    if (
        result["status"] != "observed"
        or plan["client"] != "synthetic"
        or candidate["candidate_cost"]["usd"] != 0
        or result["raw_verifier_result"] != {"rewards": {"reward": 0.0}}
        or database["run_id"] != result["binding"]["run_id"]
        or len(database["tool_calls"]) != 1
        or tool["name"] != "exec_command"
        or tool["arguments"] != {"cmd": "pwd"}
        or tool["status"] != "completed"
        or tool["result"]["details"]["exit_code"] != 0
        or "/app" not in tool["result"]["content"][0]["text"]
        or len(declarations) != 2
        or any(
            sha256_json(record["declarations"]) != plan["profile"]["tools_sha256"]
            for record in declarations
        )
    ):
        raise ValueError("Smoke result or ledger differs from its frozen contract")
    log = (root / "tests.log").read_text().strip()
    test_count = re.search(r"Ran (\d+) tests?", log)
    if test_count is None or not log.endswith("OK"):
        raise ValueError("Smoke test log does not record a passing suite")
    raw_path = slot / result["harbor_result"]
    reward = raw_path.parent / "verifier/reward.json"
    if json.loads(reward.read_text())["reward"] != 0.0:
        raise ValueError("Unexpected raw verifier reward")
    paths = [
        "prepare.log",
        "run.log",
        "report.log",
        "run-meta.json",
        "tests.log",
        "db-evidence.json",
        "docker-events.ndjson",
        "resources-before.json",
        "resources-after.json",
        "smoke/plan.json",
        "smoke/claim.json",
        "smoke/harbor-binding.json",
        "smoke/run-binding.json",
        "smoke/candidate.json",
        "smoke/result.json",
        str(raw_path.relative_to(root)),
        str(reward.relative_to(root)),
        str((raw_path.parent / "artifacts/manifest.json").relative_to(root)),
    ]
    return {
        "slot_id": plan["slot_id"],
        "plan_sha256": plan["sha256"],
        "binding": result["binding"],
        "official_reward": 0.0,
        "paid_candidate_model_calls": 0,
        "cleanup": resources,
        "passing_tests": int(test_count[1]),
        "local_artifacts": str(root),
        "sha256": {name: file_sha256(root / name) for name in paths},
        "source_sha256": plan.get("execution_sources_sha256"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Audit the TB2.1 single-slot review archive",
    )
    args = parser.parse_args()
    output = audit_smoke(args.archive) if args.smoke else audit(args.archive)
    publish(args.output, output)


if __name__ == "__main__":
    main()
