"""One-shot POSIX host commands, or a session that outlives the call; cwd is scoped."""

import asyncio
from contextlib import suppress
import os
import signal as signals
from time import monotonic

from chagent.domain.messages import JsonObject, TextContent
from chagent.tools.base import AgentTool, AgentToolResult, AgentToolUpdateCallback
from chagent.tools.context import ToolContext
from chagent.tools.processes import PAGE_CHARS, ProcessManager
from chagent.tools.sandbox import Sandbox
from chagent.tools.workspace import WorkspaceFS


class ExecCommandTool(AgentTool):
    def __init__(self, sandbox: Sandbox | None = None, processes: ProcessManager | None = None):
        self.sandbox = sandbox
        # Session processes are opt-in per call (``yield_time_ms``); without a
        # manager the one-shot behaviour is unchanged and a session request is
        # refused rather than silently downgraded.
        self.processes = processes
        confined = sandbox is not None and sandbox.enabled
        readonly = confined and not sandbox.settings.writable_workspace
        super().__init__(
            name="exec_command", label="Run command",
            description=(
                "Run one /bin/sh command on the host inside the platform sandbox: writes stay in the "
                "workspace and the user's temp directory, network is off unless the host allowed it, and "
                "configured credential paths are unreadable. In isolated mode, reads are restricted to "
                "the workspace and declared runtime, HOME/tmp are private and the environment is cleared. Output is bounded and exit status is "
                "reported. Pass yield_time_ms to keep a longer command alive: the call then returns a "
                "process_id and the output so far, and process() manages it within host policy."
                if confined else
                "Run one /bin/sh command on the host. This is not sandboxed. Output is bounded and exit "
                "status is reported. Pass yield_time_ms to keep a longer command alive: the call then "
                "returns a process_id and the output so far, and process() reads, feeds or stops it."
            ),
            parameters={
                "type": "object", "properties": {
                    "cmd": {"type": "string", "minLength": 1, "maxLength": 32_768},
                    "workdir": {"type": "string", "minLength": 1},
                    # One-shot invocation budget; ignored when yielding, where
                    # CHAGENT_PROCESS_TIMEOUT bounds the whole process instead.
                    "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 30_000},
                    "yield_time_ms": {"type": "integer", "minimum": 100, "maximum": 30_000},
                    "max_output_chars": {"type": "integer", "minimum": 1, "maximum": 64_000},
                }, "required": ["cmd"], "additionalProperties": False,
            }, requires_workspace=True, requires_host_execution=True,
        )
        if readonly:
            self.description = (
                "Run a command with a read-only workspace under the platform sandbox. "
                "Private scratch may be written. Commands cannot modify workspace files. "
                "In isolated mode, reads are allowlisted and command network is off. "
                "Use yield_time_ms for a long command; process() only reads output or stops it. "
                "Interactive stdin/EOF is disabled. Configure tool caches explicitly in scratch."
            )

    def execution_timeout(self, params: JsonObject) -> float:
        return params.get("yield_time_ms", params.get("timeout_ms", 10_000)) / 1000 + 10

    async def execute(
        self, tool_call_id: str, params: JsonObject, signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None, *, context: ToolContext | None = None,
    ) -> AgentToolResult:
        if context is None:
            raise PermissionError("A session workspace is required")
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        filesystem = WorkspaceFS(context.workspace, expected_identity=context.workspace_identity)
        parts = filesystem.parts(params.get("workdir", "."))
        # Validate directory components without following symlinks.
        with filesystem.directory(parts):
            pass
        cwd = filesystem.root.joinpath(*parts)
        # The profile is built from the resolved root, which is what the kernel
        # matches on; a symlinked workspace (/tmp -> /private/tmp) would
        # otherwise produce a profile that denies even its own directory.
        argv, sandbox_record = ["/bin/sh", "-c", params["cmd"]], {}
        if self.sandbox is not None:
            argv, sandbox_record = await self.sandbox.wrap(argv, filesystem.root, owner=context.run_id)
        child_env = self.sandbox.environment(filesystem.root, owner=context.run_id) if self.sandbox is not None else None
        started = monotonic()
        if "yield_time_ms" in params:
            return await self._session(params, context, cwd, argv, sandbox_record, started, child_env)

        # Shield spawn so cancellation cannot lose a successfully started child.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=child_env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        ))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signals.SIGKILL)
            await process.communicate()
            raise

        output = bytearray()
        limit = params.get("max_output_chars", 1_048_576)
        # A byte cap is also a conservative bound on the decoded character count.
        truncated = False

        async def capture():
            nonlocal truncated
            while chunk := await process.stdout.read(4096):
                room = max(0, limit - len(output))
                output.extend(chunk[:room])
                truncated = truncated or len(chunk) > room
                if on_update is not None and room:
                    on_update(AgentToolResult(content=[TextContent(chunk[:room].decode("utf-8", errors="replace"))]))

        waiter = asyncio.create_task(process.wait())
        reader = asyncio.create_task(capture())
        complete = asyncio.gather(waiter, reader)
        timed_out = False
        cancellation = asyncio.create_task(signal.wait()) if signal is not None else None
        try:
            if cancellation is None:
                await asyncio.wait_for(asyncio.shield(complete), params.get("timeout_ms", 10_000) / 1000)
            else:
                done, _ = await asyncio.wait(
                    {complete, cancellation}, timeout=params.get("timeout_ms", 10_000) / 1000,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancellation in done:
                    raise asyncio.CancelledError()
                if complete not in done:
                    raise TimeoutError()
                await complete
        except TimeoutError:
            timed_out = True
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signals.SIGKILL)
            await complete
        except BaseException:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signals.SIGKILL)
            await asyncio.gather(waiter, reader, return_exceptions=True)
            raise
        finally:
            # This is a one-shot tool; descendants must not outlive the invocation.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signals.SIGKILL)
            if cancellation is not None:
                cancellation.cancel()
                with suppress(asyncio.CancelledError):
                    await cancellation
        text = output.decode("utf-8", errors="replace")
        if truncated:
            text += "\n[Output truncated]"
        if timed_out:
            text += "\n[Command timed out; process group terminated]"
        details = {
            "output": text, "exit_code": process.returncode,
            "wall_time_seconds": monotonic() - started, "workdir": str(cwd),
            "truncated": truncated, "timed_out": timed_out,
            **({"sandbox": sandbox_record} if sandbox_record else {}),
        }
        return AgentToolResult(
            content=[TextContent(f"Exit code: {process.returncode}\n{text}")], details=details,
            is_error=timed_out or process.returncode != 0, outcome_unknown=timed_out,
        )

    async def _session(self, params, context, cwd, argv, sandbox_record, started, child_env) -> AgentToolResult:
        """Start the command and return once ``yield_time_ms`` has passed.

        The child is owned by the run, not by this invocation: leaving it alive
        is the whole point, so nothing here kills the process group. Cancellation
        of this call therefore only stops the wait; run end reclaims the child.
        """
        if self.processes is None:
            raise ValueError("Session processes are unavailable: no process manager is configured")
        if context.run_id is None:
            raise PermissionError("Session processes belong to a run")
        record = await self.processes.start(
            run_id=context.run_id, command=params["cmd"], cwd=cwd, argv=argv,
            sandbox=sandbox_record, env=child_env, window_chars=params.get("max_output_chars"),
        )
        with suppress(TimeoutError):
            await asyncio.wait_for(record.finished.wait(), params["yield_time_ms"] / 1000)
        text, missed = record.page(0)
        truncated = len(text) > PAGE_CHARS
        shown = text[:PAGE_CHARS]
        # Where the shown page starts: the window's front when output was
        # already dropped, otherwise zero. The model passes it back as ``since``.
        offset = record.cursor - len(text) if text else record.window_start
        next_cursor = offset + len(shown)
        details = {
            **record.snapshot(), "output": shown, "truncated": truncated,
            "wall_time_seconds": monotonic() - started, "workdir": str(cwd),
            **({"sandbox": sandbox_record} if sandbox_record else {}),
        }
        gap = f"[Output before cursor {missed} was dropped]\n" if missed is not None else ""
        if record.running:
            readonly = self.sandbox is not None and not self.sandbox.settings.writable_workspace
            head = (f"Running in background: process_id={record.id}\n"
                    f"Yielded after {params['yield_time_ms']}ms; the process keeps running.\n"
                    f"Next: process(action='read', process_id='{record.id}', since={next_cursor}, "
                    "wait_ms=30000) to collect more output.\n"
                    + ("Interactive stdin is disabled.\n" if readonly else "Use action='write' to send stdin.\n"))
            return AgentToolResult(content=[TextContent(head + gap + shown)],
                                   details=details)
        if not truncated:
            # Fully read already: keeping it would only count against the cap.
            self.processes.forget(record)
        head = f"Exit code: {record.exit_code}\n"
        if record.timed_out:
            head += "[Wall clock expired; process group terminated]\n"
        if truncated:
            # Kept alive only so the rest is reachable; a forgotten record would
            # leave the model holding an id that can never be used.
            head += (f"[{len(shown)} characters shown; read the rest with "
                     f"process(action='read', process_id='{record.id}', since={next_cursor})]\n")
        return AgentToolResult(
            content=[TextContent(head + gap + shown)], details=details,
            is_error=record.timed_out or record.exit_code != 0,
        )

