"""Single-invocation validation, permissions and bounded execution time."""

import asyncio
from copy import deepcopy
import math
import os

from jsonschema import Draft202012Validator

from chagent.domain.messages import TextContent, ToolCall
from chagent.tools.base import AgentToolResult, AgentToolUpdateCallback
from chagent.tools.context import ToolContext
from chagent.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, timeout: float = 35, *, allow_host_commands: bool = False):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Tool timeout must be finite and positive")
        self.ceiling = float(os.environ.get("CHAGENT_TOOL_TIMEOUT_CEILING", "310"))
        if not math.isfinite(self.ceiling) or self.ceiling <= 0:
            raise ValueError("Tool timeout ceiling must be finite and positive")
        self.registry = registry
        self.timeout = timeout
        self.allow_host_commands = allow_host_commands

    async def execute(self, call: ToolCall, *, context: ToolContext | None = None,
                      signal: asyncio.Event | None = None, on_update: AgentToolUpdateCallback | None = None) -> AgentToolResult:
        execution_started = False
        effective_timeout = self.timeout
        try:
            tool = self.registry.get(call.name)
            if tool is None:
                raise ValueError(f"Unknown tool: {call.name}")
            if call.arguments_error:
                raise ValueError(call.arguments_error)
            if not isinstance(call.arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
            from chagent.domain.json_types import postgres_json_safe
            if postgres_json_safe(call.arguments) != call.arguments:
                raise ValueError("Tool arguments contain a NUL character")
            arguments = tool.prepare_arguments(call.arguments)
            Draft202012Validator(tool.parameters).validate(arguments)
            if tool.requires_workspace and context is None:
                raise PermissionError("This tool requires a session workspace")
            if tool.requires_workspace and context is not None:
                from chagent.tools.workspace import WorkspaceFS
                WorkspaceFS(context.workspace, expected_identity=context.workspace_identity)
            if tool.requires_host_execution and not self.allow_host_commands:
                raise PermissionError("Host command execution is disabled")
            if signal is not None and signal.is_set():
                raise asyncio.CancelledError()
            requested = tool.execution_timeout(arguments)
            if requested is not None and (not math.isfinite(requested) or requested <= 0):
                raise ValueError("Tool requested an invalid timeout")
            effective_timeout = min(self.ceiling, max(self.timeout, requested or self.timeout))
            async with asyncio.timeout(effective_timeout):
                execution_started = True
                if tool.requires_workspace or tool.requires_run_context:
                    result = await tool.execute(call.id, deepcopy(arguments), signal=signal,
                                                on_update=on_update, context=context)
                else:
                    result = await tool.execute(call.id, deepcopy(arguments), signal=signal, on_update=on_update)
            if not isinstance(result, AgentToolResult):
                raise TypeError("Tool returned an invalid result")
            if result.structured_content is not None and tool.output_schema is not None:
                Draft202012Validator(tool.output_schema).validate(result.structured_content)
            return result
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return AgentToolResult(content=[TextContent(f"Tool did not return within {effective_timeout:g} seconds (execution timed out)")], is_error=True,
                                   outcome_unknown=execution_started)
        except Exception as error:
            return AgentToolResult(
                content=[TextContent(f"{type(error).__name__}: {error}")], is_error=True,
            )
