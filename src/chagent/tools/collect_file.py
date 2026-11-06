"""Copy one bounded regular file from a workspace container into OutputService."""

import asyncio
from hashlib import sha256
from pathlib import PurePosixPath

from chagent.domain.messages import TextContent
from chagent.outputs import MAX_OUTPUT_BYTES, OutputService
from chagent.storage.repositories import RunRepository
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.tools.context import ToolContext
from chagent.tools.sandbox import Sandbox


# Open every component relative to a directory descriptor. No symlink can
# redirect a container path to the bind-mounted workspace between checks.
READ_FILE = r'''
import os, stat, sys
path = sys.argv[1]
parts = path.split('/')[1:]
fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
try:
    for part in parts[:-1]:
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd); fd = child
    file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 20 * 1024 * 1024:
            raise ValueError('source must be a regular file of at most 20 MiB')
        copied = 0
        while chunk := os.read(file_fd, 65536):
            copied += len(chunk)
            if copied > 20 * 1024 * 1024:
                raise ValueError('source grew beyond 20 MiB during read')
            sys.stdout.buffer.write(chunk)
    finally:
        os.close(file_fd)
finally:
    os.close(fd)
'''


def source_path(raw: str, run_id) -> str:
    path = PurePosixPath(raw)
    if not raw.startswith("/") or any(part in {"", ".", ".."} for part in raw.split("/")[1:]):
        raise ValueError("Container source must be a canonical absolute path")
    if path.name in {"", ".", ".."}:
        raise ValueError("Container source must name a file")
    scratch = PurePosixPath(f"/tmp/chagent-run-{run_id}")
    cache = PurePosixPath("/cache")
    if not (path.is_relative_to(scratch) and path != scratch or
            path.is_relative_to(cache) and path != cache):
        raise PermissionError("Source must be inside this run's scratch or /cache")
    return str(path)


class CollectFileTool(AgentTool):
    def __init__(self, sandbox: Sandbox, database):
        self.sandbox, self.database = sandbox, database
        super().__init__(name="collect_file", label="Collect container file",
            description="Copy one binary file from this run's /tmp/chagent-run-<run_id> or /cache into the workspace. "
                        "Files outside the workspace are not delivered automatically. Existing target names require "
                        "host confirmation; pending means not saved. Use the returned status before claiming delivery.",
            parameters={"type": "object", "properties": {
                "container_path": {"type": "string", "minLength": 1, "maxLength": 4096},
                "workspace_path": {"type": "string", "minLength": 1, "maxLength": 4096}},
                "required": ["container_path"], "additionalProperties": False},
            requires_workspace=True, requires_host_execution=True,
            mutates_workspace=True, execution_mode="sequential", replay="never")

    def execution_timeout(self, params):
        return 60.0

    async def _read(self, name: str, path: str) -> bytes:
        child = await asyncio.create_subprocess_exec("docker", "exec", "--workdir", "/tmp", name,
            "/app/.venv/bin/python", "-c", READ_FILE, path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            data, error = await asyncio.wait_for(child.communicate(), 40)
        except BaseException:
            child.kill()
            await child.communicate()
            raise
        if child.returncode:
            raise ValueError("Container file could not be read: " + error.decode(errors="replace")[-200:])
        if len(data) > MAX_OUTPUT_BYTES:
            raise ValueError("Container file exceeds 20 MiB")
        return data

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context: ToolContext | None = None):
        if context is None or context.run_id is None or context.session_id is None:
            raise PermissionError("Collecting a file requires a session and run")
        path = source_path(params["container_path"], context.run_id)
        target = params.get("workspace_path") or PurePosixPath(path).name
        async with self.sandbox.use(context.workspace):
            name = await self.sandbox.ensure_container(context.workspace)
            data = await self._read(name, path)
            async with self.database.connection() as connection:
                cursor = await connection.execute("SELECT workspace_id FROM sessions WHERE id=%s", (context.session_id,))
                session = await cursor.fetchone()
            if session is None:
                raise ValueError("Session does not exist")
            result = await OutputService(self.database).submit(
                session["workspace_id"], context.workspace, target, data,
                source="container:" + path, session_id=context.session_id,
                run_id=context.run_id, tool_call_id=tool_call_id,
                workspace_identity=context.workspace_identity)
        committed = result["status"] == "committed"
        details = {"status": result["status"], "output_id": str(result["id"]),
                   "container_path": path, "workspace_path": result["output_path"] if committed else target,
                   "size": len(data), "sha256": sha256(data).hexdigest()}
        await RunRepository(self.database).event(context.run_id,
            "file_collected" if committed else "file_collection_pending", details)
        message = ("Saved workspace file: " if committed else "Not saved; awaiting host collision confirmation: ")
        return AgentToolResult(content=[TextContent(message + details["workspace_path"])], details=details)
