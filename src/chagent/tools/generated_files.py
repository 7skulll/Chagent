"""Create session-owned text downloads without accessing the workspace."""
import asyncio
from pathlib import PurePath
from uuid import uuid4

from chagent.domain.messages import TextContent
from chagent.tools.base import AgentTool, AgentToolResult


class CreateFileTool(AgentTool):
    def __init__(self, database):
        super().__init__(name="create_file", label="Create downloadable file", requires_workspace=True,
            description="Create a downloadable text file for this conversation when the user requests a file. "
                        "Provide the complete content and a meaningful filename ending in .md, .txt, .csv or .json. "
                        "This saves a conversation artifact, not a workspace file. Never claim a file exists unless this tool succeeds.",
            parameters={"type": "object", "properties": {
                "filename": {"type": "string", "minLength": 1, "maxLength": 160},
                "content": {"type": "string", "maxLength": 200000}},
                "required": ["filename", "content"], "additionalProperties": False})
        self.database = database

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context=None):
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        if context is None or context.session_id is None or context.run_id is None:
            raise PermissionError("File creation requires a session and run")
        name = params["filename"].strip()
        if (not name or name in {".", ".."} or "/" in name or "\\" in name or
                any(ord(ch) < 32 or ord(ch) == 127 for ch in name) or
                PurePath(name).suffix.lower() not in {".md", ".txt", ".csv", ".json"}):
            raise ValueError("Filename must be a plain .md, .txt, .csv or .json filename")
        content = params["content"]
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "INSERT INTO generated_files (id, session_id, run_id, tool_call_id, name, content) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (run_id, tool_call_id) "
                "DO UPDATE SET tool_call_id = EXCLUDED.tool_call_id "
                "RETURNING id, name, content", (uuid4(), context.session_id, context.run_id, tool_call_id, name, content))
            row = await cursor.fetchone()
            if row["name"] != name or row["content"] != content:
                raise ValueError("Tool call already created a different file")
        return AgentToolResult(content=[TextContent(f"Created downloadable file: {name}")],
            details={"generated_file": {"id": str(row["id"]), "name": name, "size": len(content.encode('utf-8'))}})
