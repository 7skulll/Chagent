"""Recompute TB1 evidence from the original local control logs."""

import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text())


def sha(path):
    return sha256(path.read_bytes()).hexdigest()


def duration(start, finish):
    return round(
        (
            datetime.fromisoformat(finish) - datetime.fromisoformat(start)
        ).total_seconds(),
        3,
    )


def require(condition, context, check):
    if not condition:
        raise ValueError(f"{context}: {check}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.results
    published = load(root / "published-evidence.json")
    registry = load(root / "published-registry-check.json")
    dataset = load(root / "dataset-metadata.json")
    pins = load(Path(__file__).resolve().parents[1] / "pins.json")
    pinned = pins["published"]
    require(
        dataset["ref"] == pinned["dataset_ref"]
        and dataset["version"] == pinned["dataset_digest"],
        "Registry file verification",
        "Dataset identity differs from the pinned release",
    )
    require(
        {name: entry["ref"] for name, entry in registry.items()} == pinned["task_refs"]
        and {name: entry["ref"] for name, entry in published.items()}
        == pinned["task_refs"],
        "Registry file verification",
        "Published package metadata differs from the pinned release",
    )
    records = []
    all_containers, all_networks = set(), set()
    for task, artifact_path in [
        ("music-harmony", "artifacts/app/harmony.mxl"),
        ("freight-dispatch-shift", "artifacts/workspace/dispatch"),
    ]:
        task_checksum = None
        for arm, expected_reward in [("oracle", 1.0), ("nop", 0.0)]:
            name = f"{task}-{arm}"
            path = root / "runs" / name
            meta = load(path / "run.json")
            require(meta["harbor_exit_code"] == 0, name, "Harbor exited with an error")
            if "trial_resources_after" in meta:
                remaining = meta["trial_resources_after"]
                require(
                    remaining == {"containers": [], "networks": [], "volumes": []},
                    name,
                    "Trial-scoped Docker inventory is incomplete or contains leftovers",
                )
            else:
                require(
                    not any(meta["new_resources"].values())
                    and load(path / "resources-before.json")
                    == load(path / "resources-after.json"),
                    name,
                    "Legacy control has different Docker resources before and after the trial",
                )
            job = path / "jobs" / name
            trial_results = list(job.glob("*/result.json"))
            require(len(trial_results) == 1, name, "Expected exactly one trial result")
            trial = trial_results[0].parent
            result = load(trial / "result.json")
            require(
                result["exception_info"] is None
                and result["verifier_environment_mode"] == "separate",
                name,
                "Trial has an exception or did not use a separate verifier",
            )
            require(
                result["agent_info"]["name"] == arm,
                name,
                "Agent does not match the control arm",
            )
            require(
                not result["agent_result"]["model_usage"],
                name,
                "Unexpected model usage in control arm",
            )
            require(
                result["verifier_result"]["rewards"]["reward"] == expected_reward,
                name,
                "Trial reward does not match the expected control reward",
            )
            require(
                load(trial / "verifier/reward.json")["reward"] == expected_reward,
                name,
                "Raw verifier reward does not match the expected control reward",
            )
            require(
                task_checksum in (None, result["task_checksum"]),
                name,
                "Task checksum differs between the two control arms",
            )
            task_checksum = result["task_checksum"]
            declared = next(
                row
                for row in load(trial / "artifacts/manifest.json")
                if row["destination"] == artifact_path
            )
            artifact = trial / artifact_path
            require(
                artifact.is_file() == (arm == "oracle"),
                name,
                "Artifact presence does not match the control arm",
            )
            require(
                declared["status"] == ("ok" if arm == "oracle" else "failed"),
                name,
                "Artifact manifest status does not match the control arm",
            )
            artifact_evidence = {
                "path": artifact_path,
                "collection_status": declared["status"],
            }
            if artifact.is_file():
                artifact_evidence.update(
                    sha256=sha(artifact), size_bytes=artifact.stat().st_size
                )

            events_path = path / "docker-events.ndjson"
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            prefix = result["trial_name"].lower() + "__"

            def attrs(event):
                return event["Actor"]["Attributes"]

            def project(event):
                return attrs(event).get("com.docker.compose.project", "")

            creations = [
                e
                for e in events
                if e["Type"] == "container"
                and e["Action"] == "create"
                and project(e).startswith(prefix)
            ]
            destructions = {
                e["Actor"]["ID"]: e["timeNano"]
                for e in events
                if e["Type"] == "container" and e["Action"] == "destroy"
            }
            expected_images = set(published[task]["images"])
            require(
                len(creations) == len(expected_images)
                and {attrs(e)["image"] for e in creations} == expected_images,
                name,
                "Docker create events do not match the published image set",
            )
            created_ids = {e["Actor"]["ID"] for e in creations}
            require(
                created_ids <= destructions.keys(),
                name,
                "Missing Docker container destroy event",
            )
            teardown_times = [
                destructions[e["Actor"]["ID"]]
                for e in creations
                if project(e).endswith("__env")
            ]
            verifier_creation = next(
                e for e in creations if "__verifier__" in project(e)
            )
            require(
                max(teardown_times) < verifier_creation["timeNano"],
                name,
                "Verifier was created before agent environment teardown",
            )
            containers = []
            for event in creations:
                identifier = event["Actor"]["ID"]
                require(
                    identifier not in all_containers,
                    name,
                    "Container ID was reused across controls",
                )
                all_containers.add(identifier)
                containers.append(
                    {
                        "id": identifier,
                        "project": project(event),
                        "service": attrs(event)["com.docker.compose.service"],
                        "image": attrs(event)["image"],
                        "created_ns": event["timeNano"],
                        "destroyed_ns": destructions[identifier],
                    }
                )
            networks = [
                e
                for e in events
                if e["Type"] == "network"
                and e["Action"] == "create"
                and attrs(e).get("name", "").startswith(prefix)
            ]
            destroyed_networks = {
                e["Actor"]["ID"]
                for e in events
                if e["Type"] == "network" and e["Action"] == "destroy"
            }
            require(
                len(networks) == 2
                and {e["Actor"]["ID"] for e in networks} <= destroyed_networks,
                name,
                "Expected two trial networks with matching destroy events",
            )
            ids = {e["Actor"]["ID"] for e in networks}
            require(
                not all_networks & ids, name, "Network ID was reused across controls"
            )
            all_networks.update(ids)
            healthy = None
            if task == "freight-dispatch-shift":
                healthy = next(
                    e["timeNano"]
                    for e in events
                    if e["Type"] == "container"
                    and e["Action"] == "health_status: healthy"
                    and project(e) == prefix + "env"
                    and attrs(e).get("com.docker.compose.service") == "event-feed"
                )
                start_main = next(
                    e["timeNano"]
                    for e in events
                    if e["Type"] == "container"
                    and e["Action"] == "start"
                    and project(e) == prefix + "env"
                    and attrs(e).get("com.docker.compose.service") == "main"
                )
                require(
                    healthy < start_main,
                    name,
                    "The main service started before event-feed was healthy",
                )
                healthy = {
                    "event_feed_healthy_ns": healthy,
                    "main_start_ns": start_main,
                }
            evidence_files = {
                label: {"path": str(file.relative_to(root)), "sha256": sha(file)}
                for label, file in {
                    "outputs": trial / "result.json",
                    "raw_reward": trial / "verifier/reward.json",
                    "artifact_manifest": trial / "artifacts/manifest.json",
                    "docker_events": events_path,
                }.items()
            }
            records.append(
                {
                    "task": task,
                    "agent": arm,
                    "trial_id": result["id"],
                    "trial_name": result["trial_name"],
                    "task_checksum": task_checksum,
                    "reward": result["verifier_result"]["rewards"],
                    "exception": result["exception_info"],
                    "verifier_environment_mode": "separate",
                    "trial_seconds": duration(
                        result["started_at"], result["finished_at"]
                    ),
                    "artifact": artifact_evidence,
                    "docker_containers": containers,
                    "docker_network_ids": sorted(ids),
                    "healthy_before_main": healthy,
                    "cleanup_verified": True,
                    "evidence": evidence_files,
                }
            )
            print(
                f"{name}: reward={expected_reward}, {records[-1]['trial_seconds']} s, cleanup confirmed"
            )

    require(
        all(
            (
                not item["differs"]
                and (not item["local_paths_without_registry_rows"])
                and (item["registered_files"] == item["matching"])
                for item in registry.values()
            )
        ),
        "Registry file verification",
        "Published package files did not match the Hub records",
    )
    output = {
        "date": args.date.isoformat(),
        "status": "accepted",
        "harbor": pins["harbor"]["release"],
        "dataset": {"ref": dataset["ref"], "content_digest": dataset["version"]},
        "published_tasks": registry,
        "docker_engine": load(root / "docker-engine.json"),
        "docker_compose": (root / "compose-version.txt").read_text().strip(),
        "amd64_smoke": (root / "amd64-smoke.txt").read_text().strip(),
        "controls": records,
        "distinct_containers": len(all_containers),
        "distinct_networks": len(all_networks),
        "candidate_model_calls": 0,
        "local_artifact_directory": str(args.results),
    }
    args.output.write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()
