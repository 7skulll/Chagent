"""Verify and parse the pinned official task source without executing task code."""

import argparse
from collections import defaultdict
import gzip
from hashlib import sha1, sha256
from importlib.metadata import version
import json
from pathlib import Path
import platform
import re

from harbor.models.task.task import Task
from harbor.models.task.verifier_mode import resolve_task_verifier_mode
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, VerifierConfig
from harbor.trial.network_policy import resolve_trial_network_plan
import yaml


PINS = json.loads(Path(__file__).with_name("pins.json").read_text())
SUITE = PINS["terminal_bench"]
COMPOSE_NAMES = ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")
PROVIDER_REFERENCE = re.compile(
    r"\b(?:import (?:openai|anthropic|litellm)|from (?:openai|anthropic|litellm)\b|"
    r"rk\.(?:llm_judge|llm_criterion)\b|(?:OPENAI|ANTHROPIC|GEMINI|DEEPSEEK)_API_KEY)\b"
)


def git_id(kind, content):
    return sha1(f"{kind} {len(content)}\0".encode() + content).hexdigest()


def verify_snapshot(root, tree_bytes):
    if tree_bytes.startswith(b"\x1f\x8b"):
        tree_bytes = gzip.decompress(tree_bytes)
    document = json.loads(tree_bytes)
    if document["truncated"] or document["sha"] != SUITE["tasks_tree"]:
        raise ValueError("Expected the complete pinned Git tasks tree")
    directories = {Path("."): document["sha"]}
    children = defaultdict(list)
    blobs = {}
    for entry in document["tree"]:
        path = Path(entry["path"])
        if path.is_absolute() or ".." in path.parts or path == Path("."):
            raise ValueError(f"Invalid tree path: {path}")
        children[path.parent].append((path.name, entry["mode"], entry["sha"]))
        if entry["type"] == "tree" and entry["mode"] == "040000":
            directories[path] = entry["sha"]
        elif entry["type"] == "blob" and entry["mode"] in {"100644", "100755"}:
            blobs[path] = entry
        else:
            raise ValueError(f"Unsupported tree entry: {path}")
    for directory, expected in directories.items():
        entries = sorted(children[directory], key=lambda row: row[0].encode() + (b"/" if row[1] == "040000" else b""))
        data = b"".join(
            f"{mode.lstrip('0')} {name}\0".encode() + bytes.fromhex(digest)
            for name, mode, digest in entries
        )
        if git_id("tree", data) != expected:
            raise ValueError(f"Git tree identity mismatch at {directory}")
    actual = {path.relative_to(root) for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    if actual != blobs.keys():
        raise ValueError(f"Snapshot file set differs: missing={len(blobs.keys() - actual)}, extra={len(actual - blobs.keys())}")
    hashes = defaultdict(list)
    for relative, entry in sorted(blobs.items()):
        path = root / relative
        if path.is_symlink() or bool(path.stat().st_mode & 0o111) != (entry["mode"] == "100755"):
            raise ValueError(f"Snapshot file type or mode differs: {relative}")
        data = path.read_bytes()
        if git_id("blob", data) != entry["sha"]:
            raise ValueError(f"Snapshot contents differ: {relative}")
        hashes[relative.parts[0]].append([relative.as_posix(), entry["mode"], sha256(data).hexdigest()])
    return directories, hashes


def load_images(path):
    data = path.read_bytes()
    if sha256(data).hexdigest() != SUITE["prebuilt_manifest_sha256"]:
        raise ValueError("Image manifest differs from the pinned release")
    manifest = json.loads(data)
    images = {row["task"]: row["images"] for row in manifest["tasks"]}
    if len(images) != SUITE["task_count"] or any(
        row["source_sha"] != SUITE["commit"] for row in manifest["tasks"]
    ):
        raise ValueError("Image manifest tasks differ from the pinned source")
    return images


def compose_services(directory):
    for name in COMPOSE_NAMES:
        path = directory / name
        if path.is_file():
            return (yaml.safe_load(path.read_text()) or {}).get("services", {})
    return {}


def verifier_fee_signals(path, verifier):
    signals = [f"verifier.env.{name}" for name in sorted(verifier.env) if "api_key" in name.lower()]
    for file in sorted((path / "tests").rglob("*")):
        if file.suffix in {".py", ".sh", ".js", ".ts"} and file.is_file():
            if PROVIDER_REFERENCE.search(file.read_text(errors="replace")):
                signals.append(file.relative_to(path).as_posix())
    return signals


def row(path, tree_id, files, official_images, registry_platforms):
    task = Task(path)
    config = task.config
    verifier = config.verifier
    mode = resolve_task_verifier_mode(config)
    network = resolve_trial_network_plan(
        config, AgentConfig(), EnvironmentConfig(), VerifierConfig(), None, verifier_mode=mode,
    )
    environment = config.environment
    services = compose_services(path / "environment")
    images = [{
        "role": image["role"],
        "pinned_ref": image["pinned_ref"],
        "registry_platforms": registry_platforms.get(image["digest"], {}).get("platforms"),
        "registry_error": registry_platforms.get(image["digest"], {}).get("error"),
    } for image in official_images]
    platform_status = "amd64_runtime_pending"
    if environment.gpus or environment.tpu or (verifier.environment and (
        verifier.environment.gpus or verifier.environment.tpu
    )):
        platform_status = "accelerator_required"
    elif environment.os.value != "linux":
        platform_status = "requires_" + environment.os.value
    return {
        "id": task.name,
        "version": config.task.version if config.task else None,
        "source_tree": tree_id,
        "source_sha256": sha256(json.dumps(files, separators=(",", ":")).encode()).hexdigest(),
        "files": len(files),
        "parsed": True,
        "schema": config.schema_version,
        "steps": len(config.steps or []),
        "category": config.metadata.get("category"),
        "tags": config.metadata.get("tags", []),
        "environment": {
            "os": environment.os.value,
            **environment.model_dump(mode="json", include={
                "cpus", "memory_mb", "storage_mb", "gpus", "gpu_types", "tpu",
                "skills_dir", "build_timeout_sec", "docker_image",
            }),
            "mcp_servers": len(environment.mcp_servers),
            "compose_services": sorted(services),
            "compose_external_images": {
                name: service["image"]
                for name, service in sorted(services.items()) if "image" in service
            },
            "agent_timeout_sec": config.agent.timeout_sec,
        },
        "verifier": {
            "mode": mode.value,
            "timeout_sec": verifier.timeout_sec,
            "compose_services": sorted(compose_services(path / "tests")),
            "dependency_files": [
                name for name in ("Dockerfile", "pyproject.toml", "uv.lock", "requirements.txt", "package.json")
                if (path / "tests" / name).is_file()
            ],
            "resources": verifier.environment.model_dump(mode="json", include={
                "cpus", "memory_mb", "storage_mb", "gpus", "tpu",
            }) if verifier.environment else None,
            "collect_services": [hook.service for hook in verifier.collect],
            "artifacts": len(config.artifacts),
            "potential_fee_signals": verifier_fee_signals(path, verifier),
        },
        "network": {phase: policy.model_dump(mode="json") if policy else None for phase, policy in (
            ("agent_environment", network.agent_env_baseline),
            ("agent_phase", network.agent_phase),
            ("verifier_environment", network.verifier_env_baseline),
            ("verifier_phase", network.verifier_phase),
        )},
        "official_images": images,
        "runtime_status": platform_status,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--git-tree", required=True, type=Path)
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--registry-evidence", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if version("harbor") != PINS["harbor"]["release"]:
        parser.error("Run this command in the locked Harbor environment")
    directories, hashes = verify_snapshot(args.tasks, args.git_tree.read_bytes())
    images = load_images(args.images)
    if {p.parent.name for p in args.tasks.glob("*/task.toml")} != images.keys():
        raise ValueError("Task directories and official image rows differ")
    registry = json.loads(args.registry_evidence.read_text()) if args.registry_evidence else {}
    rows = [
        row(args.tasks / name, directories[Path(name)], hashes[name], images[name], registry)
        for name in sorted(images)
    ]
    finding = {
        "pins": PINS,
        "host": {"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version()},
        "scope": "Static and Harbor parser preflight. Docker execution and verifier cost are unverified.",
        "verified_source_files": sum(map(len, hashes.values())),
        "tasks": rows,
    }
    args.output.write_text(json.dumps(finding, indent=2, ensure_ascii=False) + "\n")
    print(f"Parsed {len(rows)}/{SUITE['task_count']} official tasks with Harbor {version('harbor')}")


if __name__ == "__main__":
    main()
