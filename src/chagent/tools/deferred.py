"""Loads a deferred tool's declaration on request.

Chat-completions providers cannot add a tool mid-conversation, so "loading" is
two things at once: this call returns the full declaration as readable text, and
the runner declares it on the next request. Both matter — the text tells the
model how to call it, the declaration lets it actually emit the call.

The query is matched against names and descriptions rather than being a lookup by
exact name, because a model that knows it needs to run a test suite may not know
the tool is called ``run_tests``.
"""

import asyncio
import json

from chagent.domain.messages import TextContent
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.tools.context import ToolContext
from chagent.tools.registry import ToolRegistry


#: Load at most this many tools for one query. The whole point is to spend less
#: than declaring everything, and a query like "file" could match the entire face.
MAX_RESULTS = 4

#: Terms shorter than this are ignored. Substring matching on one or two letters
#: matches almost every description ("a" is inside "unchanged"), which turns an
#: unrelated query into a load and spends exactly what deferral was saving.
MIN_TERM_CHARS = 3


class ToolSearchTool(AgentTool):
    def __init__(self, registry: ToolRegistry):
        super().__init__(
            name="tool_search", label="Load a tool", replay="safe", execution_mode="parallel",
            requires_run_context=True,
            description=(
                "Some tools are listed by name in the prompt but their parameters are not declared, "
                "to save context. Call this to load one: pass a capability, a tool name, or words from "
                "what you want to do (for example \"web search\", \"run tests\", \"start a server\"). "
                "The matching schemas are returned and become callable on your next turn. "
                "Tools already declared in this request do not need searching."
            ),
            parameters={"type": "object", "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200}},
                "required": ["query"], "additionalProperties": False})
        self.registry = registry

    def matching(self, query: str) -> list:
        """Deferred tools whose name or purpose matches, best match first."""
        terms = [term for term in _normalize(query).split() if len(term) >= MIN_TERM_CHARS]
        if not terms:
            return []
        scored = []
        for name in sorted(self.registry.deferred_names):
            tool = self.registry.get(name)
            haystack = _normalize(f"{name} {tool.description}")
            hits = sum(1 for term in terms if term in haystack)
            if not hits:
                continue
            # A name hit outranks a description hit: a model asking for
            # "run_tests" means that tool, not a tool that mentions tests.
            exact = 1 if _normalize(query) in _normalize(name) or _normalize(name) in _normalize(query) else 0
            scored.append((-exact, -hits, name, tool))
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        return [(name, tool) for _, _, name, tool in scored[:MAX_RESULTS]]

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *,
                      context: ToolContext | None = None):
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        query = params["query"]
        # A query that names a tool already declared in this request needs no
        # load: its schema is in the tool list the model is about to send.
        # Answering with an unrelated deferred tool that merely shares a word
        # (asking for "host_process" and getting "host_exec" because both say
        # "native") is worse than saying "you already have it" — measured in the
        # 2026-11-08 acceptance, where the model then reported the tool missing.
        normalized = _normalize(query)
        for tool in self.registry.definitions():
            if tool.name in self.registry.deferred_names:
                continue
            if _normalize(tool.name) in normalized:
                return AgentToolResult(content=[TextContent(
                    f"`{tool.name}` is already declared in the current request; call it directly. "
                    "Its parameters are in your tool list, so there is nothing to load.")])
        matches = self.matching(query)
        if not matches:
            available = ", ".join(name for name, _ in self.registry.deferred_index())
            return AgentToolResult(
                content=[TextContent(f"No loadable tool matches {query!r}. Available: {available}")],
                is_error=True)
        if context is None or context.deferred is None:
            raise PermissionError("Tool loading requires a run that declares tools on demand")
        loaded = context.deferred.names
        sections = []
        for name, tool in matches:
            loaded.add(name)
            declaration = tool.declaration()
            sections.append(
                f"## {name}\n{declaration.description}\n"
                f"parameters: {_json(declaration.parameters)}"
            )
        remaining = sorted(self.registry.deferred_names - loaded)
        tail = (f"\n\nNot loaded yet: {', '.join(remaining)}" if remaining
                else "\n\nEvery deferrable tool is now declared.")
        return AgentToolResult(content=[TextContent(
            "Loaded for the next request; call these directly from now on.\n\n"
            + "\n\n".join(sections) + tail
        )])


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("_", " ").replace("-", " ").split())


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
