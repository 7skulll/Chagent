"""Freeze and execute a single synthetic Harbor terminal diagnostic."""

import argparse
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from harbor.job import Job
from harbor.models.job.config import JobConfig, RetryConfig
from harbor.models.task.task import Task
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig
from harbor.trial.hooks import TrialEvent
import yaml

from chagent.models.fake import FakeModelClient
from eval.benchmarks.terminal_bench.harbor_agent import (
    ACTIVE_SLOT,
    Slot,
    profile_identity,
)
from eval.cli import _eval_database
from eval.integrity import experiment_lock, publish
from eval.manifests import VersionManifest, file_sha256, sha256_json, sha256_text

DIRECTORY = Path(__file__).resolve().parent
REPO = DIRECTORY.parents[2]
AGENT = "eval.benchmarks.terminal_bench.harbor_agent:ChagentTerminalAgent"
# Whole-package byte inventories from the Hub-verified TB1 release.
PACKAGE_BYTES = {
    "music-harmony": "74cd32329c4111cd609a5fbd6859a428d0b58fdf60f0196fb336e2cba14195fe",
    "freight-dispatch-shift": "5860ed420ad1551b883637c14de903bc2f8599ee63d00ac77376b36e323c99b0",
}
FIXTURE_COMMAND = "pwd"


def read(root, name):
    return json.loads((root / name).read_text())


def require_harbor():
    pins = read(DIRECTORY, "pins.json")
    if version("harbor") != pins["harbor"]["release"]:
        raise ValueError("Run with the locked Harbor version")
    return pins


def package_identity(path):
    allowed = PACKAGE_BYTES.get(path.name)
    if allowed is None or path.is_symlink() or not path.is_dir():
        raise ValueError("Select a published TB1 diagnostic package")
    files = {}
    modes = {}
    for file in sorted(path.rglob("*")):
        if file.is_symlink() or (not file.is_file() and not file.is_dir()):
            raise ValueError(f"Invalid frozen task entry: {file.relative_to(path)}")
        if file.is_file():
            name = file.relative_to(path).as_posix()
            files[name] = file_sha256(file)
            modes[name] = file.stat().st_mode & 0o111
    if sha256_json(files) != allowed:
        raise ValueError("Package bytes differ from the pinned TB1 release")
    return sha256_json(modes)


def identity(task_path, slot_id):
    pins = require_harbor()
    modes = package_identity(task_path)
    task = Task(task_path)
    if task.name != f"terminal-bench/{task_path.name}" or task.has_steps:
        raise ValueError("Unexpected diagnostic task configuration")
    compose = task_path / "environment" / "docker-compose.yaml"
    service_images = {}
    if compose.is_file():
        service_images = {
            name: config["image"]
            for name, config in yaml.safe_load(compose.read_text())["services"].items()
        }
    version_manifest = VersionManifest.capture(REPO)
    return {
        "schema": 1,
        "slot_id": slot_id,
        "task": task_path.name,
        "dataset": pins["published"]["dataset_digest"],
        "package": pins["published"]["task_refs"][task_path.name],
        "package_inventory_sha256": PACKAGE_BYTES[task_path.name],
        "execute_bits_sha256": modes,
        "instruction_sha256": sha256_text(task.instruction),
        "harbor": pins["harbor"],
        "harbor_lock_sha256": file_sha256(DIRECTORY / "uv.lock"),
        "checkout": version_manifest.repo,
        "chagent_dependencies": version_manifest.dependencies,
        "platform": pins["terminal_bench"]["prebuilt_platform"],
        "images": {
            "main": task.config.environment.docker_image,
            "verifier": task.config.verifier.environment.docker_image,
            "compose_services": service_images,
        },
        "profile": profile_identity(),
        "fixture_command_sha256": sha256_text(FIXTURE_COMMAND),
        "client": "synthetic",
    }


def prepare(task_path, output):
    task_path, output = task_path.absolute(), output.resolve()
    result_root = REPO / "eval" / "results"
    if output.is_relative_to(task_path) or (
        output.is_relative_to(REPO) and not output.is_relative_to(result_root)
    ):
        raise ValueError(
            "Choose a new output outside the checkout or under eval/results"
        )
    slot_id = str(uuid4())
    original = identity(task_path, slot_id)
    output.mkdir(parents=True)
    frozen = output / task_path.name
    shutil.copytree(task_path, frozen, symlinks=True)
    if identity(frozen, slot_id) != original:
        raise ValueError("Diagnostic inputs changed during freeze")
    return publish(output / "plan.json", {**original, "sha256": sha256_json(original)})


def load_plan(root):
    plan = read(root, "plan.json")
    if plan.get("task") not in PACKAGE_BYTES:
        raise ValueError("Plan does not name a supported diagnostic task")
    digest = plan.pop("sha256")
    if (
        sha256_json(plan) != digest
        or identity(root / plan["task"], plan["slot_id"]) != plan
    ):
        raise ValueError("Frozen plan, inputs or execution identity changed")
    return {**plan, "sha256": digest}


