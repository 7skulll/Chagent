"""Bounded workspace search: line-oriented grep and path-oriented glob.

Both tools exist to cut model turns, not to be a general search engine: one
call returns locations, the model then reads only what it needs. Results are
bounded in every dimension (files scanned, matches, line length) so a broad
pattern cannot become an unbounded tool output.
"""

import os
from pathlib import Path
import re
import stat
from fnmatch import fnmatch

from chagent.domain.messages import JsonObject, TextContent
from chagent.tools.base import AgentToolResult
from chagent.tools.filesystem import FileTool
from chagent.tools.workspace import MAX_FILE_BYTES, WorkspaceFS


# Vendored, generated and cache directories are never useful hits and can
# dominate the scan cost of a large workspace.
SKIPPED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    ".next", "target", ".idea", ".vscode",
})
MAX_SCANNED_FILES = 20_000
MAX_VISITED_PATHS = 50_000
MAX_LINE_CHARS = 300


def _result(text: str, details: JsonObject) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text)], details=details)


class GrepTool(FileTool):
    def __init__(self):
        super().__init__(
            "grep",
            "Search file contents line by line with a Python regular expression and return matching locations "
            "(path:line: text). Skips binary files, files over 1 MiB and vendored/cache directories. "
            "Use this instead of listing directories when you know what to look for.",
            {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 512},
                "path": {"type": "string", "minLength": 1},
                "include": {"type": "string", "minLength": 1, "maxLength": 256},
                "case_sensitive": {"type": "boolean"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            ["pattern"], read_only=True,
        )

    def handle(self, filesystem: WorkspaceFS, params: JsonObject) -> AgentToolResult:
        pattern = params["pattern"]
        try:
            regex = re.compile(pattern, 0 if params.get("case_sensitive", True) else re.IGNORECASE)
        except re.error as error:
            raise ValueError(f"Invalid regular expression: {error}") from None
        include = params.get("include")
        limit = params.get("max_results", 200)
        root = filesystem.root
        target, is_file = self._target(filesystem, params.get("path", "."))
        rows: list[tuple[str, int, str]] = []
        scanned = 0
        truncated = False
        for relative, data in self._sources(target, is_file, root, include):
            scanned += 1
            if scanned > MAX_SCANNED_FILES:
                truncated = True
                break
            if data is None or len(data) > MAX_FILE_BYTES or b"\0" in data[:4096]:
                continue
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    rows.append((relative, number, line.strip()[:MAX_LINE_CHARS]))
                    # One extra match proves truncation; an exact hit at the limit
                    # does not.
                    if len(rows) > limit:
                        break
            if len(rows) > limit:
                truncated = True
                break
        if len(rows) > limit:
            truncated = True
            rows = rows[:limit]
        files = len({row[0] for row in rows})
        header = (
            f"Pattern: {pattern}\nMatches: {len(rows)} in {files} files (scanned {scanned} files)"
            + ("\n[Truncated: raise max_results or narrow the pattern]\n" if truncated else "\n")
        )
        body = "\n".join(f"{path}:{line}: {text}" for path, line, text in rows) or "[No match]"
        return _result(header + body, {
            "pattern": pattern, "match_count": len(rows), "file_count": files,
            "scanned_files": scanned, "truncated": truncated,
            "paths": sorted({row[0] for row in rows})[:50],
        })

    @staticmethod
    def _target(filesystem: WorkspaceFS, path: str) -> tuple[Path, bool]:
        parts = filesystem.parts(path)
        target = filesystem.root.joinpath(*parts)
        info = os.lstat(target)
        if stat.S_ISREG(info.st_mode):
            return target, True
        if stat.S_ISDIR(info.st_mode):
            # Validate every component without following symlinks before walking.
            with filesystem.directory(parts):
                pass
            return target, False
        raise PermissionError("Only regular files and directories can be searched")

    @staticmethod
    def _sources(target: Path, is_file: bool, root: Path, include: str | None):
        if is_file:
            yield target.relative_to(root).as_posix(), _read(target)
            return
        for current, directories, names in os.walk(target, followlinks=False):
            directories[:] = sorted(
                name for name in directories
                if name not in SKIPPED_DIRECTORIES and not os.path.islink(os.path.join(current, name))
            )
            for name in sorted(names):
                path = Path(current, name)
                if path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                if include and not (fnmatch(relative, include) or fnmatch(name, include)):
                    continue
                yield relative, _read(path)


def _read(path: Path) -> bytes | None:
    try:
        with path.open("rb") as stream:
            return stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None


class GlobTool(FileTool):
    def __init__(self):
        super().__init__(
            "glob",
            "List workspace paths matching a glob pattern such as '**/*.py' or 'src/**/test_*.py'. "
            "Returns sorted relative paths; symlinks are never followed or returned. "
            "Use this to discover files instead of running shell find.",
            {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 512},
                "path": {"type": "string", "minLength": 1},
                "include_dirs": {"type": "boolean"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            ["pattern"], read_only=True,
        )

    def handle(self, filesystem: WorkspaceFS, params: JsonObject) -> AgentToolResult:
        pattern = params["pattern"]
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise PermissionError("Pattern must stay inside the workspace; pass a relative base instead")
        parts = filesystem.parts(params.get("path", "."))
        with filesystem.directory(parts):
            pass
        base = filesystem.root.joinpath(*parts)
        limit = params.get("max_results", 200)
        keep_directories = params.get("include_dirs", False)
        found: list[str] = []
        truncated = False
        visited = 0
        try:
            for path in base.glob(pattern):
                visited += 1
                if visited > MAX_VISITED_PATHS:
                    truncated = True
                    break
                if path.is_symlink():
                    continue
                if path.is_dir() and not keep_directories:
                    continue
                found.append(path.relative_to(filesystem.root).as_posix())
                if len(found) > limit:
                    truncated = True
                    break
        except (OSError, ValueError) as error:
            raise ValueError(f"Invalid glob pattern: {error}") from None
        found.sort()
        selected = found[:limit]
        header = (
            f"Pattern: {pattern}\nPaths: {len(selected)}"
            + (" (truncated)" if truncated else "") + f"\nBase: {params.get('path', '.')}\n"
        )
        return _result(header + ("\n".join(selected) or "[No match]"), {
            "pattern": pattern, "count": len(selected), "truncated": truncated,
            "paths": selected[:50],
        })
