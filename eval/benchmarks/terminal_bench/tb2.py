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
from chagent.models.factory import create_model_client
from eval.benchmarks.terminal_bench import live_policy
from eval.benchmarks.terminal_bench.harbor_agent import (
    ACTIVE_SLOT,
    LIMITS,
    MODEL,
    Slot,
    profile_identity,
)
from eval.cli import _eval_database
from eval.integrity import experiment_lock, publish
from eval.manifests import (
    VersionManifest,
    file_sha256,
    sha256_bytes,
    sha256_json,
    sha256_text,
)

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


def identity(task_path, slot_id, control=None, *, live=False):
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
    profile = profile_identity()
    protocol = None
    if live:
        if control is not None or task_path.name != "music-harmony":
            raise ValueError(
                "TB2.3 is one real music-harmony slot without synthetic controls"
            )
        if version_manifest.repo["dirty"] or not version_manifest.repo["commit"]:
            raise ValueError(
                "Freeze and run the paid slot from a committed, clean checkout"
            )
        protocol = live_policy.protocol()
        profile = profile_identity(
            live_policy.settings("unused-for-plan").model(),
            {
                **LIMITS,
                **{
                    name: protocol["budget"][name]
                    for name in ("wall_seconds", "max_turns", "max_tool_calls")
                },
            },
        )
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
        "execution_sources_sha256": {
            str(path.relative_to(REPO)): file_sha256(path)
            for path in (
                Path(__file__).resolve(),
                DIRECTORY / "harbor_agent.py",
                DIRECTORY / "tb22.py",
                DIRECTORY / "tb23.py",
                DIRECTORY / "live_policy.py",
                DIRECTORY / "uv.lock",
                REPO / "uv.lock",
            )
        },
        "chagent_dependencies": version_manifest.dependencies,
        "platform": pins["terminal_bench"]["prebuilt_platform"],
        "images": {
            "main": task.config.environment.docker_image,
            "verifier": task.config.verifier.environment.docker_image,
            "compose_services": service_images,
        },
        "profile": profile,
        "client": "live" if live else "synthetic",
        **(
            {"live": protocol}
            if live
            else {"fixture_command_sha256": sha256_text(FIXTURE_COMMAND)}
        ),
        **({"control": control} if control is not None else {}),
    }


def prepare(task_path, output, *, control=None, live=False):
    task_path, output = task_path.absolute(), output.resolve()
    result_root = REPO / "eval" / "results"
    if output.is_relative_to(task_path) or (
        output.is_relative_to(REPO) and not output.is_relative_to(result_root)
    ):
        raise ValueError(
            "Choose a new output outside the checkout or under eval/results"
        )
    slot_id = str(uuid4())
    original = identity(task_path, slot_id, control, live=live)
    output.mkdir(parents=True)
    frozen = output / task_path.name
    shutil.copytree(task_path, frozen, symlinks=True)
    if identity(frozen, slot_id, control, live=live) != original:
        raise ValueError("Diagnostic inputs changed during freeze")
    return publish(output / "plan.json", {**original, "sha256": sha256_json(original)})


def read_plan(root):
    """Validate the archived plan independently of the current checkout."""
    plan = read(root, "plan.json")
    digest = plan.pop("sha256", None)
    if plan.get("schema") != 1 or sha256_json(plan) != digest:
        raise ValueError("Frozen plan digest or schema differs")
    return {**plan, "sha256": digest}


def load_plan(root):
    plan = read_plan(root)
    if plan.get("task") not in PACKAGE_BYTES:
        raise ValueError("Plan does not name a supported diagnostic task")
    archived = {key: value for key, value in plan.items() if key != "sha256"}
    if (
        identity(
            root / plan["task"],
            plan["slot_id"],
            plan.get("control"),
            live=plan["client"] == "live",
        )
        != archived
    ):
        raise ValueError("Frozen inputs or execution identity changed")
    return plan


def claim_identity(plan):
    return {"slot_id": plan["slot_id"], "plan_sha256": plan["sha256"]}


def summarize_result(root, plan):
    root = root.resolve()
    harbor = read(root, "harbor-binding.json")
    trial_name = harbor["trial_name"]
    if (
        harbor["slot_id"] != plan["slot_id"]
        or Path(trial_name).name != trial_name
        or trial_name in {"", ".", ".."}
    ):
        raise ValueError("Archived Harbor binding differs from the plan")
    trial_id = harbor["harbor_trial_id"]
    relative = Path("harbor", "tb2", trial_name, "result.json")
    raw_path = (root / relative).resolve()
    if not raw_path.is_relative_to(root):
        raise ValueError("Harbor result path is outside the slot archive")
    raw_bytes = raw_path.read_bytes()
    raw = json.loads(raw_bytes)
    if raw["id"] != trial_id:
        raise ValueError("Harbor result has an unexpected trial ID")
    binding = (
        read(root, "run-binding.json") if (root / "run-binding.json").exists() else None
    )
    candidate = (
        read(root, "candidate.json") if (root / "candidate.json").exists() else None
    )
    if binding is not None and (
        binding["slot_id"] != plan["slot_id"] or binding["harbor_trial_id"] != trial_id
    ):
        raise ValueError("Chagent run binding differs from the archived slot or trial")
    if candidate is not None:
        execution = candidate["execution"]
        if execution["harbor_trial_id"] != trial_id or (
            binding is not None and execution["run_id"] != binding["run_id"]
        ):
            raise ValueError(
                "Candidate execution differs from the archived trial or run"
            )
    complete = (
        binding is not None
        and candidate is not None
        and raw["exception_info"] is None
        and raw["verifier_result"] is not None
        and candidate["end_reason"] == "model_stop"
        and not candidate["execution"]["command_outcome_unknown"]
    )
    return {
        **claim_identity(plan),
        "binding": binding,
        "harbor_trial_id": trial_id,
        "status": "observed" if complete else "incomplete",
        "raw_verifier_result": raw["verifier_result"],
        "exception": raw["exception_info"],
        "harbor_result": relative.as_posix(),
        "harbor_result_sha256": sha256_bytes(raw_bytes),
        "candidate": candidate,
        "cleanup": "unverified_tb2_1",
    }


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


