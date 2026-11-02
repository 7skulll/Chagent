"""Small explicit builtin set, trimmed by the session mode.

Two faces, one rule: **a tool the mode does not allow is not registered.** The
model cannot call what is not in the registry, so the boundary is a property of
construction rather than of the prompt, the UI or the executor's mood.

Reading and writing the workspace are shared by both modes, because a mode is a
statement about *execution*, not about whether the agent may edit the user's
files at all. ``code`` adds the host-execution tools, and only exists when the
build-level switch ``CHAGENT_CODE_MODE`` is on.

A caller that must not let the model change the workspace says so with
``allow_workspace_writes=False``: the local web app does, because no approval
gate exists yet for file mutations performed on its behalf. The switch trims the
face at construction; it is not a runtime permission the model can argue with.
"""

from chagent.domain.sessions import validate_mode
from chagent.storage.artifacts import ArtifactRepository
from chagent.tools.command import ExecCommandTool
from chagent.tools.echo import EchoTool
from chagent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from chagent.tools.patch import ApplyPatchTool
from chagent.tools.processes import ProcessManager, ProcessTool
from chagent.tools.registry import ToolRegistry
from chagent.tools.search import GlobTool, GrepTool
from chagent.tools.tests_runner import RunTestsTool
from chagent.tools.artifacts import ReadArtifactTool
from chagent.tools.generated_files import CreateFileTool
from chagent.tools.sandbox import Sandbox
from chagent.tools.web import WebAccess, create_web_tools


#: Tools that change files in the workspace. Shared by both modes; what a
#: caller can withhold is the whole set, never a subset of it.
WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
#: Tools that run something the model chose on the host. Not the same set as
#: ``tools.policy.HOST_TOOLS``: that one is about which calls need a human, this
#: one is about which tools may exist at all.
EXECUTION_TOOLS = frozenset({"exec_command", "run_tests", "process"})


def create_builtin_registry(*, mode: str, allow_host_commands: bool = False,
                            allow_workspace_writes: bool = True,
                            artifacts: ArtifactRepository | None = None,
                            sandbox: Sandbox | None = None,
                            processes: ProcessManager | None = None,
                            web: WebAccess | None = None,
                            include_echo: bool = False) -> ToolRegistry:
    """One tool face. ``mode`` is required so no call site gets a face by accident.

    The second layer (``ToolExecutor``) is the registry itself: an executor built
    without ``write_file`` has no such entry, so a call naming it fails as an
    unknown tool even if it arrived through some other route.
    """
    mode = validate_mode(mode)
    registry = ToolRegistry()
    # Reads exist in both modes: they are what every session is for.
    tools = [ListDirTool(), ReadFileTool(), GlobTool(), GrepTool()]
    # Writes do too, unless this caller is one that cannot supervise them.
    readonly_code = mode == "code" and sandbox is not None and not sandbox.settings.writable_workspace
    if allow_workspace_writes and not readonly_code:
        tools += [WriteFileTool(), EditFileTool(), ApplyPatchTool()]
    # ``echo`` exists to verify the execution chain (demo runs, ``--tool``
    # probes, tests), not to do work. Registered by default it was a 201-byte
    # declaration the model could call every turn, so the live surface is the
    # real tools and demo paths opt in explicitly.
    if include_echo:
        tools.insert(0, EchoTool())
    for tool in tools:
        registry.register(tool)
    if mode == "code" and allow_host_commands:
        # Both run host processes; the executor enforces the same switch.
        registry.register(ExecCommandTool(sandbox, processes))
        registry.register(RunTestsTool(sandbox))
        if processes is not None:
            # Useless without a command to attach to, so it follows the same
            # switch instead of appearing in every session.
            registry.register(ProcessTool(processes, allowed_actions=("read", "stop") if readonly_code else None))
    if artifacts is not None:
        registry.register(ReadArtifactTool(artifacts))
        if mode == "work":
            registry.register(CreateFileTool(artifacts.database))
    if web is not None:
        # Outbound access is independent of host commands and of the mode: it
        # reads pages, changes nothing, and needs no shell. The keyless case
        # registers nothing at all.
        for tool in create_web_tools(web):
            registry.register(tool)
    return registry
