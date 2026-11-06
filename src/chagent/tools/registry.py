"""Explicit registration, with no filesystem discovery or implicit plugins."""

from collections.abc import Collection, Iterable
from jsonschema import Draft202012Validator

from chagent.domain.tools import Tool
from chagent.tools.base import AgentTool


#: One index line may not spend more than this on a tool nobody has loaded yet.
INDEX_DESCRIPTION_CHARS = 90


def index_line(description: str) -> str:
    """The first line of a description, cut to a bounded length.

    The index exists so the model knows a tool exists, not so it knows how to
    call it: the schema arrives through ``tool_search``. Keeping it to one short
    line is what makes deferring the tool cheaper than declaring it.
    """
    line = " ".join((description or "").strip().splitlines()[0].split()) if description else ""
    if len(line) <= INDEX_DESCRIPTION_CHARS:
        return line
    return line[:INDEX_DESCRIPTION_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:") + " …"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}
        self._deferred: set[str] = set()
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

    def defer(self, names: Iterable[str]) -> None:
        """Index these tools instead of declaring them on every request.

        A deferred tool is still registered and still executable — what changes is
        that its JSON Schema is not sent until the model asks for it. One batch
        decision per face, taken after registration, so no call site has to know
        which tools are worth deferring.
        """
        selected = {name for name in names if name in self._tools}
        unknown = set(names) - selected
        if unknown:
            raise ValueError(f"Unknown tools cannot be deferred: {sorted(unknown)}")
        if selected - self._deferred:
            self._deferred |= selected
            self._revision += 1

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def definitions(self) -> list[Tool]:
        """Every declaration, deferred or not. Inspection paths use this."""
        return [self._tools[name].declaration() for name in sorted(self._tools)]

    def declarations(self, loaded: Collection[str] = ()) -> list[Tool]:
        """What this request declares: the core face plus what has been loaded."""
        return [self._tools[name].declaration() for name in sorted(self._tools)
                if name not in self._deferred or name in loaded]

    def deferred_index(self) -> list[tuple[str, str]]:
        """Name and one-line purpose of every deferred tool, byte-stable per run.

        Deliberately independent of what has been loaded: the index stays put for
        the whole run, so loading a tool costs one prompt-prefix change (the
        declaration appearing) instead of two.
        """
        return [(name, index_line(self._tools[name].description)) for name in sorted(self._deferred)]

    @property
    def deferred_names(self) -> frozenset[str]:
        return frozenset(self._deferred)
