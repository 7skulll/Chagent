"""Multi-file unified diff in one call, validated before anything is written.

A single patch is the unit of intent: the model states every edit it means to
make and the tool either applies all of them or none. Hunk locations are
resolved against real file content, so a stale diff fails loudly instead of
writing at a guessed offset.
"""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from chagent.domain.messages import JsonObject, TextContent
from chagent.tools.base import AgentToolResult
from chagent.tools.filesystem import FileTool
from chagent.tools.workspace import MAX_FILE_BYTES, WorkspaceFS


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DEV_NULL = "/dev/null"


class PatchError(ValueError):
    pass


@dataclass(slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_count: int
    lines: list[tuple[str, str]]


@dataclass(slots=True)
class Section:
    old_path: str
    new_path: str
    hunks: list[Hunk]


def parse_patch(text: str) -> list[Section]:
    lines = text.splitlines()
    sections: list[Section] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            if lines[index].strip() and not lines[index].startswith("***"):
                raise PatchError(f"Line {index + 1}: expected a '--- ' file header")
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise PatchError(f"Line {index + 1}: '--- ' must be followed by '+++ '")
        old_path, new_path = lines[index][4:].strip(), lines[index + 1][4:].strip()
        index += 2
        hunks: list[Hunk] = []
        while index < len(lines) and not _starts_section(lines, index):
            line = lines[index]
            if line.startswith("***"):
                # Codex-style begin/end markers are common model habits and carry
                # no meaning for a unified diff.
                index += 1
                continue
            if not line.startswith("@@"):
                if line.strip():
                    raise PatchError(f"Line {index + 1}: expected a hunk header '@@ -n,m +n,m @@'")
                index += 1
                continue
            match = HUNK_HEADER.match(line)
            if match is not None:
                old_start, old_count = int(match.group(1)), int(match.group(2) or 1)
                new_count = int(match.group(4) or 1)
            elif line.strip() == "@@":
                # A bare header means "locate this hunk by content"; line numbers
                # are a hint, never a requirement.
                old_start, old_count, new_count = 1, 0, 0
            else:
                raise PatchError(f"Line {index + 1}: malformed hunk header")
            index += 1
            body: list[tuple[str, str]] = []
            while index < len(lines) and not lines[index].startswith("@@") and not _starts_section(lines, index):
                entry = lines[index]
                if entry.startswith("***") or entry.startswith("\\"):
                    index += 1
                    continue
                if entry == "":
                    body.append((" ", ""))
                elif entry[0] in " +-":
                    body.append((entry[0], entry[1:]))
                else:
                    raise PatchError(f"Line {index + 1}: hunk line must start with ' ', '+' or '-'")
                index += 1
            if not body:
                raise PatchError(f"Hunk near line {old_start} has no lines")
            hunks.append(Hunk(old_start, old_count, new_count, body))
        if not hunks:
            raise PatchError(f"{old_path}: file section has no hunks")
        sections.append(Section(old_path, new_path, hunks))
    if not sections:
        raise PatchError("Patch contains no file sections")
    return sections


def _starts_section(lines: list[str], index: int) -> bool:
    return (lines[index].startswith("--- ") and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ "))


def _normalize(raw: str) -> str:
    value = raw.split("\t")[0].strip()
    for prefix in ("a/", "b/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if not value:
        raise PatchError("Empty path in patch header")
    if value.startswith("/") or ".." in Path(value).parts:
        raise PatchError(f"Patch path must be relative and inside the workspace: {value}")
    return value


def _locate(lines: list[str], olds: list[str], expected: int) -> int | None:
    if not olds:
        return max(0, min(expected, len(lines)))
    span = len(olds)
    last = len(lines) - span
    if 0 <= expected <= last and lines[expected:expected + span] == olds:
        return expected
    hits = [index for index in range(last + 1) if lines[index:index + span] == olds]
    if not hits:
        return None
    nearest = min(hits, key=lambda index: (abs(index - expected), index))
    if sum(1 for index in hits if abs(index - expected) == abs(nearest - expected)) > 1:
        return None  # Ambiguous: refuse rather than edit the wrong occurrence.
    return nearest


def apply_hunks(original: str, hunks: list[Hunk], label: str) -> tuple[str, int, int]:
    lines = original.split("\n") if original else []
    delta = added = removed = 0
    for number, hunk in enumerate(hunks, 1):
        olds = [text for op, text in hunk.lines if op in " -"]
        news = [text for op, text in hunk.lines if op in " +"]
        position = _locate(lines, olds, hunk.old_start - 1 + delta)
        if position is None:
            raise PatchError(
                f"{label}: hunk {number} does not match the file near line {hunk.old_start}; "
                "read the file again and rebuild the patch")
        lines[position:position + len(olds)] = news
        delta += len(news) - len(olds)
        added += sum(1 for op, _ in hunk.lines if op == "+")
        removed += sum(1 for op, _ in hunk.lines if op == "-")
    return "\n".join(lines), added, removed


@dataclass(slots=True)
class Plan:
    action: str
    path: str
    before: bytes | None
    after: bytes
    added: int
    removed: int

    @property
    def digest(self) -> str:
        return sha256(self.after).hexdigest()


class ApplyPatchTool(FileTool):
    def __init__(self):
        super().__init__(
            "apply_patch",
            "Apply one unified diff to one or more workspace files in a single call. "
            "Format: '--- a/path' followed by '+++ b/path' per file, then hunks "
            "'@@ -old_start,old_count +new_start,new_count @@' whose body lines start with "
            "' ' (context), '-' (remove) or '+' (add). Use '--- /dev/null' to create a file and "
            "'+++ /dev/null' to delete one. Hunks are located by their context lines; if any hunk "
            "fails to apply, no file is written.",
            {"patch": {"type": "string", "minLength": 1, "maxLength": MAX_FILE_BYTES}},
            ["patch"], read_only=False,
        )

    def handle(self, filesystem: WorkspaceFS, params: JsonObject) -> AgentToolResult:
        plans = [self._plan(filesystem, section) for section in parse_patch(params["patch"])]
        duplicates = {plan.path for plan in plans if [item.path for item in plans].count(plan.path) > 1}
        if duplicates:
            raise PatchError(f"Each file may appear once per patch: {', '.join(sorted(duplicates))}")
        if getattr(filesystem, 'require_versions', False):
            # Validate every read revision before any file is changed.
            for plan in plans:
                if plan.action != 'created' and filesystem.versions.get(plan.path) != sha256(plan.before).hexdigest():
                    raise PatchError(f'{plan.path}: provide the latest read SHA256 in versions before patching')
        return self._commit(filesystem, plans)

    def _plan(self, filesystem: WorkspaceFS, section: Section) -> Plan:
        old_path = None if section.old_path == DEV_NULL else _normalize(section.old_path)
        new_path = None if section.new_path == DEV_NULL else _normalize(section.new_path)
        if old_path is not None and new_path is not None and old_path != new_path:
            raise PatchError(f"Renames are not supported in one hunk set: {old_path} -> {new_path}")
        path = new_path or old_path
        try:
            before = filesystem.read(path)
        except FileNotFoundError:
            before = None
        if old_path is None:
            if before is not None:
                raise PatchError(f"{path}: file already exists, cannot create")
            before = b""
            creating = True
        elif before is None:
            # '--- a/path' with '@@ -0,0' is how many tools spell "new file".
            if not all(hunk.old_start == 0 and not any(op == "-" for op, _ in hunk.lines)
                       for hunk in section.hunks):
                raise PatchError(f"{path}: file does not exist, cannot patch")
            before = b""
            creating = True
        else:
            creating = False
        try:
            original = before.decode("utf-8")
        except UnicodeDecodeError:
            raise PatchError(f"{path}: only UTF-8 text files can be patched") from None
        updated, added, removed = apply_hunks(original, section.hunks, path)
        if new_path is None:
            if updated.strip():
                raise PatchError(f"{path}: deletion patch leaves content behind")
            action = "deleted"
        elif creating:
            action = "created"
            # A unified diff cannot express "no trailing newline" portably; text
            # files created through this tool always end with one.
            if updated and not updated.endswith("\n"):
                updated += "\n"
        else:
            action = "modified"
        return Plan(action, path, before, updated.encode("utf-8"), added, removed)

    def _commit(self, filesystem: WorkspaceFS, plans: list[Plan]) -> AgentToolResult:
        for plan in plans:
            if len(plan.after) > MAX_FILE_BYTES:
                raise PatchError(f"{plan.path}: result exceeds the 1 MiB file-tool limit")
        committed: list[str] = []
        for plan in plans:
            try:
                if plan.action == "deleted":
                    filesystem.remove(plan.path, expected_hash=sha256(plan.before).hexdigest())
                elif plan.action == "created":
                    filesystem.write(plan.path, plan.after, overwrite=False)
                else:
                    filesystem.write(plan.path, plan.after, overwrite=True,
                                     expected_hash=sha256(plan.before).hexdigest())
            except Exception as error:
                # Cross-file atomicity is not available on a POSIX filesystem;
                # report exactly what landed instead of implying a clean rollback.
                raise PatchError(
                    f"{plan.path}: write failed ({type(error).__name__}: {error}); "
                    f"already committed: {committed or 'none'}") from error
            committed.append(plan.path)
        counts = [f"{action} {total}" for action in ("created", "modified", "deleted")
                  if (total := sum(1 for item in plans if item.action == action))]
        lines = [f"Patched {len(plans)} file(s): " + ", ".join(counts)]
        for plan in plans:
            lines.append(f"{plan.action[0].upper()} {plan.path} (+{plan.added} -{plan.removed}) "
                         f"sha256={plan.digest}")
        return AgentToolResult(
            content=[TextContent("\n".join(lines))],
            details={"files": [{"path": plan.path, "action": plan.action, "added": plan.added,
                                "removed": plan.removed, "sha256": plan.digest} for plan in plans]},
        )
