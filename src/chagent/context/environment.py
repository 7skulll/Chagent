"""Run-static workspace facts, rendered once into the system prompt.

The purpose is to remove the opening exploration turn: with the working
directory, the top of the tree, the branch state and the project instructions
already in the prompt, the model does not need ``list_dir`` before it can decide
what to read.

Everything here is computed once at run start and never refreshed. That is not a
limitation but the contract: this text is part of the provider prompt prefix, so
it has to stay byte-identical across every turn of one run. It is therefore
labelled as a snapshot, and the model is told to use tools for anything that may
have changed since.

**Not wired.** Nothing in the request path calls :func:`build` today. Commit
``6917378`` (container execution isolation) removed the call from
``agent/runner.py`` together with the ``environment_projected`` audit event.
``tests/test_environment.py`` still covers :func:`collect` on its own, and
``tests/test_runner.py::test_environment_is_not_injected_into_prompt`` pins the
removal: implemented, tested, and deliberately not injected.
``RunState.environment`` is still set — but only by the containerized branch of
``AgentRunner._load_state``, to a fixed boundary sentence, not to anything
produced here. The module is kept because it is the only implementation of the
idea and re-deriving it is expensive; see ``docs/会话与上下文设计.md`` for why
the snapshot was taken back out of the prefix. Read this file as a proposal, not
as current behaviour.
"""

from dataclasses import dataclass, field
import asyncio
import os
from pathlib import Path
import subprocess

TREE_MAX_DEPTH = 2
TREE_MAX_ENTRIES = 200
TREE_MAX_BYTES = 4096
# A directory with a count but no names still costs a list_dir turn to find the
# file the task is about; names are what remove that turn. They are capped per
# directory so a large tree degrades to counts instead of blowing the budget.
TREE_NAMES_PER_DIRECTORY = 8
AGENTS_MAX_FILES = 4
AGENTS_FILE_MAX_BYTES = 2048
AGENTS_MAX_BYTES = 4096
GIT_TIMEOUT_SECONDS = 1.5

# The snapshot competes with the tool declarations for the same window: 4 KB of
# facts plus 4 KB of schemas is most of an 8 K window. Budgets therefore scale
# with the model's window instead of being a constant, so a small local model
# still gets the working directory and the top of the tree rather than a context
# rejection. Roughly a tenth of the window, split between tree and AGENTS.md.
TREE_BUDGET_DIVISOR = 16
AGENTS_BUDGET_DIVISOR = 32
TREE_MIN_BYTES = 384
AGENTS_MIN_BYTES = 256

# Vendored, generated and cache directories say nothing about the project's own
# layout, and they are the ones that make a tree either enormous or useless.
SKIPPED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    "dist", "build", "target", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".idea", ".next", ".cache", "site-packages", ".workbuddy",
})

SNAPSHOT_NOTICE = (
    "Environment snapshot from the start of this run. It is authoritative for the working "
    "directory and the two directory levels below it: do not list those directories again, "
    "read or grep the files directly, and use list_dir only for a directory that is not shown "
    "below. Anything that changes during the run must be re-read with the tools."
)


@dataclass(slots=True)
class EnvironmentFacts:
    """What could be observed, plus what had to be dropped to fit the budget."""

    text: str
    tree_entries: int = 0
    tree_truncated: bool = False
    agents_files: list[str] = field(default_factory=list)
    git_repo: bool = False


def _skip(name: str) -> bool:
    return name in SKIPPED_DIRECTORIES or name.startswith(".")