async def execute(root, database, *, create_job=Job.create, client=None, api_key=None):
    root = root.resolve()
    if client is not None and not isinstance(client, FakeModelClient):
        raise ValueError("TB2.1 accepts only a synthetic model client")
    with experiment_lock(root):
        plan = load_plan(root)
        paid = plan["client"] == "live"
        if paid != (api_key is not None) or (paid and client is not None):
            raise ValueError(
                "Model dispatch must match the frozen live or synthetic plan"
            )
        provider = live_policy.settings(api_key) if paid else None
        if "control" in plan and (
            client is None
            or client.tool_name != "exec_command"
            or sha256_json(client.arguments) != plan["control"]["arguments_sha256"]
        ):
            raise ValueError("Scripted command does not match the frozen control")
        publish(
            root / "claim.json",
            claim_identity(plan),
        )
        if paid:
            model = provider.model()
            model_client = create_model_client(provider)
        else:
            model = MODEL
            model_client = client or FakeModelClient(
                "exec_command", {"cmd": FIXTURE_COMMAND}
            )
        slot = Slot(root, plan, database, model_client, model=model)
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
            evidence = summarize_result(root, plan)
            if evidence["harbor_trial_id"] != str(slot.trial_id):
                raise PermissionError("Archived result has an unexpected trial ID")
            return publish(root / "result.json", evidence)
        except BaseException as error:
            publish(
                root / "failure.json",
                {"slot_id": plan["slot_id"], "error_type": type(error).__name__},
            )
            raise
        finally:
            ACTIVE_SLOT.reset(token)
            if paid:
                await model_client.aclose()


def report(root):
    root = root.resolve()
    plan = read_plan(root)
    claimed = (root / "claim.json").exists()
    if claimed and read(root, "claim.json") != claim_identity(plan):
        raise ValueError("Archived claim differs from the frozen plan")
    if (root / "result.json").is_file():
        if not claimed:
            raise ValueError("Archived result is missing a slot claim")
        evidence = summarize_result(root, plan)
        if read(root, "result.json") != evidence:
            raise ValueError("Result summary differs from the archived evidence")
    else:
        evidence = {
            "slot_id": plan["slot_id"],
            "status": "incomplete" if claimed else "prepared",
        }
        if claimed and all(
            (root / name).is_file()
            for name in (
                "harbor-binding.json",
                "run-binding.json",
                "candidate.json",
            )
        ):
            # Candidate evidence may exist before the raw Harbor result is published.
            harbor = read(root, "harbor-binding.json")
            binding = read(root, "run-binding.json")
            candidate = read(root, "candidate.json")
            if (
                harbor["slot_id"] != plan["slot_id"]
                or binding["slot_id"] != plan["slot_id"]
                or binding["harbor_trial_id"] != harbor["harbor_trial_id"]
                or candidate["execution"]["harbor_trial_id"]
                != harbor["harbor_trial_id"]
                or candidate["execution"]["run_id"] != binding["run_id"]
            ):
                raise ValueError(
                    "Interrupted candidate binding differs from the archived slot"
                )
            evidence["end_reason"] = candidate["end_reason"]
            evidence["interruption"] = candidate["execution"].get("interruption")
    legacy = root.parent / f"{plan.get('control', {}).get('name')}-evidence.json"
    named_cleanup = (root / "cleanup-evidence.json").is_file()
    if named_cleanup or (
        plan.get("control", {}).get("name") == root.name and legacy.is_file()
    ):
        if not claimed:
            raise ValueError("Cleanup evidence is missing a slot claim")
        from eval.benchmarks.terminal_bench.tb22 import cleanup_evidence

        proven = cleanup_evidence(root, plan)
        if named_cleanup:
            saved = read(root, "cleanup-evidence.json")
        else:
            old = read(legacy.parent, legacy.name)
            saved = {
                "slot_id": old["slot_id"],
                "plan_sha256": old["plan_sha256"],
                "harbor_trial_id": old["trial"]["harbor_trial_id"],
                "events_sha256": old["events_sha256"],
                "resources": old["cleanup"],
            }
        if saved != proven:
            raise ValueError("Cleanup record differs from the archived Docker events")
        evidence["cleanup"] = "verified"
        evidence["cleanup_evidence"] = proven
    return evidence


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
