"""Small explicit builtin set, trimmed by the session mode.

Two faces, one rule: **a tool the mode does not allow is not registered.** The
model cannot call what is not in the registry, so the boundary is a property of
construction rather than of the prompt, the UI or the executor's mood.

Reading and writing the workspace are shared by both modes, because a mode is a
statement about *execution*, not about whether the agent may edit the user's
files at all. ``code`` adds the host-execution tools, and only exists when the
build-level switch ``CHAGENT_CODE_MODE`` is on.

A caller that must not let the model change the workspace says so with
``allow_workspace_writes=False``. Explicit read-only code uses this; ordinary
web work and development code enable file writes independently of per-call approval. The switch trims the
face at construction; it is not a runtime permission the model can argue with.
"""

from chagent.domain.sessions import validate_mode
from chagent.storage.artifacts import ArtifactRepository
from chagent.tools.command import ExecCommandTool
from chagent.tools.echo import EchoTool
from chagent.tools.filesystem import CreateDirectoryTool, EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from chagent.tools.patch import ApplyPatchTool
from chagent.tools.processes import ProcessManager, ProcessTool
from chagent.tools.registry import ToolRegistry
from chagent.tools.search import GlobTool, GrepTool
from chagent.tools.tests_runner import RunTestsTool
from chagent.tools.artifacts import ReadArtifactTool
from chagent.tools.generated_files import CreateFileTool
from chagent.tools.sandbox import Sandbox
from chagent.tools.web import WebAccess, create_web_tools
from chagent.tools.host import HostExecTool
from chagent.hostexec.native import NativeHostExecTool, NativeHostProcessTool
from chagent.tools.site import SiteTool
from chagent.tools.collect_file import CollectFileTool
from chagent.tools.task_history import TaskHistoryTool
from chagent.tools.skills import ReadSkillTool
from chagent.tools.run_diagnostics import RunDiagnosticsTool
from chagent.storage.run_diagnostics import RunDiagnosticsRepository


#: Tools that change files in the workspace. Shared by both modes; what a
#: caller can withhold is the whole set, never a subset of it.
WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "create_directory"})
#: Tools that run something the model chose on the host. Not the same set as
#: ``tools.policy.HOST_TOOLS``: that one is about which calls need a human, this
#: one is about which tools may exist at all.
EXECUTION_TOOLS = frozenset({"exec_command", "run_tests", "process"})

#: Tools whose declaration is indexed rather than sent, when deferral is on.
#: Chosen by measured cost and by when a session needs them: together they are
#: 1663 of the 3923 declaration tokens, and every one of them is something a
#: session reaches for late or never (a web lookup, a test run, a long-lived
#: process, a downloadable file, host execution).
#:
#: Deliberately **not** deferred: the read/write/edit/patch loop, directory
#: listing, glob, grep, command execution and artifact pagination. Those are what
#: the first three turns of every session use, and one extra `tool_search` turn
#: costs more than the declaration it saves.
DEFERRABLE_TOOLS = frozenset({"web_search", "web_fetch", "host_exec", "process", "run_tests", "create_file",
                              "search_task_history", "read_task_history", "list_recent_runs", "inspect_run"})

#: Below this many deferrable tools the index and `tool_search` cost about what
#: they save, so the face is declared whole.
MIN_DEFERRED_TOOLS = 3


