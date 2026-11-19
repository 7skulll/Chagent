"""Resolve, download and verify the two official TB1 task packages."""

import argparse
import asyncio
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path

from harbor.db.client import RegistryDB
from harbor.models.task.task import Task
from harbor.registry.client.package import PackageDatasetClient
import yaml

PINS = Path(__file__).resolve().parents[1] / "pins.json"


def inspect_images(path, config):
    refs = {config.environment.docker_image, config.verifier.environment.docker_image}
    compose = path / "environment" / "docker-compose.yaml"
    if compose.is_file():
        refs.update(
            service["image"]
            for service in yaml.safe_load(compose.read_text())["services"].values()
        )
    return sorted(refs)


async def prepare(root, manifest, pins):
    client = PackageDatasetClient()
    release = pins["published"]
    metadata = await client.get_dataset_metadata(release["dataset_ref"])
    if (
        metadata.version != release["dataset_digest"]
        or len(metadata.task_ids) != pins["terminal_bench"]["task_count"]
    ):
        raise ValueError("Dataset metadata differs from the pinned release")
    selected_names = release["task_refs"].keys()
    tasks = [
        task
        for task in metadata.task_ids
        if getattr(task, "name", None) in selected_names
    ]
    if {task.name: task.ref for task in tasks} != release["task_refs"] or len(
        tasks
    ) != len(selected_names):
        raise ValueError("Pinned TB1 tasks are missing or duplicated")
    published = root / "published"
    if published.exists():
        raise ValueError(f"Choose a new published output directory: {published}")
    from harbor.tasks.client import TaskClient

    await TaskClient().download_tasks(tasks, output_dir=published, export=True)
    registry = RegistryDB()
    published_evidence = {}
    registry_evidence = {}
    release_images = {
        entry["task"]: sorted(img["pinned_ref"] for img in entry["images"])
        for entry in manifest["tasks"]
    }
    for task in tasks:
        path = published / task.name
        resolved = await registry.resolve_task_version(task.org, task.name, task.ref)
        if task.ref != f"sha256:{resolved.content_hash}":
            raise ValueError(f"Task reference drift: {task.name}")
        rows = await registry.get_task_version_files(resolved.id)
        expected = {
            row["path"]: (
                row["content_hash"].removeprefix("sha256:"),
                row["size_bytes"],
            )
            for row in rows
        }
        actual = {}
        for file in sorted(path.rglob("*")):
            if file.is_symlink():
                raise ValueError(f"Unexpected symlink in published package: {file}")
            if file.is_file():
                data = file.read_bytes()
                actual[file.relative_to(path).as_posix()] = (
                    sha256(data).hexdigest(),
                    len(data),
                )
        if not actual or actual != expected:
            raise ValueError(f"Published files differ from Hub: {task.name}")
        parsed = Task(path)
        refs = inspect_images(path, parsed.config)
        if refs != release_images[task.name]:
            raise ValueError(
                f"Published images differ from the release manifest: {task.name}"
            )
        published_evidence[task.name] = {
            "ref": task.ref,
            "version": parsed.config.task.version,
            "file_sha256": {name: value[0] for name, value in actual.items()},
            "images": refs,
        }
        registry_evidence[task.name] = {
            "ref": task.ref,
            "revision": resolved.revision,
            "registered_files": len(expected),
            "matching": len(actual),
            "differs": [],
            "local_paths_without_registry_rows": [],
        }
    for name, data in {
        "dataset-metadata.json": {
            **metadata.model_dump(mode="json"),
            "ref": release["dataset_ref"],
        },
        "published-evidence.json": published_evidence,
        "published-registry-check.json": registry_evidence,
    }.items():
        (root / name).write_text(json.dumps(data, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--images", required=True, type=Path)
    args = parser.parse_args()
    pins = json.loads(PINS.read_text())
    if version("harbor") != pins["harbor"]["release"]:
        parser.error("Use the pinned Harbor environment")
    data = args.images.read_bytes()
    if sha256(data).hexdigest() != pins["terminal_bench"]["prebuilt_manifest_sha256"]:
        parser.error("Image manifest differs from the pinned release")
    args.results.mkdir(parents=True, exist_ok=True)
    asyncio.run(prepare(args.results, json.loads(data), pins))


if __name__ == "__main__":
    main()
