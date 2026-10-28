"""Bounded text file operations with explicit workspace scope."""

import asyncio
from difflib import unified_diff
from hashlib import sha256
from heapq import nsmallest
import os

from chagent.domain.messages import JsonObject, TextContent
from chagent.tools.base import AgentTool, AgentToolResult, AgentToolUpdateCallback
from chagent.tools.context import ToolContext
from chagent.tools.workspace import WorkspaceFS


PATH_SCHEMA = {"type": "string", "minLength": 1}
HASH_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}


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
        )

    async def execute(
        self, tool_call_id: str, params: JsonObject, signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None, *, context: ToolContext | None = None,
    ) -> AgentToolResult:
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        if context is None:
            raise PermissionError("A session workspace is required")
        return self.handle(WorkspaceFS(context.workspace), params)

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
        super().__init__("read_file", "Read a UTF-8 regular file (up to 1 MiB) with 1-based line numbers and a SHA-256 revision.", {
            "path": PATH_SCHEMA, "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        }, ["path"], read_only=True)

    def handle(self, filesystem: WorkspaceFS, params: JsonObject) -> AgentToolResult:
        path, offset, limit = params["path"], params.get("offset", 1), params.get("limit", 200)
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
    def __init__(self):
        super().__init__("edit_file", "Replace exactly one occurrence of old_text in a UTF-8 workspace file. Read first; supply expected_sha256 to detect stale edits.", {
            "path": PATH_SCHEMA, "old_text": {"type": "string", "minLength": 1, "maxLength": 1_048_576},
            "new_text": {"type": "string", "maxLength": 1_048_576}, "expected_sha256": HASH_SCHEMA,
        }, ["path", "old_text", "new_text"], read_only=False)

    def handle(self, filesystem: WorkspaceFS, params: JsonObject) -> AgentToolResult:
        path = params["path"]
        data = filesystem.read(path)
        original = data.decode("utf-8")
        digest = sha256(data).hexdigest()
        if params.get("expected_sha256", digest) != digest:
            raise ValueError("File changed since it was read")
        if original.count(params["old_text"]) != 1:
            raise ValueError("old_text must match exactly once; provide more surrounding context")
        updated = original.replace(params["old_text"], params["new_text"], 1)
        new_digest = filesystem.write(path, updated.encode("utf-8"), overwrite=True, expected_hash=digest)
        diff = "".join(unified_diff(original.splitlines(keepends=True), updated.splitlines(keepends=True), fromfile=path, tofile=path))
        return text_result(f"Edited: {path}\nSHA256: {new_digest}\n{diff}", {"path": path, "sha256": new_digest})
