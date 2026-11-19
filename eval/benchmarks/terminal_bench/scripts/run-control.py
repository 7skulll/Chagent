"""Run one official control and capture Docker lifecycle evidence."""

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess


def resources(env, name=None):
    commands = {
        "containers": ("ps", "-aq", "--no-trunc"),
        "networks": ("network", "ls", "-q", "--no-trunc"),
        "volumes": ("volume", "ls", "-q"),
    }
    selector = ("--filter", f"name={name}") if name else ()
    return {
        kind: sorted(
            subprocess.check_output(
                ["docker", *args, *selector], env=env, text=True
            ).splitlines()
        )
        for kind, args in commands.items()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published-root", required=True, type=Path)
    parser.add_argument("--published-evidence", required=True, type=Path)
    parser.add_argument(
        "--task", required=True, choices=("music-harmony", "freight-dispatch-shift")
    )
    parser.add_argument("--agent", required=True, choices=("oracle", "nop"))
    parser.add_argument("--runs-dir", required=True, type=Path)
    parser.add_argument("--harbor", required=True, type=Path)
    args = parser.parse_args()
    task_path = args.published_root.resolve() / args.task
    pins = json.loads((Path(__file__).resolve().parents[1] / "pins.json").read_text())
    package = json.loads(args.published_evidence.read_text()).get(args.task, {})
    files = {
        path.relative_to(task_path).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in task_path.rglob("*")
        if path.is_file()
    }
    if (
        package.get("ref") != pins["published"]["task_refs"][args.task]
        or "task.toml" not in files
        or files != package.get("file_sha256")
    ):
        parser.error(f"Published package differs from verified input: {task_path}")
    name = f"{args.task}-{args.agent}"
    output = args.runs_dir.resolve() / name

    def write(filename, data):
        (output / filename).write_text(json.dumps(data, indent=2) + "\n")

    env = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    env.update(
        HARBOR_TELEMETRY="0",
        DOCKER_DEFAULT_PLATFORM="linux/amd64",
        PYTHONUNBUFFERED="1",
    )
    harbor = str(args.harbor.resolve())
    if (
        subprocess.check_output([harbor, "--version"], env=env, text=True).strip()
        != pins["harbor"]["release"]
    ):
        parser.error("Use the pinned Harbor executable")
    output.mkdir(parents=True)
    command = [
        harbor,
        "run",
        "--path",
        str(task_path),
        "--agent",
        args.agent,
        "--env",
        "docker",
        "--jobs-dir",
        str(output / "jobs"),
        "--job-name",
        name,
        "--n-concurrent",
        "1",
        "--n-attempts",
        "1",
        "--max-retries",
        "0",
        "--delete",
        "--no-stream",
    ]
    write(
        "command.json",
        {
            "argv": command,
            "platform": env["DOCKER_DEFAULT_PLATFORM"],
            "telemetry": False,
        },
    )
    before = resources(env)
    write("resources-before.json", before)
    start = datetime.now(UTC).isoformat()
    with (output / "harbor.log").open("w") as log:
        result = subprocess.run(
            command, cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    end = datetime.now(UTC).isoformat()
    after = resources(env)
    write("resources-after.json", after)
    with (output / "docker-events.ndjson").open("w") as events:
        subprocess.run(
            [
                "docker",
                "events",
                "--since",
                start,
                "--until",
                end,
                "--format",
                "{{json .}}",
            ],
            env=env,
            check=True,
            stdout=events,
        )
    run = {
        "start": start,
        "end": end,
        "harbor_exit_code": result.returncode,
        "new_resources": {
            key: sorted(set(after[key]) - set(before[key])) for key in after
        },
    }
    trial_results = list((output / "jobs" / name).glob("*/result.json"))
    if len(trial_results) == 1:
        trial_name = json.loads(trial_results[0].read_text())["trial_name"]
        run["trial_resources_after"] = resources(env, trial_name.lower() + "__")
    write("run.json", run)
    print(f"{name}: Harbor exit {result.returncode}, logs in {output}", flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
