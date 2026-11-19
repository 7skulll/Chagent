"""Check the platform of official images by immutable Docker Hub digest."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

PINS = Path(__file__).resolve().parents[1] / "pins.json"
DEFAULT_TASKS = {
    "music-harmony",
    "freight-dispatch-shift",
    "production-planning",
    "risk-scorer-replay",
    "react-lead-form",
    "ctr-optimization",
    "nextjs-performance",
    "cumulative-layout-shift",
}
MEDIA_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--task", action="append", help="Select a task instead of the TB0 sample"
    )
    args = parser.parse_args()
    pins = json.loads(PINS.read_text())["terminal_bench"]
    data = args.images.read_bytes()
    if sha256(data).hexdigest() != pins["prebuilt_manifest_sha256"]:
        parser.error("Image manifest differs from the pinned release")
    manifest = json.loads(data)
    selected = set(args.task) if args.task else DEFAULT_TASKS
    tasks = {task["task"]: task["images"] for task in manifest["tasks"]}
    if selected - tasks.keys():
        parser.error(f"Unknown tasks: {sorted(selected - tasks.keys())}")

    repository = manifest["image_repository"]
    url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repository}:pull"
    with urlopen(url, timeout=20) as stream:
        token = json.load(stream)["token"]
    headers = {"Authorization": f"Bearer {token}", "Accept": ", ".join(MEDIA_TYPES)}

    def get(kind, digest):
        url = f"https://registry-1.docker.io/v2/{repository}/{kind}/{digest}"
        with urlopen(Request(url, headers=headers), timeout=20) as stream:
            data = stream.read()
        if "sha256:" + sha256(data).hexdigest() != digest:
            raise ValueError(f"Registry bytes differ for {digest}")
        return json.loads(data)

    def probe(digest):
        try:
            document = get("manifests", digest)
            if "manifests" in document:
                platforms = [
                    entry["platform"]
                    for entry in document["manifests"]
                    if entry.get("platform", {}).get("os") not in (None, "unknown")
                ]
            else:
                platforms = [get("blobs", document["config"]["digest"])]
            names = {
                "/".join(
                    p[key] for key in ("os", "architecture", "variant") if key in p
                )
                for p in platforms
            }
            if not names:
                raise ValueError("No executable platforms in image manifest")
            return digest, {"platforms": sorted(names)}
        except (URLError, TimeoutError, ValueError, KeyError) as error:
            return digest, {"error": str(error)}

    digests = sorted({image["digest"] for task in selected for image in tasks[task]})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(probe, digests))
    args.output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    return int(any("error" in result for result in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
