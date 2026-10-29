"""Small explicit builtin set, with host commands enabled only by host settings."""

from chagent.tools.command import ExecCommandTool
from chagent.tools.echo import EchoTool
from chagent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from chagent.tools.patch import ApplyPatchTool
from chagent.tools.processes import ProcessManager, ProcessTool
from chagent.tools.registry import ToolRegistry
from chagent.tools.search import GlobTool, GrepTool
from chagent.tools.tests_runner import RunTestsTool
from chagent.tools.artifacts import ReadArtifactTool
from chagent.storage.artifacts import ArtifactRepository
from chagent.tools.sandbox import Sandbox


def create_builtin_registry(*, allow_host_commands: bool = False, artifacts: ArtifactRepository | None = None,
                            sandbox: Sandbox | None = None,
                            processes: ProcessManager | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (EchoTool(), ListDirTool(), ReadFileTool(), WriteFileTool(), EditFileTool(),
                 GlobTool(), GrepTool(), ApplyPatchTool()):
        registry.register(tool)
    if allow_host_commands:
        # Both run host processes; the executor enforces the same switch.
        registry.register(ExecCommandTool(sandbox, processes))
        registry.register(RunTestsTool(sandbox))
        if processes is not None:
            # Useless without a command to attach to, so it follows the same
            # switch instead of appearing in every session.
            registry.register(ProcessTool(processes))
    if artifacts is not None:
        registry.register(ReadArtifactTool(artifacts))
    return registry
