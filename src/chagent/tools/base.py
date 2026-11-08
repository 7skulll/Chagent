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
    # A successful call means the workspace changed, so earlier identical calls
    # are no longer the same attempt (see tools/progress.py). Declared by the
    # tool, never inferred from its name.
    mutates_workspace: bool = False

    def execution_timeout(self, params: JsonObject) -> float | None:
        """Requested execution budget, including cleanup time."""
        return None

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

    def prepare_arguments(self, params: JsonObject) -> JsonObject:
        """Repair provider-specific argument shapes before schema validation.

        Models disagree about nesting and key spelling for the same intent; a
        strict schema turns that into a failed turn. A repairer may only
        normalize the shape of what was sent, never invent content, and the
        result still has to satisfy ``parameters``.
        """
        return params

    def declaration(self) -> Tool:
        """Expose only model-visible metadata, never an execution callback."""
        bounds = bounds_note(self.parameters)
        return Tool(self.name, f"{self.description} {bounds}" if bounds else self.description,
                    deepcopy(self.parameters))


def bounds_note(parameters: JsonObject) -> str:
    """Render numeric bounds inside the description, once, for every tool.

    Measured: a model asked for ``timeout_ms: 60000`` when the schema capped it
    at 30000 and spent a whole turn on the rejection. The schema was right; the
    declaration was incomplete, because a model can only learn a bound by
    failing unless it is written where the model reads. Doing it here rather than
    in each tool keeps the two from drifting apart.
    """
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    if not isinstance(properties, dict):
        return ""
    parts = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        low, high = spec.get("minimum"), spec.get("maximum")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            parts.append(f"{name} {low}-{high}")
        elif isinstance(high, (int, float)):
            parts.append(f"{name}<={high}")
        elif isinstance(low, (int, float)):
            parts.append(f"{name}>={low}")
    # Grammar is deliberately terse: this text repeats in every request of every
    # run, so it is budget, not documentation. ASCII only — “–” costs three bytes.
    return "Bounds: " + ", ".join(parts) + "." if parts else ""
