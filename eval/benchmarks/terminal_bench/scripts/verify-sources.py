"""Compare the pinned Harbor checkout files with the installed wheel."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from importlib.metadata import distribution
import json
from pathlib import Path
from urllib.request import urlopen

PINS = Path(__file__).resolve().parents[1] / "pins.json"
FILES = (
    "agents/base.py",
    "agents/factory.py",
    "environments/base.py",
    "environments/docker/docker.py",
    "models/task/config.py",
    "models/task/task.py",
    "models/task/verifier_mode.py",
    "trial/network_policy.py",
    "trial/single_step.py",
    "trial/trial.py",
    "verifier/verifier.py",
)


def read_source(url, cached):
    if cached:
        return cached.read_bytes()
    with urlopen(url, timeout=90) as stream:
        return stream.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="Read existing harbor-source/ and release_prebuilt.py offline",
    )
    args = parser.parse_args()
    pins = json.loads(PINS.read_text())
    harbor = distribution("harbor")
    if harbor.version != pins["harbor"]["release"]:
        parser.error("Use the pinned Harbor environment")
    commit = pins["harbor"]["commit"]
    task_commit = pins["terminal_bench"]["commit"]
    platform = pins["terminal_bench"]["prebuilt_platform"]

    def verify(relative):
        url = f"https://raw.githubusercontent.com/harbor-framework/harbor/{commit}/src/harbor/{relative}"
        upstream = read_source(
            url,
            args.source_dir / "harbor-source" / relative if args.source_dir else None,
        )
        if upstream != harbor.locate_file(f"harbor/{relative}").read_bytes():
            raise ValueError(f"Wheel source differs from tag: {relative}")
        return relative, upstream

    with ThreadPoolExecutor(max_workers=8) as pool:
        sources = dict(pool.map(verify, FILES))
    url = f"https://raw.githubusercontent.com/harbor-framework/terminal-bench/{task_commit}/scripts/release-build/release_prebuilt.py"
    release = read_source(
        url, args.source_dir / "release_prebuilt.py" if args.source_dir else None
    )
    if f'PLATFORM = "{platform}"'.encode() not in release.splitlines():
        raise ValueError("Release platform differs from pins")
    result = {
        "harbor_commit": commit,
        "wheel_sources": {
            name: sha256(data).hexdigest() for name, data in sources.items()
        },
        "terminal_bench_commit": task_commit,
        "prebuilt_release_script_sha256": sha256(release).hexdigest(),
        "prebuilt_platform": platform,
    }
    if args.source_dir:
        expected = json.loads(PINS.with_name("source-audit.json").read_text())
        if result != expected:
            raise ValueError(
                "Offline sources differ from the saved pinned source audit"
            )
    args.output.mkdir(parents=True, exist_ok=True)
    for name, data in sources.items():
        path = args.output / "harbor-source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (args.output / "release_prebuilt.py").write_bytes(release)
    (args.output / "source-audit.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
