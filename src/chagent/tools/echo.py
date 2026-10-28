"""No-side-effect tool used to verify the complete execution chain."""

import asyncio

from chagent.domain.messages import JsonObject, TextContent
from chagent.tools.base import AgentTool, AgentToolResult, AgentToolUpdateCallback
from chagent.tools.context import ToolContext


class EchoTool(AgentTool):
    def __init__(self) -> None:
        super().__init__(
            name="echo", label="Echo", description="Return the supplied text unchanged.",
            parameters={
                "type": "object", "properties": {"text": {"type": "string"}},
                "required": ["text"], "additionalProperties": False,
            }, replay="safe",
        )

    async def execute(
        self, tool_call_id: str, params: JsonObject,
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
        *, context: ToolContext | None = None,
    ) -> AgentToolResult:
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        return AgentToolResult(content=[TextContent(text=params["text"])])
