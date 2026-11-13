"""Version manifests: what code, prompts, tools and skills produced a trial.

A trial is only comparable to another trial when both can name their inputs. This
module answers that question at two deliberately separate levels:

``VersionManifest.capture``
    Fingerprints the *checkout*: commit, the uncommitted patch, the dependency
    lock and the platform. It answers "which tree produced this result", which a
    branch name cannot — branches and tags move, a commit does not.

``VersionManifest.with_request``
    Fingerprints one *stored model request*: the exact system instructions and
    the exact tool declarations that call carried. Source hashing cannot answer
    this, because declarations are assembled per run and may be deferred,
    restricted or replaced by a later system message. ``chagent`` keeps the
    request verbatim in ``model_calls.request``; the same merge rules
    ``models/transcript.normalize`` applies to the live transcript are applied
    here to the stored form, so the hash describes what the provider saw rather
    than what the source file said it might.

Neither level is a promise of reproducibility. A floating provider alias, a
mutable image tag, or a network dependency inside a fixture stays unprovable, and
the manifest records that in ``warnings`` instead of implying more.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import platform
import subprocess

SCHEMA_VERSION = 1
#: Repository-relative paths this manifest knows how to fingerprint.
PROMPT_SOURCE = Path("src/chagent/context/builder.py")
TOOLS_SOURCE = Path("src/chagent/tools")
SKILLS_SOURCE = Path("src/chagent/skills")
LOCK_SOURCE = Path("uv.lock")


class CaseInvalid(ValueError):
    """A case document does not satisfy the published contract."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical(value) -> str:
    """One stable JSON spelling. Sorting and separators are part of the hash."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value) -> str:
    return sha256_text(canonical(value))


def file_sha256(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def prompt_material(request: dict) -> tuple[str, list[dict]]:
    """The instructions and the surviving tool declarations of a stored request.

    Mirrors ``chagent.models.transcript.normalize``: system messages append
    instructions, named sections override each other by name (``None`` removes),
    and tools are added and removed by name. The stored form is
    ``asdict(ModelRequest)``, so a system message is recognised by its
    ``sections``/``tools_added`` fields rather than by a role tag that the
    dataclass does not carry. ``tests/test_eval_manifests.py`` builds the request
    from the real dataclasses, so a shape change breaks that test instead of
    silently hashing the wrong thing.
    """
    instructions: list[str] = []
    sections: dict[str, str] = {}
    tools: dict[str, dict] = {}
    for message in request.get("messages") or []:
        if not isinstance(message, dict) or "sections" not in message:
            continue
        content = message.get("content")
        text = content if isinstance(content, str) else "".join(
            block.get("text", "") for block in content or [] if isinstance(block, dict))
        if text:
            instructions.append(text)
        for name, value in (message.get("sections") or {}).items():
            if value is None:
                sections.pop(name, None)
            else:
                sections[name] = value
        for tool in message.get("tools_added") or []:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                tools[tool["name"]] = tool
        for tool in message.get("tools_removed") or []:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                tools.pop(tool["name"], None)
    prompt = "\n\n".join([*instructions, *sections.values()])
    return prompt, [tools[name] for name in sorted(tools)]


@dataclass(frozen=True)
class VersionManifest:
    """The inputs one trial was produced from. Serialised as-is."""

    repo: dict
    prompts: dict
    tools: dict
    skills: dict
    dependencies: dict
    runtime: dict
    model: dict | None = None
    schema_version: int = SCHEMA_VERSION
    warnings: tuple[str, ...] = ()

    @classmethod
    def capture(
        cls, root: Path, *, model: dict | None = None, image_digest: str | None = None,
    ) -> "VersionManifest":
        root = Path(root).resolve()
        warnings: list[str] = []

        commit = _git_text(root, "rev-parse", "HEAD")
        patch, dirty = _patch_digest(root, warnings)
        if commit is None:
            warnings.append("not a git checkout: the tree cannot be pinned to a commit")
        repo = {"commit": commit, "dirty": dirty, "patch_sha256": patch}

        skills, skill_warnings = _skill_hashes(root)
        warnings.extend(skill_warnings)

        lock = file_sha256(root / LOCK_SOURCE)
        if lock is None:
            warnings.append(f"{LOCK_SOURCE.as_posix()} missing: dependency versions are unbound")

        prompts = {"source": PROMPT_SOURCE.as_posix(), "sha256": file_sha256(root / PROMPT_SOURCE)}
        tools = {
            "source": TOOLS_SOURCE.as_posix(),
            "sha256": _tree_digest(root / TOOLS_SOURCE),
            # Filled by with_request. Source text is not a declaration.
            "declarations_sha256": None,
            "names": None,
        }
        if prompts["sha256"] is None:
            warnings.append("prompt source unreadable: prompt version is unbound")
        if image_digest is None:
            warnings.append("no container image digest: the execution environment is not pinned")
        warnings.append(
            "no model request: tool declarations are unfingerprinted until a run's request is attached")

        return cls(
            repo=repo,
            prompts=prompts,
            tools=tools,
            skills=skills,
            dependencies={"lock_sha256": lock, "python": platform.python_version()},
            runtime={"platform": platform.platform(), "image_digest": image_digest},
            model=model,
            warnings=tuple(warnings),
        )

    def with_request(self, request: dict) -> "VersionManifest":
        """Add the fingerprint of one stored request, without exporting its text.

        Only hashes and tool names leave this method. The request carries the
        conversation, so returning it would turn a version check into a
        transcript export — a different decision with a different audience.
        """
        prompt, declarations = prompt_material(request)
        return self.with_fingerprint({
            "prompt_sha256": sha256_text(prompt),
            "declarations_sha256": sha256_json(declarations),
            "tool_names": [tool.get("name") for tool in declarations],
        })

    def with_fingerprint(self, fingerprint: dict) -> "VersionManifest":
        """Apply a fingerprint that was computed where the request lives.

        The export process can hash a stored request without handing its text to
        the caller, so the manifest can be complete while the transcript stays in
        the database it was read from.
        """
        return replace(
            self,
            prompts={**self.prompts, "request_sha256": fingerprint["prompt_sha256"]},
            tools={
                **self.tools,
                "declarations_sha256": fingerprint["declarations_sha256"],
                "names": fingerprint["tool_names"],
            },
            warnings=tuple(
                warning for warning in self.warnings if not warning.startswith("no model request")),
        )

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "repo": self.repo,
            "prompts": self.prompts,
            "tools": self.tools,
            "skills": self.skills,
            "dependencies": self.dependencies,
            "runtime": self.runtime,
            "model": self.model,
            "warnings": list(self.warnings),
        }

    def missing(self) -> list[str]:
        """Which manifest fields a trial cannot be compared without."""
        absent = []
        if not self.repo.get("commit"):
            absent.append("repo.commit")
        if self.repo.get("dirty") and not self.repo.get("patch_sha256"):
            absent.append("repo.patch_sha256")
        if not self.prompts.get("request_sha256"):
            absent.append("prompts.request_sha256")
        if not self.tools.get("declarations_sha256"):
            absent.append("tools.declarations_sha256")
        if not self.skills:
            absent.append("skills")
        if self.model is None:
            absent.append("model")
        return absent


def _git_text(root: Path, *args: str) -> str | None:
    completed = _git(root, *args)
    return completed.decode("utf-8", "replace").strip() if completed is not None else None


def _git(root: Path, *args: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _patch_digest(root: Path, warnings: list[str]) -> tuple[str | None, bool]:
    """Hash everything that is not in the commit: tracked edits and new files.

    ``git diff`` alone misses exactly the files a reviewer most needs to see — a
    new test, a new skill body, a new module — so untracked files are hashed by
    path and content. ``eval/results`` is excluded by ``.gitignore`` so writing a
    trial does not change the version of the tree that produced it.
    """
    diff = _git(root, "diff", "HEAD", "--binary")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if diff is None or untracked is None:
        warnings.append("git could not report the working tree: the uncommitted patch is unknown")
        return None, True
    parts = [diff]
    names = sorted(filter(None, untracked.split(b"\0")))
    for raw in names:
        path = root / raw.decode("utf-8", "surrogateescape")
        digest = file_sha256(path)
        parts.append(raw + b"\0" + (digest or "unreadable").encode() + b"\n")
    dirty = bool(diff.strip() or names)
    return (sha256_bytes(b"".join(parts)) if dirty else None), dirty


def _skill_hashes(root: Path) -> tuple[dict, list[str]]:
    """The bundled skills, by the text the model would actually read."""
    try:
        from chagent.skills import SKILL_NAMES, read_skill

        return ({name: sha256_text(read_skill(name)) for name in SKILL_NAMES}, [])
    except (ImportError, OSError, ValueError) as error:
        # An exported tree without ``src`` on the path still needs a fingerprint.
        warning = f"skill list read from disk, chagent not importable: {type(error).__name__}"
    hashes = {}
    for path in sorted((root / SKILLS_SOURCE).glob("*/SKILL.md")):
        digest = file_sha256(path)
        if digest is not None:
            hashes[path.parent.name] = digest
    if not hashes:
        return {}, [warning, "no bundled skills found: the skill surface is unbound"]
    return hashes, [warning]


def _tree_digest(root: Path) -> str | None:
    if not root.is_dir():
        return None
    parts = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".md"}:
            continue
        digest = file_sha256(path)
        if digest is not None:
            parts.append(f"{path.relative_to(root).as_posix()}\0{digest}\n")
    return sha256_text("".join(parts)) if parts else None
