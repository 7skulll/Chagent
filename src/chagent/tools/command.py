"""One-shot POSIX host commands; cwd is scoped, the process is not sandboxed."""

import asyncio
from contextlib import suppress
import os
import signal as signals
from time import monotonic

from chagent.domain.messages import JsonObject, TextContent
from chagent.tools.base import AgentTool, AgentToolResult, AgentToolUpdateCallback
from chagent.tools.context import ToolContext
from chagent.tools.workspace import WorkspaceFS


class ExecCommandTool(AgentTool):
    def __init__(self):
        super().__init__(
            name="exec_command", label="Run command",
            description="Run one non-interactive /bin/sh command on the host. This is not sandboxed. No background sessions or stdin. Output is bounded; exit status is reported.",
            parameters={
                "type": "object", "properties": {
                    "cmd": {"type": "string", "minLength": 1, "maxLength": 32_768},
                    "workdir": {"type": "string", "minLength": 1},
                    "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 30_000},
                    "max_output_chars": {"type": "integer", "minimum": 1, "maximum": 64_000},
                }, "required": ["cmd"], "additionalProperties": False,
            }, requires_workspace=True, requires_host_execution=True,
        )

    async def execute(
        self, tool_call_id: str, params: JsonObject, signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None, *, context: ToolContext | None = None,
    ) -> AgentToolResult:
        if context is None:
            raise PermissionError("A session workspace is required")
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        filesystem = WorkspaceFS(context.workspace)
        parts = filesystem.parts(params.get("workdir", "."))
        # Validate directory components without following symlinks.
        with filesystem.directory(parts):
            pass
        cwd = filesystem.root.joinpath(*parts)
        started = monotonic()
        # Shield spawn so cancellation cannot lose a successfully started child.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            "/bin/sh", "-c", params["cmd"], cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
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
        }
        return AgentToolResult(
            content=[TextContent(f"Exit code: {process.returncode}\n{text}")], details=details,
            is_error=timed_out or process.returncode != 0, outcome_unknown=timed_out,
        )
