"""Session-scoped paginated reads for saved tool output."""

import asyncio
from uuid import UUID

from chagent.domain.messages import TextContent
from chagent.storage.artifacts import ArtifactRepository
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.tools.context import ToolContext


class ReadArtifactTool(AgentTool):
    def __init__(self, artifacts: ArtifactRepository):
        super().__init__(name="read_artifact", label="Read saved tool output", replay="safe", requires_workspace=True,
                         execution_mode="parallel",
                         description="Read an immutable tool-output artifact owned by this session, in character pages.",
                         parameters={"type": "object", "properties": {
                             "artifact_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
                             "required": ["artifact_id"], "additionalProperties": False})
        self.artifacts = artifacts

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context: ToolContext | None = None):
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        if context is None or context.session_id is None:
            raise PermissionError("Artifact reads require an owning session")
        page = await self.artifacts.read(context.session_id, UUID(params["artifact_id"]),
                                         offset=params.get("offset", 0), limit=params.get("limit", 1000))
        return AgentToolResult(content=[TextContent(
            f"Artifact: {page['id']}\nOffset: {page['offset']}\nTotal characters: {page['char_count']}\n"
            f"SHA256: {page['sha256']}\nSource truncated: {page['source_truncated']}\n"
            f"Next offset: {page['next_offset']}\n{page['text']}"
        )])