def create_builtin_registry(*, mode: str, allow_host_commands: bool = False,
                            allow_workspace_writes: bool = True,
                            unified: bool = False,
                            containerized: bool = False,
                            artifacts: ArtifactRepository | None = None,
                            sandbox: Sandbox | None = None,
                            processes: ProcessManager | None = None,
                            web: WebAccess | None = None,
                            include_echo: bool = False,
                            process_stdin: bool = True,
                            versioned_files: bool = False,
                            host_exec: tuple[str, str] | None = None,
                            native_host_exec: bool = False,
                            defer_tools: bool = False,
                            history_tools: bool = True,
                            include_skills: bool = True) -> ToolRegistry:
    """One tool face. ``mode`` is required so no call site gets a face by accident.

    The second layer (``ToolExecutor``) is the registry itself: an executor built
    without ``write_file`` has no such entry, so a call naming it fails as an
    unknown tool even if it arrived through some other route.
    """
    mode = validate_mode(mode)
    if unified and (sandbox is None or not containerized or not sandbox.settings.allows_execution):
        raise ValueError("Unified tools require a writable container boundary")
    if unified and (not allow_host_commands or not allow_workspace_writes):
        raise ValueError("Unified tools require host commands and workspace writes")
    registry = ToolRegistry()
    # Reads exist in both modes: they are what every session is for.
    tools = [ListDirTool(), ReadFileTool(), GlobTool(), GrepTool()]
    if include_skills:
        tools.append(ReadSkillTool())
    # Writes do too, unless this caller is one that cannot supervise them.
    readonly_code = mode == "code" and sandbox is not None and not sandbox.settings.writable_workspace
    if allow_workspace_writes and not readonly_code:
        tools += [WriteFileTool(), EditFileTool(), ApplyPatchTool(), CreateDirectoryTool()]
    # ``echo`` exists to verify the execution chain (demo runs, ``--tool``
    # probes, tests), not to do work. Registered by default it was a 201-byte
    # declaration the model could call every turn, so the live surface is the
    # real tools and demo paths opt in explicitly.
    if include_echo:
        tools.insert(0, EchoTool())
    for tool in tools:
        if tool.name == "read_file":
            tool.database = artifacts.database if artifacts is not None else None
        if tool.name in WRITE_TOOLS:
            tool.versioned_files = versioned_files
            tool.database = artifacts.database if artifacts is not None else None
            if versioned_files and tool.name != "create_directory":
                tool.description += " Existing files require the latest read SHA256: expected_sha256 for write/edit, versions={relative_path: sha256} for apply_patch."
                if tool.name == "edit_file":
                    tool.parameters["required"].append("expected_sha256")
                if tool.name == "apply_patch":
                    tool.parameters["properties"]["versions"] = {"type":"object", "additionalProperties":{"type":"string","pattern":"^[0-9a-f]{64}$"}}
        registry.register(tool)
    if (mode == "code" or unified) and allow_host_commands:
        # Both run host processes; the executor enforces the same switch.
        registry.register(ExecCommandTool(sandbox, processes, process_stdin=process_stdin))
        registry.register(RunTestsTool(sandbox))
        if processes is not None and not (sandbox is not None and sandbox.settings.host_controller):
            # Useless without a command to attach to, so it follows the same
            # switch instead of appearing in every session.
            registry.register(ProcessTool(processes, allowed_actions=("read", "stop") if readonly_code or not process_stdin else None))
        if host_exec is not None and containerized:
            registry.register(HostExecTool(*host_exec, artifacts.database if artifacts is not None else None))
        if native_host_exec:
            if sandbox is None or not sandbox.settings.host_controller or artifacts is None:
                raise ValueError("Native host execution requires a host controller and database")
            if processes is None:
                raise ValueError("Native host execution requires a process manager")
            # In the host-controller build the manager holds native children only:
            # container exec is one-shot and the container `process` tool is absent.
            registry.register(NativeHostExecTool(artifacts.database, processes))
            registry.register(NativeHostProcessTool(artifacts.database, processes))
        if sandbox is not None and sandbox.settings.host_controller and artifacts is not None:
            registry.register(SiteTool(sandbox, artifacts.database))
            registry.register(CollectFileTool(sandbox, artifacts.database))
    if artifacts is not None:
        registry.register(ReadArtifactTool(artifacts))
        if history_tools:
            diagnostics = RunDiagnosticsRepository(artifacts.database)
            registry.register(RunDiagnosticsTool(diagnostics))
            registry.register(RunDiagnosticsTool(diagnostics, detail=True))
            registry.register(TaskHistoryTool(artifacts.database))
            registry.register(TaskHistoryTool(artifacts.database, detail=True))
        if allow_workspace_writes and not readonly_code:
            registry.register(CreateFileTool(artifacts.database))
    if web is not None:
        # Outbound access is independent of host commands and of the mode: it
        # reads pages, changes nothing, and needs no shell. The keyless case
        # registers nothing at all.
        for tool in create_web_tools(web):
            registry.register(tool)
    if defer_tools:
        # One decision, after everything is registered: defer the whole set or
        # none of it. A half-deferred face would pay for `tool_search` without
        # getting its savings.
        candidates = sorted(DEFERRABLE_TOOLS & {tool.name for tool in registry.definitions()})
        if len(candidates) >= MIN_DEFERRED_TOOLS:
            registry.defer(candidates)
            from chagent.tools.deferred import ToolSearchTool
            registry.register(ToolSearchTool(registry))
    return registry
