"""Bounded text file operations with explicit workspace scope."""

import asyncio
from difflib import unified_diff
from hashlib import sha256
from heapq import nsmallest
import json
import os

from chagent.domain.messages import JsonObject, TextContent
from chagent.documents import ParseCache, is_binary_document, parse_with_cache
from chagent.tools.base import AgentTool, AgentToolResult, AgentToolUpdateCallback
from chagent.tools.context import ToolContext
from chagent.tools.edit_diff import Edit, apply_edits
from chagent.tools.workspace import WorkspaceFS


PATH_SCHEMA = {"type": "string", "minLength": 1}
HASH_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}

#: Binary documents are parsed rather than decoded, so the 1 MiB text limit
#: does not apply. Parsing runs in the tool's worker thread, and this bound
#: keeps one pathological file from holding that thread for the whole timeout.
DOCUMENT_MAX_BYTES = 32 * 1024 * 1024

#: Shared across calls so paging through a long PDF parses it once. Keyed by
#: content digest, so an overwritten file is re-parsed instead of going stale.
_DOCUMENT_CACHE = ParseCache(limit=8)


def text_result(text: str, details: JsonObject) -> AgentToolResult:
    # Files are already bounded by WorkspaceFS. Persistence creates an artifact
    # before the context layer substitutes an excerpt, so no generated text is lost.
    return AgentToolResult(content=[TextContent(text)], details={**details, "output_truncated": False})