def job_config(root, plan):
    return JobConfig(
        job_name="tb2",
        jobs_dir=root / "harbor",
        quiet=True,
        tasks=[TaskConfig(path=root / plan["task"])],
        agents=[AgentConfig(import_path=AGENT)],
        environment=EnvironmentConfig(type="docker", delete=True, stream=False),
        n_attempts=1,
        n_concurrent_trials=1,
        retry=RetryConfig(max_retries=0),
    )


async def execute(root, database, *, create_job=Job.create, client=None):
    root = root.resolve()
    if client is not None and not isinstance(client, FakeModelClient):
        raise ValueError("TB2.1 accepts only a synthetic model client")
    with experiment_lock(root):
        plan = load_plan(root)
        publish(
            root / "claim.json",
            {"slot_id": plan["slot_id"], "plan_sha256": plan["sha256"]},
        )
        synthetic = (
            client
            if client is not None
            else FakeModelClient("exec_command", {"cmd": FIXTURE_COMMAND})
        )
        slot = Slot(root, plan, database, synthetic)
        token = ACTIVE_SLOT.set(slot)
        end_event = None

        async def started(event):
            if load_plan(root) != plan:
                raise ValueError("Plan changed before Harbor started")
            await slot.on_start(event)

        async def ended(event):
            nonlocal end_event
            if event.trial_id != slot.trial_id or end_event is not None:
                raise PermissionError("Harbor terminal event does not match the slot")
            end_event = event

        try:
            job = await create_job(job_config(root, plan))
            if len(job) != 1:
                raise ValueError("Expected exactly one Harbor trial")
            job.add_hook(TrialEvent.START, started)
            job.add_hook(TrialEvent.END, ended)
            await job.run()
            if end_event is None:
                raise RuntimeError("Missing Harbor terminal evidence")
            if load_plan(root) != plan:
                raise ValueError("Plan changed while Harbor was executing")
            raw_path = slot.agent_dir.parent / "result.json"
            raw = json.loads(raw_path.read_text())
            if raw["id"] != str(slot.trial_id):
                raise PermissionError("Harbor result has an unexpected trial ID")
            binding_path = root / "run-binding.json"
            binding = read(root, binding_path.name) if binding_path.exists() else None
            candidate_path = root / "candidate.json"
            candidate = (
                read(root, candidate_path.name) if candidate_path.exists() else None
            )
            complete = (
                binding is not None
                and candidate is not None
                and raw["exception_info"] is None
                and raw["verifier_result"] is not None
                and candidate["end_reason"] == "model_stop"
                and not candidate["execution"]["command_outcome_unknown"]
            )
            return publish(
                root / "result.json",
                {
                    "slot_id": plan["slot_id"],
                    "plan_sha256": plan["sha256"],
                    "binding": binding,
                    "harbor_trial_id": str(slot.trial_id),
                    "status": "observed" if complete else "incomplete",
                    "raw_verifier_result": raw["verifier_result"],
                    "exception": raw["exception_info"],
                    "harbor_result": str(raw_path.relative_to(root)),
                    "harbor_result_sha256": file_sha256(raw_path),
                    "candidate": candidate,
                    "cleanup": "unverified_tb2_1",
                },
            )
        except BaseException as error:
            publish(
                root / "failure.json",
                {"slot_id": plan["slot_id"], "error_type": type(error).__name__},
            )
            raise
        finally:
            ACTIVE_SLOT.reset(token)


def report(root):
    plan = read(root, "plan.json")
    if (root / "result.json").is_file():
        result = read(root, "result.json")
        result_path = (root / result["harbor_result"]).resolve()
        if (
            result["plan_sha256"] != plan["sha256"]
            or not result_path.is_relative_to(root.resolve())
            or file_sha256(result_path) != result["harbor_result_sha256"]
        ):
            raise ValueError("Result evidence differs from the recorded binding")
        return result
    status = "incomplete" if (root / "claim.json").exists() else "prepared"
    return {"slot_id": plan["slot_id"], "status": status}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("prepare")
    freeze.add_argument("--task", type=Path, required=True)
    commands.add_parser("run")
    commands.add_parser("report")
    for command in ("prepare", "run", "report"):
        commands.choices[command].add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args.task, args.out))
    elif args.command == "report":
        print(json.dumps(report(args.out), indent=2))
    else:
        # The Harbor environment gets its official Docker architecture and no model overrides.
        os.environ["DOCKER_DEFAULT_PLATFORM"] = require_harbor()["terminal_bench"][
            "prebuilt_platform"
        ]
        os.environ["HARBOR_TELEMETRY"] = "0"

        async def run():
            plan = load_plan(args.out.resolve())
            schema = f"chagent_eval_tb2_{sha256_text(plan['slot_id'])[:16]}"
            async with _eval_database(
                os.environ.get("CHAGENT_EVAL_DATABASE_URL"), schema
            ) as database:
                print(await execute(args.out, database))

        asyncio.run(run())


if __name__ == "__main__":
    main()
