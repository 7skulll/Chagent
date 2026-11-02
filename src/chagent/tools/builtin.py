"""Small explicit builtin set, trimmed by the session mode.

Two faces, one rule: **a tool the mode does not allow is not registered.** The
model cannot call what is not in the registry, so the boundary is a property of
construction rather than of the prompt, the UI or the executor's mood.

``work`` reads the workspace and web, and can create session-owned downloadable
text artifacts in the database; it cannot modify the workspace. ``code`` adds the
write and host-execution tools, and only exists when the build-level switch
``CHAGENT_CODE_MODE`` is on.
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


#: Tools that only make sense when a run may change the workspace. Kept next to
#: the registry so the assertion in tests and the construction share one list.
WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
#: Tools that run something the model chose on the host. Not the same set as
#: ``tools.policy.HOST_TOOLS``: that one is about which calls need a human, this
#: one is about which tools may exist at all.
EXECUTION_TOOLS = frozenset({"exec_command", "run_tests", "process"})


def create_builtin_registry(*, mode: str, allow_host_commands: bool = False,
                            artifacts: ArtifactRepository | None = None,
                            sandbox: Sandbox | None = None,
                            processes: ProcessManager | None = None,
                            web: WebAccess | None = None,
                            include_echo: bool = False) -> ToolRegistry:
    """One tool face. ``mode`` is required so no call site gets a face by accident.

    The second layer (``ToolExecutor``) is the registry itself: an executor built
    for ``work`` has no ``write_file`` entry, so a call naming it fails as an
    unknown tool even if it arrived through some other route.
    """
    mode = validate_mode(mode)
    registry = ToolRegistry()
    # Reads exist in both modes: they are what ``work`` is for.
    tools = [ListDirTool(), ReadFileTool(), GlobTool(), GrepTool()]
    if mode == "code":
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
            registry.register(ProcessTool(processes))
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
