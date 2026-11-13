"""What each tool is, as far as replay is concerned.

The product already declares most of this: ``AgentTool`` carries ``replay``,
``mutates_workspace``, ``requires_workspace`` and ``requires_host_execution``.
Reading those declarations is why this module builds a real registry instead of
listing tool names by hand — a new write tool is then classified without anyone
editing the evaluator.

Two things the declarations do not answer, and the reason this module exists:

**``replay="safe"`` is not replay eligibility.** ``web_fetch`` declares it, and
re-fetching a page during a replay is exactly the host side effect the plan
forbids. The declaration answers "may this be asked again"; eligibility answers
"can this run offline, against a fixture, with no external state". They are
different questions, so the network/service/host set is explicit below.

**A tool that cannot be constructed here has no declaration.** ``exec_command``,
``site`` and the history tools need a sandbox, a database or a container to even
exist. They are named in a fixed set with the missing dependency written down,
so an unknown future tool is refused rather than silently treated as harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from chagent.tools.builtin import EXECUTION_TOOLS, create_builtin_registry
from chagent.tools.policy import HOST_TOOLS

#: Tools whose execution reaches outside the process: the host, a network, a
#: container service or a long-lived process. Refused for replay, not simulated.
EXTERNAL_TOOLS: dict[str, str] = {
    **{name: "runs a process on the host" for name in EXECUTION_TOOLS},
    **{name: "runs a process on the host" for name in HOST_TOOLS},
    "site": "starts or probes a local web service",
    "collect_file": "reads a file out of the container",
    "web_search": "reaches the network",
    "web_fetch": "reaches the network",
}

#: Tools whose initial state is rows, not files. A fixture cannot rebuild them,
#: so a recorded call is refused instead of replayed against an empty database.
DATABASE_TOOLS: dict[str, str] = {
    "read_artifact": "reads a stored tool output row",
    "create_file": "writes a workspace file and an output row",
    "list_recent_runs": "reads run rows",
    "inspect_run": "reads run rows",
    "search_task_history": "reads task rows",
    "read_task_history": "reads task rows",
}

#: Tools that mutate run state rather than files. Replaying them would claim the
#: registry change happened without a run to hold it.
CONTEXT_TOOLS: dict[str, str] = {
    "tool_search": "loads declarations into a run's registry",
}


@dataclass(frozen=True)
class ToolFacts:
    name: str
    declared_replay: str
    mutates_workspace: bool
    read_only: bool
    requires_workspace: bool
    requires_host_execution: bool


@lru_cache(maxsize=1)
def registry():
    """A tool face that needs no database, network or container.

    ``work`` mode is the smallest real face: reads, writes, skills and no host
    execution, which is what a replay process must not have anyway. Tools that
    need a service are absent by construction, which is why they are named above
    instead of being quietly reported as unknown. ``echo`` is included because
    demo and verification runs record it, and its whole contract is "return the
    text" — the one tool a replay can decide without any state at all.
    """
    return create_builtin_registry(mode="work", include_echo=True)


def facts(name: str) -> ToolFacts | None:
    tool = registry().get(name)
    if tool is None:
        return None
    return ToolFacts(
        name=name,
        declared_replay=getattr(tool, "replay", "never"),
        mutates_workspace=bool(getattr(tool, "mutates_workspace", False)),
        read_only=not bool(getattr(tool, "mutates_workspace", False)),
        requires_workspace=bool(getattr(tool, "requires_workspace", False)),
        requires_host_execution=bool(getattr(tool, "requires_host_execution", False)),
    )


def external_reason(name: str) -> str | None:
    return EXTERNAL_TOOLS.get(name)


def database_reason(name: str) -> str | None:
    return DATABASE_TOOLS.get(name)


def context_reason(name: str) -> str | None:
    return CONTEXT_TOOLS.get(name)


def is_stateful(name: str) -> bool:
    """Eligible for the isolated stateful tier: a file tool in the workspace."""
    tool = facts(name)
    return tool is not None and tool.requires_workspace and not tool.requires_host_execution


def tool_object(name: str):
    return registry().get(name)


__all__ = [
    "CONTEXT_TOOLS", "DATABASE_TOOLS", "EXTERNAL_TOOLS", "ToolFacts",
    "context_reason", "database_reason", "external_reason", "facts", "is_stateful",
    "registry", "tool_object",
]
