"""One recorded call, re-executed in a directory that is thrown away.

The stateful tier exists to answer a narrow question: given this fixture and this
argument, does the tool still produce the same files and the same error contract?
It is not a run. Nothing starts the agent, nothing answers an approval, and the
call is refused unless the tool is a workspace file tool that needs no database,
network or host process.

Three properties are enforced here rather than described:

**The fixture is rebuilt, never reused.** Every replay gets a fresh directory
from declared text files. A recorded ``arguments`` object is a candidate, not a
fixture: without the starting file, ``edit_file`` cannot be replayed, and saying
so is the correct outcome. Only directory fixtures are accepted — unpacking an
archive needs its own traversal and link checks, and pretending that is solved by
``tarfile`` is how the next path-escape bug ships.

**Paths are checked before anything is created.** ``..``, absolute paths, a drive
letter and a backslash are refused, and every component is opened with
``O_NOFOLLOW``, so a pre-existing link cannot redirect a write outside the
directory. The executor repeats these checks; this is the earlier refusal that
keeps a bad fixture from producing a file outside the sandbox in the first place.

**Cleanup is verified, and its failure is an error.** A leftover directory makes
the next trial's baseline unknown, which is worse than a failed replay, so a
cleanup that cannot confirm removal raises instead of warning.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from chagent.models.outbound import invocation_to_host
from chagent.tools.context import ToolContext

from eval.replay import catalog
from eval.replay.contract import (
    Comparison, ErrorContract, NormalizationRules, compare_error, compare_state, snapshot_facts,
)

#: A fixture is text, bounded. These limits keep one case from turning a replay
#: into a disk-copy job, and keep the snapshot small enough to compare by hand.
MAX_FILES = 500
MAX_BYTES = 2 * 1024 * 1024
TEMP_PREFIX = "chagent-replay-"
#: Tools that only create, and so need no previous content to be meaningful.
CREATING = frozenset({"create_directory"})
#: Skill bodies come from the checkout, not from a case fixture.
SKILL_NAMES = frozenset({"read_skill"})


class FixtureInvalid(ValueError):
    """A fixture cannot be used: unsafe path, unreadable entry or binary content."""


class UnsafePath(ValueError):
    """A path would leave the replay directory."""


class ReplayRefused(ValueError):
    """This call may not be replayed, and the reason is the result."""


class CleanupError(RuntimeError):
    """The replay directory could not be proven removed."""


@dataclass(frozen=True)
class Fixture:
    """The declared starting state: relative path to text content."""

    files: dict[str, str]
    notes: str = ""
    directories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", dict(self.files))
        object.__setattr__(self, "directories", tuple(sorted(set(self.directories))))
        for relative in self.directories:
            check_relative(relative)
            if relative in self.files:
                raise FixtureInvalid(f"{relative}: declared as both file and directory")
        if len(self.files) + len(self.directories) > MAX_FILES:
            raise FixtureInvalid(f"fixture has more than {MAX_FILES} files")
        total = 0
        for relative, content in self.files.items():
            check_relative(relative)
            if not isinstance(content, str):
                raise FixtureInvalid(f"{relative}: fixture content must be text")
            total += len(content.encode("utf-8"))
        if total > MAX_BYTES:
            raise FixtureInvalid(f"fixture is larger than {MAX_BYTES} bytes")

    @classmethod
    def from_directory(cls, root: Path, *, ignore: tuple[str, ...] = (".git", "__pycache__")) -> "Fixture":
        root = Path(root)
        if root.is_symlink():
            raise FixtureInvalid("fixture root is a symbolic link")
        if not root.is_dir():
            raise FixtureInvalid(f"{root} is not a directory")
        files: dict[str, str] = {}
        directories = []
        total = 0
        for directory, names, filenames in os.walk(root, followlinks=False):
            for name in names:
                path = Path(directory) / name
                if path.is_symlink():
                    raise FixtureInvalid(f"fixture contains a symbolic link: {path.relative_to(root)}")
            names[:] = sorted(name for name in names if name not in ignore)
            directories.extend((Path(directory) / name).relative_to(root).as_posix() for name in names)
            if len(directories) + len(files) + len(filenames) > MAX_FILES:
                raise FixtureInvalid(f"fixture has more than {MAX_FILES} entries")
            for name in sorted(filenames):
                path = Path(directory) / name
                if path.is_symlink():
                    # A link in a fixture is an unrecorded dependency on content
                    # that is not in the case document.
                    raise FixtureInvalid(f"fixture contains a symbolic link: "
                                         f"{path.relative_to(root).as_posix()}")
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise FixtureInvalid(f"fixture contains a non-regular file: "
                                         f"{path.relative_to(root).as_posix()}")
                total += info.st_size
                if total > MAX_BYTES:
                    raise FixtureInvalid(f"fixture is larger than {MAX_BYTES} bytes")
                try:
                    # Decode bytes directly: universal-newline conversion changes the fixture.
                    text = path.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    raise FixtureInvalid(f"fixture file is not UTF-8 text: "
                                         f"{path.relative_to(root).as_posix()}") from None
                files[path.relative_to(root).as_posix()] = text
        return cls(files, directories=tuple(directories))

    @classmethod
    def from_json(cls, document: dict) -> "Fixture":
        if not isinstance(document, dict) or not isinstance(document.get("files"), dict):
            raise FixtureInvalid("fixture needs a files object")
        directories = document.get("directories", [])
        if not isinstance(directories, list) or any(not isinstance(name, str) for name in directories):
            raise FixtureInvalid("fixture directories must be a list of paths")
        return cls(dict(document["files"]), notes=str(document.get("notes", "")),
                   directories=tuple(directories))

    def paths(self) -> set[str]:
        """Every path the fixture defines, files and their parent directories."""
        known = set()
        for relative in (*self.files, *self.directories):
            parts = relative.split("/")
            for index in range(1, len(parts) + 1):
                known.add("/".join(parts[:index]))
        return known

    def covers(self, relative: str) -> bool:
        parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
        if not parts:
            # The directory itself always exists: it is the replay root.
            return True
        return "/".join(parts) in self.paths()

    def to_json(self) -> dict:
        document = {"files": dict(sorted(self.files.items()))}
        if self.directories:
            document["directories"] = list(self.directories)
        if self.notes:
            document["notes"] = self.notes
        return document


@dataclass(frozen=True)
class CleanupReport:
    removed_files: int
    verified: bool
    note: str = ""

    def to_json(self) -> dict:
        return {"removed_files": self.removed_files, "verified": self.verified,
                "note": self.note}


class Workspace:
    """A disposable directory built from a fixture, then removed and checked."""

    def __init__(self, fixture: Fixture, *, base: Path | None = None) -> None:
        self.fixture = fixture
        self.base = Path(base) if base is not None else None
        self.path: Path | None = None
        self.notes: list[str] = []

    def __enter__(self) -> "Workspace":
        self.build()
        return self

    def __exit__(self, kind, value, traceback) -> None:
        report = self.cleanup()
        if not report.verified and kind is None:
            raise CleanupError(report.note or "replay directory still exists after removal")

    def build(self) -> Path:
        if self.path is not None:
            raise RuntimeError("workspace already built")
        if self.base is not None:
            self.base.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=self.base))
        try:
            for relative in self.fixture.directories:
                _mkdirs(root, _join(root, relative))
            for relative in sorted(self.fixture.files):
                target = _join(root, relative)
                _mkdirs(root, target.parent)
                _write_new(target, self.fixture.files[relative].encode("utf-8"))
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        self.path = root
        return root

    def snapshot(self) -> dict[str, dict]:
        if self.path is None:
            raise RuntimeError("workspace is not built")
        facts: dict[str, dict] = {}
        for directory, names, filenames in os.walk(self.path, followlinks=False):
            names.sort()
            for name in names:
                path = Path(directory) / name
                relative = path.relative_to(self.path).as_posix()
                if path.is_symlink():
                    facts[relative] = {"kind": "symlink"}
                    self.notes.append(f"{relative} is a symbolic link created during replay; "
                                      "its target is not compared")
                else:
                    facts[relative] = {"kind": "directory"}
            for name in sorted(filenames):
                path = Path(directory) / name
                relative = path.relative_to(self.path).as_posix()
                if path.is_symlink():
                    facts[relative] = {"kind": "symlink"}
                    self.notes.append(f"{relative} is a symbolic link created during replay; "
                                      "its target is not compared")
                    continue
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    facts[relative] = {"kind": "other"}
                    continue
                facts[relative] = snapshot_facts(path.read_bytes())
        return facts

    def cleanup(self) -> CleanupReport:
        if self.path is None:
            return CleanupReport(0, True, "nothing was built")
        root = self.path
        if not root.name.startswith(TEMP_PREFIX):
            raise CleanupError(f"refusing to remove {root}: not a replay directory")
        files = sum(1 for item in root.rglob("*") if item.is_file() or item.is_symlink())
        shutil.rmtree(root, ignore_errors=True)
        verified = not root.exists()
        self.path = None
        return CleanupReport(files, verified,
                             "" if verified else f"{root} still exists after removal")


@dataclass(frozen=True)
class CallOutcome:
    tool: str
    ok: bool
    result: dict | None
    error_type: str | None = None
    error_message: str = ""
    arguments: dict | None = None

    @property
    def contract(self) -> ErrorContract:
        if self.error_type is not None:
            return ErrorContract(True, code=self.error_type, message=self.error_message)
        return ErrorContract.from_tool_result(self.result or {})

    def to_json(self) -> dict:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "contract": self.contract.to_json(),
        }


def run_call(name: str, arguments: dict, workspace: Workspace, *,
             require_initial_state: bool = True) -> CallOutcome:
    """Execute one recorded call in ``workspace`` and describe what happened.

    ``require_initial_state`` is on by default: a call that was recorded as
    successful must have had something to work with, and replaying it against a
    fixture that lacks that file would produce a different call. A scenario that
    deliberately reproduces a failure passes ``False`` — the absent file is then
    the input, not an accident.
    """
    if workspace.path is None:
        raise ReplayRefused("workspace is not built")
    reason = catalog.external_reason(name)
    if reason is not None:
        raise ReplayRefused(f"{name}: {reason}; a recorded call is not permission to repeat it")
    reason = catalog.database_reason(name)
    if reason is not None:
        raise ReplayRefused(f"{name}: {reason}; a fixture cannot rebuild database rows")
    reason = catalog.context_reason(name)
    if reason is not None:
        raise ReplayRefused(f"{name}: {reason}; there is no run to hold the change")
    tool = catalog.tool_object(name)
    if tool is None:
        raise ReplayRefused(f"{name}: no declaration in this checkout, so its tier is unknown")
    if not catalog.is_stateful(name):
        raise ReplayRefused(f"{name}: not a workspace file tool")

    converted = invocation_to_host(dict(arguments or {}), workspace.path)
    # Argument paths are checked before the tool has a chance to touch anything,
    # including the writes that need no starting file.
    problems = path_problems(name, converted)
    if problems:
        raise UnsafePath("; ".join(problems))

    # The live path normalises arguments and validates them before executing.
    # Replaying the raw argument dict would test a call the executor never makes.
    prepared = tool.prepare_arguments(converted) if hasattr(tool, "prepare_arguments") else converted
    required = required_state(name, prepared)
    for relative in required:
        if relative == ".":
            continue
        check_relative(relative)
    if require_initial_state:
        absent = [relative for relative in required
                  if relative != "." and not workspace.fixture.covers(relative)]
        if absent:
            raise ReplayRefused("missing initial state: the fixture does not contain "
                                + ", ".join(sorted(absent)[:3]))

    context = ToolContext(workspace=workspace.path)
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator(tool.parameters).validate(prepared)
        result = asyncio.run(tool.execute("replay-call", prepared, context=context))
    except Exception as error:  # the failure is the observation, not a crash
        return CallOutcome(name, False, None, type(error).__name__,
                           str(error)[:500], prepared)
    return CallOutcome(name, True, _result_json(result), None, "", prepared)


def check_relative(relative: str) -> str:
    """Refuse anything that is not a plain path inside the replay directory."""
    if not isinstance(relative, str) or not relative.strip():
        raise UnsafePath("empty path")
    text = relative.strip()
    if text.startswith(("/", "~")):
        raise UnsafePath(f"{text!r} is absolute")
    if "\\" in text:
        raise UnsafePath(f"{text!r} contains a backslash")
    first = text.replace("\\", "/").split("/")[0]
    if ":" in first:
        raise UnsafePath(f"{text!r} looks platform-absolute")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise UnsafePath(f"{text!r} names no path")
    if any(part == ".." for part in parts):
        raise UnsafePath(f"{text!r} traverses out of the directory")
    return "/".join(parts)


#: Argument keys that name a workspace path. A new key is not treated as a path
#: by accident, and adding one here is the single place that changes.
PATH_KEYS = ("path", "filename", "target")


def argument_paths(name: str, arguments: dict) -> tuple[str, ...]:
    paths = []
    for key in PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    if name == "apply_patch":
        paths.extend(patch_paths(str(arguments.get("patch", ""))))
    return tuple(paths)


def path_problems(name: str, arguments: dict) -> list[str]:
    """Every unsafe argument path, as text, without raising on the first."""
    problems = []
    for value in argument_paths(name, arguments):
        if value in (".", "./", "/workspace", "/workspace/"):
            continue
        candidate = value[len("/workspace/"):] if value.startswith("/workspace/") else value
        try:
            check_relative(candidate)
        except UnsafePath as error:
            problems.append(str(error))
    return problems


def required_state(name: str, arguments: dict) -> tuple[str, ...]:
    """Which fixture paths must exist before this call can mean the same thing.

    Derived from the tool, not from the recorded result: a plain ``write_file``
    replaces whatever is there, so it needs nothing, while ``edit_file`` and a
    versioned write are about content that must already exist. ``apply_patch``
    names its files in the patch headers.
    """
    if name in SKILL_NAMES:
        return (f"skills/{arguments.get('name', '')}",)
    if name == "apply_patch":
        # A section that starts at /dev/null creates its file; asking the fixture
        # for content that the patch itself supplies would refuse every new file.
        return tuple(path for path, creates in patch_targets(str(arguments.get("patch", "")))
                     if not creates)
    path = arguments.get("path") or arguments.get("filename") or arguments.get("target")
    if not isinstance(path, str) or not path.strip():
        return ("." if name in ("list_dir", "glob", "grep") else "",)
    cleaned = path.strip()
    if cleaned in ("", "."):
        return ("." if name in ("list_dir", "glob", "grep") else "",)
    if cleaned == "/workspace":
        return ("." if name in ("list_dir", "glob", "grep") else "",)
    if cleaned.startswith("/workspace/"):
        cleaned = cleaned[len("/workspace/"):]
    if name in CREATING:
        return ()
    if name == "write_file" and not arguments.get("expected_sha256"):
        return ()
    return (cleaned,)


def patch_targets(patch: str) -> tuple[tuple[str, bool], ...]:
    """Every file a patch names, and whether the patch creates it."""
    from chagent.tools.patch import parse_patch

    try:
        sections = parse_patch(patch)
    except Exception:
        return ()
    targets = []
    for section in sections:
        creates = section.old_path == "/dev/null"
        raw = section.new_path if section.new_path != "/dev/null" else section.old_path
        if raw == "/dev/null":
            continue
        value = raw.split("\t", 1)[0].strip()
        if value[:2] in ("a/", "b/"):
            value = value[2:]
        if value:
            targets.append((value, creates))
    return tuple(targets)


def patch_paths(patch: str) -> tuple[str, ...]:
    return tuple(path for path, _ in patch_targets(patch))


# ----------------------------------------------------------------------- scenario


@dataclass(frozen=True)
class Scenario:
    """A declared stateful replay: fixture, calls and what the result must be."""

    id: str
    fixture: Fixture
    calls: tuple[dict, ...]
    expect: dict
    notes: str = ""

    @classmethod
    def from_json(cls, document: dict) -> "Scenario":
        if not isinstance(document, dict):
            raise FixtureInvalid("scenario must be an object")
        for key in ("id", "fixture", "calls"):
            if key not in document:
                raise FixtureInvalid(f"scenario is missing {key}")
        calls = document["calls"]
        if not isinstance(calls, list) or not calls:
            raise FixtureInvalid("scenario needs at least one call")
        prepared = []
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
                raise FixtureInvalid("each call needs a tool name")
            arguments = call.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise FixtureInvalid("call arguments must be an object")
            entry = {"tool": call["tool"], "arguments": arguments}
            if call.get("expects_failure"):
                # Declared, because reproducing a failure is the point of some
                # scenarios and an absent starting file is not an accident there.
                entry["expects_failure"] = True
            prepared.append(entry)
        expect = document.get("expect") or {}
        if not isinstance(expect, dict):
            raise FixtureInvalid("expect must be an object")
        return cls(str(document["id"]), Fixture.from_json(document["fixture"]),
                   tuple(prepared), expect, str(document.get("notes", "")))


def load_scenario(path: Path) -> Scenario:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FixtureInvalid(f"{path}: {error}") from None
    return Scenario.from_json(document)


def run_scenario(scenario: Scenario, *, base: Path | None = None) -> dict:
    """Run the calls, compare the declared expectations, then remove the tree.

    A cleanup that cannot be confirmed makes the whole scenario fail
    (``ok: false``) rather than scroll by as a warning: the next trial's baseline
    depends on this directory being gone.
    """
    rules = _rules(scenario.expect.get("normalize") or ())
    declared_errors = scenario.expect.get("errors")
    if declared_errors is not None:
        if not isinstance(declared_errors, list) or len(declared_errors) != len(scenario.calls):
            raise FixtureInvalid("errors must declare one contract per call")
        for wanted in declared_errors:
            if not isinstance(wanted, dict) or type(wanted.get("failed")) is not bool:
                raise FixtureInvalid("each error contract needs a boolean failed field")
    expected_state = None
    if "state" in scenario.expect:
        if not isinstance(scenario.expect["state"], dict):
            raise FixtureInvalid("expected state must be an object")
        expected_state = _expected_state(scenario.expect["state"])
    workspace = Workspace(scenario.fixture, base=base)
    workspace.build()
    outcomes: list[CallOutcome] = []
    comparisons: list[dict] = []
    state: Comparison | None = None
    try:
        for call in scenario.calls:
            outcomes.append(run_call(call["tool"], call["arguments"], workspace,
                                     require_initial_state=not call.get("expects_failure")))
        actual_state = workspace.snapshot()
        if expected_state is not None:
            state = compare_state(expected_state, actual_state, rules)
        for index, wanted in enumerate(declared_errors or ()):
            actual = (outcomes[index].contract if index < len(outcomes)
                      else ErrorContract(True, "missing"))
            comparisons.append(compare_error(
                ErrorContract(**wanted), actual,
                exact_message=bool(scenario.expect.get("exact_message"))).to_json())
        notes = ([scenario.notes] if scenario.notes else []) + list(workspace.notes)
    finally:
        report = workspace.cleanup()
    state_ok = state is None or state.equal
    if declared_errors is not None:
        errors_ok = all(item["equal"] for item in comparisons)
    else:
        # A matching file snapshot cannot turn an unexpected tool failure green.
        errors_ok = all(not outcome.contract.failed
                        and outcome.contract.outcome_unknown is not True
                        and not call.get("expects_failure")
                        for call, outcome in zip(scenario.calls, outcomes))
    document = {
        "scenario_version": 2,
        "id": scenario.id,
        "fixture": {"files": len(scenario.fixture.files), "paths": sorted(scenario.fixture.paths())},
        "calls": [outcome.to_json() for outcome in outcomes],
        "state": state.to_json() if state is not None else None,
        "errors": comparisons,
        "calls_ok": errors_ok,
        "cleanup": report.to_json(),
        "notes": notes,
        "ok": bool(state_ok and errors_ok and report.verified),
    }
    return document


def format_scenario(document: dict, *, limit: int = 20) -> str:
    lines = [f"scenario {document['id']}  {'ok' if document['ok'] else 'not ok'}",
             f"fixture  {document['fixture']['files']} file(s)"]
    for outcome in document["calls"][:limit]:
        contract = outcome["contract"]
        detail = ("ok" if not contract["failed"]
                  else "failed:" + str(contract["code"] or contract["exit_code"]))
        lines.append(f"  {outcome['tool']:<18} {detail}")
    if len(document["calls"]) > limit:
        lines.append(f"  … {len(document['calls']) - limit} more call(s)")
    if document["state"] is not None:
        lines.append("state: " + ("match" if document["state"]["equal"] else "differs"))
    for item in document["errors"]:
        lines.append("error: " + ("match" if item["equal"] else f"{len(item['differences'])} difference(s)"))
    cleanup = document["cleanup"]
    lines.append(f"cleanup: {cleanup['removed_files']} file(s) removed, "
                 f"{'verified' if cleanup['verified'] else 'NOT verified'}")
    for note in document["notes"]:
        lines.append("note: " + note)
    if document["state"] is not None and not document["state"]["equal"]:
        for item in document["state"]["differences"][:limit]:
            lines.append("  state " + item["path"] + ": " + item["kind"])
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------ helpers


def _rules(declarations) -> NormalizationRules:
    from eval.replay.contract import Normalization

    return NormalizationRules(tuple(Normalization(**item) for item in declarations))


def _expected_state(declared: dict) -> dict:
    facts = {}
    for relative, value in declared.items():
        check_relative(relative)
        if not isinstance(value, dict):
            raise FixtureInvalid(f"expected state for {relative} must be an object")
        if "content" in value:
            data = str(value["content"]).encode("utf-8")
            facts[relative] = {"kind": "file", "sha256": hashlib.sha256(data).hexdigest(),
                               "size": len(data)}
        elif value.get("kind") == "directory":
            facts[relative] = {"kind": "directory"}
        elif "sha256" in value:
            facts[relative] = {"kind": "file", "sha256": str(value["sha256"]),
                               "size": int(value.get("size", 0))}
        else:
            raise FixtureInvalid(f"expected state for {relative} needs content, sha256 or "
                                 "kind=directory")
    return facts


def _result_json(result) -> dict:
    content = []
    for block in getattr(result, "content", ()) or ():
        text = getattr(block, "text", None)
        if text is not None:
            # The output is kept as a short observation, not as a transcript.
            content.append({"type": "text", "text": text[:2000]})
    details = getattr(result, "details", None)
    try:
        details = json.loads(json.dumps(details, default=str))
    except (TypeError, ValueError):
        details = None
    return {"is_error": bool(getattr(result, "is_error", False)),
            "outcome_unknown": bool(getattr(result, "outcome_unknown", False)),
            "content": content, "details": details}


def _join(root: Path, relative: str) -> Path:
    parts = check_relative(relative).split("/")
    return root.joinpath(*parts)


def _mkdirs(root: Path, directory: Path) -> None:
    relative = check_relative(directory.relative_to(root).as_posix()) if directory != root else "."
    if relative == ".":
        return
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise UnsafePath(f"{current.name} is a symbolic link")
        try:
            current.mkdir()
        except FileExistsError:
            if not current.is_dir() or current.is_symlink():
                raise UnsafePath(f"{current.name} exists and is not a directory") from None


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


__all__ = [
    "CallOutcome", "CleanupError", "CleanupReport", "Fixture", "FixtureInvalid",
    "ReplayRefused", "Scenario", "UnsafePath", "Workspace", "argument_paths",
    "check_relative", "path_problems",
    "format_scenario", "load_scenario", "patch_paths", "required_state", "run_call",
    "run_scenario",
]
