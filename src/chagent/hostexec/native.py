"""Host-controller-only native command, gated by a consumed web approval."""

import asyncio
from contextlib import suppress
import os
import signal as signal_module
from time import monotonic

from chagent.domain.messages import TextContent
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.tools.context import ToolContext
from chagent.tools.policy import fingerprint
from chagent.tools.workspace import WorkspaceFS


class NativeHostExecTool(AgentTool):
    def __init__(self, database):
        self.database = database
        super().__init__(name="host_exec", label="Request native host execution",
            description="Request one native command on the user's computer only when Docker cannot satisfy the task. "
                        "The exact command and workdir require one approval in this page. First run and inspect a "
                        "successful container command or test in an earlier model turn. Refusal is final.",
            parameters={"type": "object", "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 32768},
                "workdir": {"type": "string", "minLength": 1},
                "purpose": {"type": "string", "minLength": 1, "maxLength": 1000},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 120000}},
                "required": ["command", "purpose"], "additionalProperties": False},
            requires_workspace=True, requires_host_execution=True)

    def execution_timeout(self, params):
        return params.get("timeout_ms", 30000) / 1000 + 15

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context: ToolContext | None = None):
        if context is None or context.run_id is None:
            raise PermissionError("Native execution requires a run")
        fs = WorkspaceFS(context.workspace, expected_identity=context.workspace_identity)
        parts = fs.parts(params.get("workdir", "."))
        with fs.directory(parts):
            pass
        cwd = fs.root.joinpath(*parts)
        expected = fingerprint("host_exec", params, str(fs.root))
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT a.id FROM tool_calls t JOIN approval_requests a ON a.tool_record_id = t.id "
                "WHERE t.run_id = %s AND t.call_id = %s AND t.name = 'host_exec' "
                "AND a.run_id = t.run_id AND a.status = 'approved' AND a.consumed_at IS NOT NULL "
                "AND a.tool_name = 'host_exec' AND a.fingerprint = %s AND a.cwd = %s "
                "AND a.expires_at > clock_timestamp()",
                (context.run_id, tool_call_id, expected, str(fs.root)))
            grant = await cursor.fetchone()
            cursor = await connection.execute(
                "SELECT t.name, t.result FROM tool_calls t JOIN model_calls m ON m.id = t.model_call_id "
                "WHERE t.run_id = %s AND t.name IN ('exec_command', 'run_tests') "
                "AND t.status = 'completed' AND m.iteration < "
                "(SELECT current.iteration FROM tool_calls own JOIN model_calls current "
                "ON current.id = own.model_call_id WHERE own.run_id = %s AND own.call_id = %s) "
                "ORDER BY t.ended_at DESC LIMIT 1", (context.run_id, context.run_id, tool_call_id))
            evidence = await cursor.fetchone()
        if grant is None:
            raise PermissionError("Native command has no matching consumed approval")
        details = (evidence or {}).get("result") or {}
        if (details.get("details") or {}).get("exit_code") != 0:
            return AgentToolResult(content=[TextContent(
                "First complete and inspect a successful container command or test in an earlier model turn.")],
                is_error=True)
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "INSERT INTO native_executions (approval_id, run_id, command, cwd, status) "
                "VALUES (%s, %s, %s, %s, 'starting') ON CONFLICT DO NOTHING RETURNING approval_id",
                (grant["id"], context.run_id, params["command"], str(cwd)))
            if await cursor.fetchone() is None:
                return AgentToolResult(content=[TextContent("This native approval has already been used. No retry.")],
                                       is_error=True, outcome_unknown=True)
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
        started = monotonic()
        try:
            child = await asyncio.create_subprocess_exec("/bin/sh", "-c", params["command"], cwd=cwd,
                env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except BaseException:
            async with self.database.connection() as connection:
                await connection.execute("UPDATE native_executions SET status='unknown', ended_at=now() WHERE approval_id=%s",
                                         (grant["id"],))
            raise
        output = bytearray()

        async def collect():
            while chunk := await child.stdout.read(4096):
                room = max(0, 65536 - len(output))
                output.extend(chunk[:room])

        waiter = asyncio.create_task(child.wait())
        reader = asyncio.create_task(collect())
        complete = asyncio.gather(waiter, reader)
        try:
            await asyncio.wait_for(asyncio.shield(complete), params.get("timeout_ms", 30000) / 1000)
            timed_out = False
        except (TimeoutError, asyncio.CancelledError) as error:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal_module.SIGKILL)
            await complete
            if isinstance(error, asyncio.CancelledError):
                async with self.database.connection() as connection:
                    await connection.execute("UPDATE native_executions SET status='unknown', ended_at=now(), exit_code=%s WHERE approval_id=%s",
                                             (child.returncode, grant["id"]))
                raise
            timed_out = True
        body = output.decode(errors="replace")
        status = "timed_out" if timed_out else "completed" if child.returncode == 0 else "failed"
        async with self.database.connection() as connection:
            await connection.execute("UPDATE native_executions SET status=%s, ended_at=now(), exit_code=%s WHERE approval_id=%s",
                                     (status, child.returncode, grant["id"]))
        return AgentToolResult(content=[TextContent(f"Native exit: {child.returncode}\n{body}")],
            details={"approval_id": str(grant["id"]), "exit_code": child.returncode,
                     "output": body, "truncated": len(output) >= 65536,
                     "wall_time_seconds": round(monotonic() - started, 3), "workdir": str(cwd),
                     "timed_out": timed_out, "boundary": "host-native"},
            is_error=timed_out or child.returncode != 0, outcome_unknown=timed_out)