class FileTool(AgentTool):
    """File tools require per-invocation context; instances hold no workspace."""

    def __init__(self, name: str, description: str, properties: JsonObject, required: list[str], *, read_only: bool):
        super().__init__(
            name=name, label=name, description=description,
            parameters={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            requires_workspace=True, replay="safe" if read_only else "never",
            execution_mode="parallel" if read_only else "sequential",
            mutates_workspace=not read_only,
        )
        self.read_only = read_only

    async def execute(
        self, tool_call_id: str, params: JsonObject, signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None, *, context: ToolContext | None = None,
    ) -> AgentToolResult:
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        if context is None:
            raise PermissionError("A session workspace is required")
        filesystem = WorkspaceFS(context.workspace)
        # Every file tool runs off the event loop, writes included. edit_file
        # searches for a near match when the exact text is absent, and that
        # search is quadratic in the size of old_text: a failing 40-line edit on
        # a 400-line file takes ~19s. On the loop that stalls this run's
        # heartbeat (5s interval, 30s lease), every other session sharing the
        # process, and the tool timeout itself, which can only fire at an
        # await point. Serialisation between writes is the scheduler's job, not
        # the loop's: one workspace gate admits a single writer at a time.
        task = asyncio.create_task(asyncio.to_thread(self.handle, filesystem, params))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A thread cannot be forcibly cancelled. Drain it before releasing
            # its scheduler slot so late reads cannot cross a write barrier and
            # a half-finished write is still reported as unknown, not as done.
            await asyncio.gather(task, return_exceptions=True)
            raise

    def handle(self, filesystem: WorkspaceFS, params: JsonObject) -> AgentToolResult:
        raise NotImplementedError


class ListDirTool(FileTool):
    def __init__(self):
        super().__init__("list_dir", "List one directory in the session workspace. Symlinks are listed but never followed.", {
            "path": PATH_SCHEMA, "offset": {"type": "integer", "minimum": 0, "maximum": 10_000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        }, [], read_only=True)

    def handle(self, filesystem: WorkspaceFS, params: JsonObject) -> AgentToolResult:
        path, offset, limit = params.get("path", "."), params.get("offset", 0), params.get("limit", 200)
        with filesystem.directory(filesystem.parts(path)) as descriptor:
            with os.scandir(descriptor) as iterator:
                entries = nsmallest(offset + limit + 1, iterator, key=lambda item: item.name)
            selected = entries[offset:offset + limit]
            rows = [f"{entry.name}{'@' if entry.is_symlink() else '/' if entry.is_dir(follow_symlinks=False) else ''}" for entry in selected]
        more = len(entries) > offset + limit
        header = f"Directory: {path}\nOffset: {offset}\nNext offset: {offset + limit if more else 'none'}\n"
        return text_result(header + ("\n".join(rows) or "[Empty page]"), {
            "path": path, "offset": offset, "count": len(selected),
            "has_more": more, "next_offset": offset + limit if more else None,
        })


class ReadFileTool(FileTool):
    def __init__(self):
        super().__init__("read_file", (
            "Read one workspace file, paged. Text files come back as UTF-8 lines with a SHA-256 "
            "revision. PDF, DOCX and XLSX are parsed into numbered text blocks tagged with their "
            "page or sheet, and offset/limit then count blocks instead of lines. Always continue "
            "from the reported Next offset until it is 'none'."
        ), {
            "path": PATH_SCHEMA, "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        }, ["path"], read_only=True)

    def handle(self, filesystem: WorkspaceFS, params: JsonObject) -> AgentToolResult:
        path, offset, limit = params["path"], params.get("offset", 1), params.get("limit", 200)
        if is_binary_document(path):
            return read_document(filesystem, path, offset, limit)
        data = filesystem.read(path)
        lines = data.decode("utf-8").splitlines()
        selected = lines[offset - 1:offset - 1 + limit]
        digest = sha256(data).hexdigest()
        body = "\n".join(f"L{number}: {line}" for number, line in enumerate(selected, start=offset))
        next_offset = offset + len(selected) if offset - 1 + len(selected) < len(lines) else None
        return text_result(f"File: {path}\nSHA256: {digest}\nTotal lines: {len(lines)}\nNext offset: {next_offset or 'none'}\n{body}", {
            "path": path, "sha256": digest, "total_lines": len(lines), "offset": offset,
            "next_offset": next_offset,
        })


def read_document(filesystem: WorkspaceFS, path: str, offset: int, limit: int) -> AgentToolResult:
    """Read one parsed page of a binary document.

    The page boundary is a text block, not a character window, so a table never
    arrives cut in half and every block carries the page it came from.
    """
    data = filesystem.read(path, limit=DOCUMENT_MAX_BYTES)
    digest = sha256(data).hexdigest()
    blocks = parse_with_cache(_DOCUMENT_CACHE, digest, path, data)
    selected = blocks[offset - 1:offset - 1 + limit]
    rows = []
    for number, block in enumerate(selected, start=offset):
        locator = f" [page {block.page}]" if block.page else (f" [{block.section}]" if block.section else "")
        rows.append(f"B{number}{locator}: {block.text}")
    next_offset = offset + len(selected) if offset - 1 + len(selected) < len(blocks) else None
    header = (f"File: {path}\nSHA256: {digest}\nFormat: parsed document\n"
              f"Total blocks: {len(blocks)}\nNext offset: {next_offset or 'none'}\n")
    return text_result(header + ("\n".join(rows) or "[Empty page]"), {
        "path": path, "sha256": digest, "format": "document", "total_blocks": len(blocks),
        "offset": offset, "next_offset": next_offset,
    })


class WriteFileTool(FileTool):
    def __init__(self):
        super().__init__("write_file", "Create a UTF-8 file in an existing workspace directory. Overwrite requires overwrite=true; expected_sha256 detects stale content.", {
            "path": PATH_SCHEMA, "content": {"type": "string", "maxLength": 1_048_576},
            "overwrite": {"type": "boolean"}, "expected_sha256": HASH_SCHEMA,
        }, ["path", "content"], read_only=False)

    def handle(self, filesystem: WorkspaceFS, params: JsonObject) -> AgentToolResult:
        digest = filesystem.write(params["path"], params["content"].encode("utf-8"),
                                  overwrite=params.get("overwrite", False), expected_hash=params.get("expected_sha256"))
        return text_result(f"Written: {params['path']}\nSHA256: {digest}", {"path": params["path"], "sha256": digest})


class EditFileTool(FileTool):
    """Multi-region edit: all pairs are matched against the original file.

    The single-pair form (``old_text``/``new_text``) stays accepted so an old
    prompt or a replayed message keeps working; ``prepare_arguments`` folds it
    into the one shape the implementation handles.
    """

    def __init__(self):
        super().__init__("edit_file", (
            "Edit an existing UTF-8 file. edits is [{old_text, new_text}]; every pair is matched "
            "against the original file and must match exactly once. Quotes, dashes and trailing "
            "whitespace are tolerated when matching. Nothing is written unless every edit resolves. "
            "Read first; expected_sha256 detects stale content."
        ), {
            "path": PATH_SCHEMA,
            "edits": {
                "type": "array", "minItems": 1, "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string", "minLength": 1, "maxLength": 1_048_576},
                        "new_text": {"type": "string", "maxLength": 1_048_576},
                    },
                    "required": ["old_text", "new_text"], "additionalProperties": False,
                },
            },
            "expected_sha256": HASH_SCHEMA,
        }, ["path", "edits"], read_only=False)

    def prepare_arguments(self, params: JsonObject) -> JsonObject:
        """Accept the shapes models actually send for the same edit request."""
        prepared = {key: value for key, value in params.items() if key not in {"edits", "old_text", "new_text", "oldText", "newText", "old_str", "new_str"}}
        raw = params.get("edits")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ValueError(f"edits must be a JSON array of {{old_text, new_text}} objects: {error}") from error
        if isinstance(raw, dict):
            raw = [raw]
        if raw is not None and not isinstance(raw, list):
            raise ValueError("edits must be an array of {old_text, new_text} objects")
        entries = list(raw or [])
        legacy_old = params.get("old_text", params.get("oldText", params.get("old_str")))
        legacy_new = params.get("new_text", params.get("newText", params.get("new_str")))
        if legacy_old is not None:
            same_pair = bool(entries) and isinstance(entries[0], dict) and entries[0].get(
                "old_text", entries[0].get("oldText")) == legacy_old
            if entries and not (len(entries) == 1 and same_pair):
                raise ValueError("Provide either edits or old_text/new_text, not both")
            if not entries:
                entries = [{"old_text": legacy_old, "new_text": "" if legacy_new is None else legacy_new}]
        repaired = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"edits[{index}] must be an object with old_text and new_text")
            old_text, new_text = _edit_pair(entry, index)
            repaired.append({"old_text": old_text, "new_text": new_text})
        if repaired:
            prepared["edits"] = repaired
        elif params.get("path") is not None:
            raise ValueError("edit_file requires edits: [{old_text, new_text}] (at least one pair)")
        return prepared

    def handle(self, filesystem: WorkspaceFS, params: JsonObject) -> AgentToolResult:
        path = params["path"]
        data = filesystem.read(path)
        original = data.decode("utf-8")
        digest = sha256(data).hexdigest()
        if params.get("expected_sha256", digest) != digest:
            raise ValueError("File changed since it was read")
        edits = [Edit(item["old_text"], item["new_text"]) for item in params["edits"]]
        updated, applied = apply_edits(original, edits, path)
        new_digest = filesystem.write(path, updated.encode("utf-8"), overwrite=True, expected_hash=digest)
        diff = "".join(unified_diff(original.splitlines(keepends=True), updated.splitlines(keepends=True), fromfile=path, tofile=path))
        strategies = ", ".join(f"edits[{item.index}]={item.strategy}" for item in applied)
        count = "edit" if len(applied) == 1 else "edits"
        return text_result(f"Edited: {path} ({len(applied)} {count}; {strategies})\nSHA256: {new_digest}\n{diff}", {
            "path": path, "sha256": new_digest, "edit_count": len(applied),
            "strategies": [item.strategy for item in applied],
        })


def _edit_pair(entry: dict, index: int) -> tuple[str, str]:
    """Read one pair, tolerating camelCase and an omitted replacement."""
    old_text = entry.get("old_text", entry.get("oldText", entry.get("old_str")))
    new_text = entry.get("new_text", entry.get("newText", entry.get("new_str", "")))
    if not isinstance(old_text, str) or not old_text:
        raise ValueError(f"edits[{index}].old_text must be a non-empty string")
    if new_text is None:
        new_text = ""
    if not isinstance(new_text, str):
        raise ValueError(f"edits[{index}].new_text must be a string")
    return old_text, new_text