def _files_label(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def _file_names(directory: Path, limit: int = TREE_NAMES_PER_DIRECTORY) -> tuple[list[str], int]:
    """Visible file names only: dotfiles are noise here and include credentials."""
    names, total = [], 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if _skip(entry.name):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                total += 1
                if len(names) < limit:
                    names.append(entry.name)
    except OSError:
        return [], 0
    return sorted(names), total


def _describe(directory: Path) -> str:
    names, total = _file_names(directory)
    label = _files_label(total)
    if not total:
        return f"({label})"
    listed = ", ".join(names)
    more = "" if len(names) == total else f", +{total - len(names)} more"
    return f"({label}: {listed}{more})"


def _tree(root: Path, *, max_depth: int = TREE_MAX_DEPTH, max_entries: int = TREE_MAX_ENTRIES,
          max_bytes: int = TREE_MAX_BYTES) -> tuple[list[str], int, bool]:
    """Render directories to ``max_depth`` with their direct file counts and names."""
    lines = [f"{root.name or root}/ {_describe(root)}"]
    entries = 0
    used = len(lines[0].encode("utf-8")) + 1
    truncated = False

    def walk(directory: Path, depth: int, prefix: str) -> bool:
        nonlocal entries, used, truncated
        try:
            with os.scandir(directory) as iterator:
                children = sorted(
                    (entry for entry in iterator if not _skip(entry.name)),
                    key=lambda entry: entry.name,
                )
        except OSError:
            return True
        for entry in children:
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not is_directory:
                continue
            if entries >= max_entries:
                truncated = True
                return False
            line = f"{prefix}{entry.name}/ {_describe(Path(entry.path))}"
            cost = len(line.encode("utf-8")) + 1
            if used + cost > max_bytes:
                truncated = True
                return False
            lines.append(line)
            entries += 1
            used += cost
            if depth > 1 and not walk(Path(entry.path), depth - 1, prefix + "  "):
                return False
        return True

    walk(root, max_depth, "  ")
    if truncated:
        # The marker is deliberately outside the byte budget: a tree that stops
        # without saying so reads as complete.
        lines.append("  ... truncated; use list_dir for the rest")
    return lines, entries, truncated


def _git_environment(root: Path, *, timeout: float = GIT_TIMEOUT_SECONDS) -> list[str]:
    """Branch and a short status summary; empty when this is not a repository."""
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    base = ["git", "-C", os.fspath(root), "-c", "core.quotepath=false"]

    def run(*arguments: str) -> str | None:
        try:
            completed = subprocess.run(base + list(arguments), capture_output=True, text=True,
                                       timeout=timeout, env=environment, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout if completed.returncode == 0 else None

    inside = run("rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        return []
    branch = (run("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if branch == "HEAD":
        branch = (run("rev-parse", "--short", "HEAD") or "detached").strip()
    summary = [f"git: branch {branch or 'unknown'}"]
    status = run("status", "--short", "--untracked-files=normal")
    if status is None:
        summary.append("status unavailable")
        return summary
    rows = [line for line in status.splitlines() if line.strip()]
    if not rows:
        summary.append("working tree clean")
        return summary
    counts: dict[str, int] = {}
    for row in rows:
        kind = "untracked" if row.startswith("??") else "changed"
        counts[kind] = counts.get(kind, 0) + 1
    described = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
    shown = [row[3:].strip() for row in rows[:6]]
    more = "" if len(rows) <= len(shown) else f", +{len(rows) - len(shown)} more"
    return [f"{summary[0]}; status: {described} ({', '.join(shown)}{more})"]


def _repository_root(workspace: Path) -> Path | None:
    for candidate in [workspace, *workspace.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _agents_files(workspace: Path) -> list[Path]:
    """Deepest first, so a workspace file is read as the most specific rule."""
    root = _repository_root(workspace)
    if root is None:
        return [path for path in (workspace / "AGENTS.md",) if path.is_file()]
    chain, current = [], workspace
    while True:
        candidate = current / "AGENTS.md"
        if candidate.is_file():
            chain.append(candidate)
        if current == root or current == current.parent:
            break
        current = current.parent
    return chain


def _agents_section(workspace: Path, *, max_bytes: int = AGENTS_MAX_BYTES) -> tuple[list[str], list[str]]:
    files = _agents_files(workspace)
    if not files:
        return [], []
    root = _repository_root(workspace) or workspace
    blocks, included, used = ["Project instructions (deepest file takes precedence):"], [], 0
    for path in files[:AGENTS_MAX_FILES]:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:AGENTS_FILE_MAX_BYTES]
        except OSError:
            continue
        try:
            label = os.fspath(path.relative_to(root))
        except ValueError:
            label = os.fspath(path)
        block = f"--- {label} ---\n{content.strip()}"
        if used + len(block.encode("utf-8")) > max_bytes:
            blocks.append("(further project instruction files omitted: budget)")
            break
        blocks.append(block)
        included.append(label)
        used += len(block.encode("utf-8"))
    return blocks, included


def budgets_for(context_window: int | None) -> tuple[int, int]:
    """Tree and AGENTS.md byte budgets for a model window.

    ``None`` or a non-positive window means "unknown", and the hard caps apply.
    """
    if not context_window or context_window <= 0:
        return TREE_MAX_BYTES, AGENTS_MAX_BYTES
    tree = max(TREE_MIN_BYTES, min(TREE_MAX_BYTES, context_window // TREE_BUDGET_DIVISOR))
    agents = max(AGENTS_MIN_BYTES, min(AGENTS_MAX_BYTES, context_window // AGENTS_BUDGET_DIVISOR))
    return tree, agents


def collect(workspace: Path, *, tree_bytes: int = TREE_MAX_BYTES,
            agents_bytes: int = AGENTS_MAX_BYTES) -> EnvironmentFacts:
    """Build the snapshot; a missing or unreadable workspace degrades, never raises."""
    workspace = Path(workspace)
    sections = [SNAPSHOT_NOTICE, f"Working directory: {os.fspath(workspace)}"]
    facts = EnvironmentFacts(text="")
    if not workspace.is_dir():
        facts.text = "\n".join(sections)
        return facts
    sections.extend(_git_environment(workspace))
    facts.git_repo = any(line.startswith("git: branch") for line in sections)
    tree, entries, truncated = _tree(workspace, max_bytes=tree_bytes)
    facts.tree_entries, facts.tree_truncated = entries, truncated
    sections.append("Directory tree (up to two levels; counts are totals, names are capped):")
    sections.extend(tree)
    agents, included = _agents_section(workspace, max_bytes=agents_bytes)
    facts.agents_files = included
    if agents:
        sections.extend(agents)
    facts.text = "\n".join(sections)
    return facts


async def build(workspace: Path, *, context_window: int | None = None) -> EnvironmentFacts:
    """Run the snapshot off the event loop; the runner stores it once per run."""
    tree_bytes, agents_bytes = budgets_for(context_window)
    return await asyncio.to_thread(collect, workspace, tree_bytes=tree_bytes, agents_bytes=agents_bytes)
