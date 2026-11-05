"""Optional decisions made before a tool takes execution resources."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from chagent.domain.messages import ToolCall
from chagent.tools.context import ToolContext
from chagent.tools.approvals import ApprovalBroker, UNAVAILABLE, WRITE_REFUSALS
from chagent.tools.policy import ApprovalPolicy, FILE_WRITE_TOOLS, sensitive_path


@dataclass(frozen=True, slots=True)
class GateDecision:
    allow: bool
    reason: str = ""
    code: str = "denied"
    approval_id: UUID | None = None


class ToolGate(Protocol):
    name: str

    async def check(self, call: ToolCall, *, context: ToolContext,
                    tool_record_id: UUID, signal: asyncio.Event, budget) -> GateDecision: ...


class HumanGate:
    name = "human"

    def __init__(self, policy: ApprovalPolicy, broker: ApprovalBroker | None, sandbox_mode: str):
        self.policy, self.broker, self.sandbox_mode = policy, broker, sandbox_mode

    async def check(self, call: ToolCall, *, context: ToolContext,
                    tool_record_id: UUID, signal: asyncio.Event, budget) -> GateDecision:
        if not self.policy.requires_approval(call.name, call.arguments, sandbox=self.sandbox_mode):
            return GateDecision(True)
        if self.broker is None:
            return GateDecision(False, UNAVAILABLE, "unavailable")
        outcome = await self.broker.request(
            run_id=context.run_id, tool_record_id=tool_record_id,
            tool_name=call.name, arguments=call.arguments,
            cwd=str(context.workspace), signal=signal, budget=budget)
        return GateDecision(outcome.approved and outcome.approval_id is not None,
                            outcome.message, outcome.reason, outcome.approval_id)


@dataclass(frozen=True, slots=True)
class WriteIntent:
    """What one write would change, and why it needs a human (empty: it does not)."""

    paths: tuple[str, ...] = ()
    reason: str = ""
    subject: str = ""


def grade_write(tool_name: str, arguments: dict, workspace: Path) -> WriteIntent:
    """Decide whether this write is a decision, by what it would replace.

    The workspace is a bind mount of the user's own directory, so an overwrite
    here is an edit to their real project with no copy and no undo. Creating a
    file where nothing existed is cheap and reversible; replacing one is not.
    A new file in a hook or configuration path is not reversible either: it runs
    later without anyone reading it.
    """
    if tool_name not in FILE_WRITE_TOOLS:
        return WriteIntent()
    # An empty or existing directory is not a change anyone has to approve.
    if tool_name == "create_directory":
        return WriteIntent()
    if tool_name == "apply_patch":
        paths = _patch_paths(str(arguments.get("patch", "")))
        if not paths:
            # Unreadable headers mean the executor will reject the patch anyway;
            # asking about it would only train the user to approve garbage.
            return WriteIntent()
        listed = ", ".join(paths[:3]) + (" …" if len(paths) > 3 else "")
        existing = [path for path in paths if _safe_join(workspace, path) is not None]
        reason = "overwrite" if existing else "patch"
        detail = "replace" if existing else "apply to"
        return WriteIntent(tuple(paths), reason, f"{detail} {len(paths)} file(s): {listed}")
    relative = arguments.get("path") or arguments.get("filename") or ""
    if not isinstance(relative, str) or not relative.strip():
        # The executor rejects an empty path; do not ask about a call that dies.
        return WriteIntent()
    relative = relative.strip()
    if _escapes(relative):
        return WriteIntent((relative,), "outside", f"{relative} is not a path inside the workspace")
    target = _safe_join(workspace, relative)
    if target is None:
        return WriteIntent((relative,), "outside", f"{relative} passes through a symbolic link")
    if target.exists():
        kind = "directory" if target.is_dir() else f"{_size(target)} file"
        return WriteIntent((relative,), "overwrite", f"overwrite {relative} (existing {kind})")
    if sensitive_path(relative):
        return WriteIntent((relative,), "sensitive", f"create {relative}, which runs or configures later")
    return WriteIntent()


def _escapes(relative: str) -> bool:
    if relative.startswith(("/", "~")) or ":" in relative.split("/")[0]:
        return True
    return any(part == ".." for part in relative.replace("\\", "/").split("/"))


def _safe_join(workspace: Path, relative: str) -> Path | None:
    """The target path, or ``None`` when an existing component is a symlink.

    Only used for grading: the executor repeats every check with the real
    no-follow guarantees before anything is written.
    """
    if _escapes(relative):
        return None
    current = Path(workspace)
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
    for part in parts:
        current = current / part
        if current.is_symlink():
            return None
    return current


def _patch_paths(patch: str) -> tuple[str, ...]:
    from chagent.tools.patch import parse_patch

    try:
        sections = parse_patch(patch)
    except Exception:
        return ()
    paths = []
    for section in sections:
        raw = section.new_path if section.new_path != "/dev/null" else section.old_path
        if raw == "/dev/null":
            continue
        name = raw.split("\t", 1)[0].strip()
        if name[:2] in ("a/", "b/"):
            name = name[2:]
        if name:
            paths.append(name)
    return tuple(paths)


def _size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "unknown size"
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"


class WriteGate:
    """Ask before a call overwrites, patches, or lands in a hook or config path.

    This is the half of the boundary the command gate could not cover: the
    container is only a place, and the workspace it mounts is the user's real
    directory, so a silent overwrite reaches their machine without passing
    through any command at all.
    """

    name = "write"

    def __init__(self, policy: ApprovalPolicy, broker: ApprovalBroker | None):
        self.policy, self.broker = policy, broker

    async def check(self, call: ToolCall, *, context: ToolContext,
                    tool_record_id: UUID, signal: asyncio.Event, budget) -> GateDecision:
        if self.policy.mode == "never":
            return GateDecision(True)
        intent = grade_write(call.name, call.arguments, context.workspace)
        if not intent.reason:
            return GateDecision(True)
        if self.broker is None:
            return GateDecision(False, WRITE_REFUSALS["unavailable"], "unavailable")
        outcome = await self.broker.request(
            run_id=context.run_id, tool_record_id=tool_record_id,
            tool_name=call.name, arguments=call.arguments,
            cwd=str(context.workspace), signal=signal, budget=budget,
            subject=intent.subject)
        if outcome.approved and outcome.approval_id is not None:
            return GateDecision(True, "", outcome.reason, outcome.approval_id)
        return GateDecision(False, WRITE_REFUSALS.get(outcome.reason, outcome.message),
                            outcome.reason or "denied")
