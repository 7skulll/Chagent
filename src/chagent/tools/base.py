"""Executable tool contracts adapted from Pi's AgentTool types."""

from abc import ABC, abstractmethod
from asyncio import Event
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, TypeAlias

from chagent.domain.messages import ImageContent, JsonObject, JsonValue, TextContent
from chagent.domain.tools import Tool
from chagent.domain.usage import Usage
from chagent.tools.context import ToolContext


ToolExecutionMode: TypeAlias = Literal["sequential", "parallel"]


@dataclass(slots=True)
class AgentToolResult:
    """A final or partial result; separate from its persisted message.

    Only content is model-facing. Structured content and details are for
    programmatic callers and UI. Usage belongs to the tool itself.
    """

    content: list[TextContent | ImageContent]
    details: JsonValue = None
    structured_content: JsonValue = None
    usage: Usage | None = None
    is_error: bool = False
    terminate: bool = False
    outcome_unknown: bool = False


AgentToolUpdateCallback: TypeAlias = Callable[[AgentToolResult], None]


@dataclass(slots=True)
class AgentTool(Tool, ABC):
    """Executable tool; a set signal requests cooperative cancellation.

    Implementations must honor cancellation and either raise on failure or
    return is_error=True. on_update is scoped to this execute invocation.
    A replay policy is metadata, not a guarantee of safe recovery.
    """

    label: str
    output_schema: JsonObject | None = None
    replay: Literal["never", "safe"] = "never"
    execution_mode: ToolExecutionMode = "sequential"
    requires_workspace: bool = False
    requires_host_execution: bool = False

    @abstractmethod
    async def execute(
        self,
        tool_call_id: str,
        params: JsonObject,
        signal: Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
        *,
        context: ToolContext | None = None,
    ) -> AgentToolResult:
        """Execute validated parameters; runtime owns validation and timeouts."""
        ...

    def declaration(self) -> Tool:
        """Expose only model-visible metadata, never an execution callback."""
        return Tool(self.name, self.description, deepcopy(self.parameters))
