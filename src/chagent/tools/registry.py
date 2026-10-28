"""Explicit registration, with no filesystem discovery or implicit plugins."""

from jsonschema import Draft202012Validator

from chagent.domain.tools import Tool
from chagent.tools.base import AgentTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def register(self, tool: AgentTool) -> None:
        if not tool.name or tool.name in self._tools:
            raise ValueError(f"Empty or duplicate tool name: {tool.name!r}")
        Draft202012Validator.check_schema(tool.parameters)
        if tool.output_schema is not None:
            Draft202012Validator.check_schema(tool.output_schema)
        self._tools[tool.name] = tool
        self._revision += 1

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def definitions(self) -> list[Tool]:
        return [self._tools[name].declaration() for name in sorted(self._tools)]
