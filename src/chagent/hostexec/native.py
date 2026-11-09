"""Host-controller-only native command, gated by a consumed web approval.

A one-shot command waits for its own exit. A ``yield_time_ms`` launch keeps the
child alive with an open stdin pipe, owned by the run's process manager; every
later write to that stdin is a separate decision with its own one-time approval
(the launch grant never covers later input, and a write grant never covers a
second write). Native rows in ``native_executions`` are the audit trail: a
restart cannot prove what happened to a child it no longer manages, so recovery
marks those rows unknown and refuses to retry them.
"""

import asyncio
from contextlib import suppress
import os
import signal as signal_module
from time import monotonic

from chagent.domain.messages import JsonObject, TextContent
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.tools.context import ToolContext
from chagent.tools.policy import fingerprint
from chagent.tools.processes import (_status, ManagedProcess, PAGE_CHARS,
                                     ProcessManager, read_process, stop_process)
from chagent.tools.workspace import WorkspaceFS


def _host_environment() -> dict[str, str]:
    # Never the controller's whole environment: API keys and the database URL
    # stay with the controller, exactly as in the one-shot path.
    return {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}


async def mark_interrupted_native_executions(database) -> int:
    """A restarted controller must not treat an in-flight spawn as retryable.

    ``starting`` rows lost the race between insert and spawn; ``running`` rows
    belonged to a process manager that no longer exists, so the child's real
    fate — alive, dead, or something in between — is unknown and stays that way.
    A stdin write claimed its one-time grant but never settled the outcome row,
    so the bytes' fate is equally unknown.
    """
    async with database.connection() as connection:
        cursor = await connection.execute(
            "UPDATE native_executions SET status='unknown', ended_at=now() "
            "WHERE status IN ('starting', 'running') RETURNING approval_id")
        marked = len(await cursor.fetchall())
        cursor = await connection.execute(
            "UPDATE native_stdin_writes SET status='unknown' WHERE status='pending' RETURNING approval_id")
        marked += len(await cursor.fetchall())
        return marked


async def _consumed_grant(database, *, run_id, tool_call_id, tool_name, expected, cwd):
    """The one approval this exact invocation already consumed, or ``None``."""
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT a.id FROM tool_calls t JOIN approval_requests a ON a.tool_record_id = t.id "
            "WHERE t.run_id = %s AND t.call_id = %s AND t.name = %s "
            "AND a.run_id = t.run_id AND a.status = 'approved' AND a.consumed_at IS NOT NULL "
            "AND a.tool_name = %s AND a.fingerprint = %s AND a.cwd = %s "
            "AND a.expires_at > clock_timestamp()",
            (run_id, tool_call_id, tool_name, tool_name, expected, cwd))
        return await cursor.fetchone()


async def _container_evidence(database, *, run_id, tool_call_id) -> bool:
    """Whether an earlier model turn completed a successful container command."""
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT t.name, t.result FROM tool_calls t JOIN model_calls m ON m.id = t.model_call_id "
            "WHERE t.run_id = %s AND t.name IN ('exec_command', 'run_tests') "
            "AND t.status = 'completed' AND m.iteration < "
            "(SELECT current.iteration FROM tool_calls own JOIN model_calls current "
            "ON current.id = own.model_call_id WHERE own.run_id = %s AND own.call_id = %s) "
            "ORDER BY t.ended_at DESC LIMIT 1", (run_id, run_id, tool_call_id))
        evidence = await cursor.fetchone()
    details = (evidence or {}).get("result") or {}
    return (details.get("details") or {}).get("exit_code") == 0


def _terminal_status(record: ManagedProcess) -> str:
    if record.timed_out:
        return "timed_out"
    if record.stopped:
        return "stopped"
    return "completed" if record.exit_code == 0 else "failed"


class NativeHostExecTool(AgentTool):
    def __init__(self, database, processes: ProcessManager | None = None):
        self.database = database
        self.processes = processes
        #: Live exit watchers, held for as long as they run.
        self._watchers: set[asyncio.Task] = set()
        super().__init__(name="host_exec", label="Request native host execution",
            description="Request one native command on the user's computer only when Docker cannot satisfy the task. "
                        "The exact command and workdir require one approval in this page. First run and inspect a "
                        "successful container command or test in an earlier model turn. Refusal is final. "
                        "Pass yield_time_ms to keep the command running with an open stdin and manage it with "
                        "host_process; each input write needs its own approval.",
            parameters={"type": "object", "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 32768},
                "workdir": {"type": "string", "minLength": 1},
                "purpose": {"type": "string", "minLength": 1, "maxLength": 1000},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 120000},
                "yield_time_ms": {"type": "integer", "minimum": 100, "maximum": 30000}},
                "required": ["command", "purpose"], "additionalProperties": False},
            requires_workspace=True, requires_host_execution=True)

    def execution_timeout(self, params):
        if "yield_time_ms" in params:
            # The initial wait only; the child answers to the process wall clock.
            return params["yield_time_ms"] / 1000 + 15
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
        grant = await _consumed_grant(self.database, run_id=context.run_id, tool_call_id=tool_call_id,
                                      tool_name="host_exec", expected=expected, cwd=str(fs.root))
        if grant is None:
            raise PermissionError("Native command has no matching consumed approval")
        if not await _container_evidence(self.database, run_id=context.run_id, tool_call_id=tool_call_id):
            return AgentToolResult(content=[TextContent(
                "First complete and inspect a successful container command or test in an earlier model turn.")],
                is_error=True)
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        interactive = "yield_time_ms" in params
        if interactive and self.processes is None:
            raise ValueError("Interactive native execution requires a process manager")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "INSERT INTO native_executions (approval_id, run_id, command, cwd, status, interactive) "
                "VALUES (%s, %s, %s, %s, 'starting', %s) ON CONFLICT DO NOTHING RETURNING approval_id",
                (grant["id"], context.run_id, params["command"], str(cwd), interactive))
            if await cursor.fetchone() is None:
                return AgentToolResult(content=[TextContent("This native approval has already been used. No retry.")],
                                       is_error=True, outcome_unknown=True)
        if interactive:
            return await self._interactive(tool_call_id, params, context, cwd, grant)

        env = _host_environment()
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

    async def _interactive(self, tool_call_id, params, context, cwd, grant) -> AgentToolResult:
        """Start an approved native command and return once the yield has passed.

        The child is owned by the run, exactly like a container session process,
        so nothing here kills the process group: run end and the process wall
        clock are the only things that reclaim it. A watcher task turns the
        child's eventual exit into the terminal status of the audit row.
        """
        assert self.processes is not None
        started = monotonic()
        try:
            record = await self.processes.start(
                run_id=context.run_id, command=params["command"], cwd=cwd,
                argv=["/bin/sh", "-c", params["command"]], env=_host_environment(),
                sandbox={"boundary": "host-native"})
        except BaseException:
            async with self.database.connection() as connection:
                await connection.execute(
                    "UPDATE native_executions SET status='unknown', ended_at=now() WHERE approval_id=%s",
                    (grant["id"],))
            raise
        async with self.database.connection() as connection:
            await connection.execute(
                "UPDATE native_executions SET status='running', process_id=%s, child_pid=%s, started_at=now() "
                "WHERE approval_id=%s", (record.id, record.process.pid, grant["id"]))
        watcher = asyncio.create_task(self._record_exit(record, grant["id"]),
                                      name=f"native-exit-{record.id}")
        # The child can outlive this call, so the watcher must outlive it too.
        # A bare local reference is enough only while this frame is alive: the
        # task is kept until it finishes, or a collected watcher would leave the
        # audit row 'running' with a healthy controller and nothing left to
        # write it (restart recovery is the backstop, not the plan).
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        with suppress(TimeoutError):
            await asyncio.wait_for(record.finished.wait(), params["yield_time_ms"] / 1000)
        text, missed = record.page(0)
        truncated = len(text) > PAGE_CHARS
        shown = text[:PAGE_CHARS]
        offset = record.cursor - len(text) if text else record.window_start
        next_cursor = offset + len(shown)
        gap = f"[Output before cursor {missed} was dropped]\n" if missed is not None else ""
        if record.running:
            head = (f"Native process running: process_id={record.id}\n"
                    f"Yielded after {params['yield_time_ms']}ms; the process keeps running on the host.\n"
                    f"Next: host_process(action='read', process_id='{record.id}', since={next_cursor}, "
                    "wait_ms=30000) to collect output.\n"
                    "Send input with host_process(action='write'); every write needs its own page approval.\n")
            return AgentToolResult(content=[TextContent(head + gap + shown)],
                details={**record.snapshot(), "output": shown, "truncated": truncated,
                         "approval_id": str(grant["id"]), "wall_time_seconds": round(monotonic() - started, 3),
                         "workdir": str(cwd), "boundary": "host-native", "watcher_pending": not watcher.done()})
        head = f"Native exit code: {record.exit_code}\n"
        if record.timed_out:
            head += "[Wall clock expired; process group terminated]\n"
        elif record.stopped:
            head += "[Stopped by request]\n"
        return AgentToolResult(content=[TextContent(head + gap + shown)],
            details={**record.snapshot(), "output": shown, "truncated": truncated,
                     "approval_id": str(grant["id"]), "wall_time_seconds": round(monotonic() - started, 3),
                     "workdir": str(cwd), "boundary": "host-native"},
            is_error=record.timed_out or record.exit_code != 0)

    async def _record_exit(self, record: ManagedProcess, approval_id) -> None:
        """The only writer of a live row's terminal status.

        Runs independently of the run that started it, because the child can
        outlive its run's tool calls; a controller restart kills this task, and
        recovery then marks the still-``running`` row unknown instead.
        """
        try:
            await record.finished.wait()
            async with self.database.connection() as connection:
                await connection.execute(
                    "UPDATE native_executions SET status=%s, ended_at=now(), exit_code=%s "
                    "WHERE approval_id=%s AND status='running'",
                    (_terminal_status(record), record.exit_code, approval_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            # The row stays 'running'; restart recovery is the backstop.
            pass


class NativeHostProcessTool(AgentTool):
    """Read, feed and stop the native processes this run started.

    Mirrors ``process`` for native children: bounded output windows, cursors,
    run-scoped ids, wall-clock and run-end reclamation. The one asymmetry is
    deliberate: writing to a native stdin is a host-side effect, so each write
    (chars, EOF, or both) requires its own one-time page approval, verified
    against the exact invocation before a byte moves.
    """

    def __init__(self, database, processes: ProcessManager):
        self.database = database
        self.processes = processes
        super().__init__(
            name="host_process", label="Manage a native host process",
            description=(
                "Inspect the native host processes this run started with host_exec and yield_time_ms. "
                "action=list shows them; action=read returns output at or after 'since' and the cursor to "
                "pass next, optionally waiting up to wait_ms; action=write sends 'chars' to the process's "
                "stdin (close_stdin=true ends input) and requires one page approval per write; action=stop "
                "kills the process group. Ids do not survive a controller restart."
            ),
            parameters={
                "type": "object", "properties": {
                    "action": {"type": "string", "enum": ["list", "read", "write", "stop"]},
                    "process_id": {"type": "string", "minLength": 1},
                    "since": {"type": "integer", "minimum": 0},
                    "wait_ms": {"type": "integer", "minimum": 0, "maximum": 30_000},
                    "chars": {"type": "string", "maxLength": 8_192},
                    "close_stdin": {"type": "boolean"},
                },
                "required": ["action"], "additionalProperties": False,
                "allOf": [{
                    "if": {"properties": {"action": {"enum": ["read", "write", "stop"]}},
                           "required": ["action"]},
                    "then": {"required": ["process_id"]},
                }],
            },
            requires_workspace=True, requires_host_execution=True)

    def execution_timeout(self, params: JsonObject) -> float:
        return (params.get("wait_ms", 0) or 0) / 1000 + 10

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *,
                      context: ToolContext | None = None):
        if context is None or context.run_id is None:
            raise PermissionError("Native processes belong to a run")
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        action = params["action"]
        if action == "list":
            return self._list(context.run_id)
        record = self.processes.get(params["process_id"], context.run_id)
        if record is None:
            raise LookupError(self._unknown(context.run_id))
        if action == "read":
            return await self._read(record, params)
        if action == "write":
            return await self._write(tool_call_id, params, context, record)
        return await self._stop(record)

    def _list(self, run_id) -> AgentToolResult:
        records = self.processes.owned(run_id)
        if not records:
            return AgentToolResult(
                content=[TextContent("No native processes in this run. Start one with host_exec "
                                     "and yield_time_ms.")],
                details={"processes": []})
        lines = ["Native host processes in this run:"]
        for record in records:
            lines.append(f"- {record.id} {_status(record)} {monotonic() - record.started:.1f}s "
                         f"cursor={record.cursor} {record.command[:120]}")
        return AgentToolResult(content=[TextContent("\n".join(lines))],
                               details={"processes": [record.snapshot() for record in records]})

    async def _read(self, record, params) -> AgentToolResult:
        return await read_process(record, params)

    async def _write(self, tool_call_id, params, context, record) -> AgentToolResult:
        chars, close = params.get("chars", ""), bool(params.get("close_stdin", False))
        if not chars and not close:
            raise ValueError("write needs 'chars' or close_stdin=true")
        fs = WorkspaceFS(context.workspace, expected_identity=context.workspace_identity)
        expected = fingerprint("host_process", params, str(fs.root))
        grant = await _consumed_grant(self.database, run_id=context.run_id, tool_call_id=tool_call_id,
                                      tool_name="host_process", expected=expected, cwd=str(fs.root))
        if grant is None:
            raise PermissionError("Native stdin write has no matching consumed approval")
        # Claim the one-time grant before a byte moves. The insert fails only
        # for a replay of the same approval, which must not deliver the input
        # twice — the same shape the launch path uses against re-execution.
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "INSERT INTO native_stdin_writes (approval_id, execution_id, run_id, process_id, "
                "chars, close_stdin, status) VALUES (%s, "
                "(SELECT approval_id FROM native_executions WHERE run_id=%s AND process_id=%s), "
                "%s, %s, %s, %s, 'pending') ON CONFLICT (approval_id) DO NOTHING RETURNING approval_id",
                (grant["id"], context.run_id, record.id, context.run_id, record.id, chars, close))
            if await cursor.fetchone() is None:
                return AgentToolResult(content=[TextContent(
                    "This write approval has already been used. No retry.")],
                    is_error=True, outcome_unknown=True)
        async with record.stdin_lock:
            stdin = record.process.stdin
            if not record.running or stdin is None or stdin.is_closing():
                reason = (f"already exited with code {record.exit_code}" if not record.running
                          else "stdin is closed")
                await self._settle_write(grant["id"], "failed")
                return AgentToolResult(
                    content=[TextContent(f"Native process {record.id} {reason}; read its remaining output instead.")],
                    details={**record.snapshot(), "written": 0, "approval_id": str(grant["id"])}, is_error=True)
            failure = None
            if chars:
                try:
                    stdin.write(chars.encode("utf-8"))
                    async with asyncio.timeout(5):
                        await stdin.drain()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    failure = ("Could not finish writing to the native process; check its output and status.")
            if failure is None and close and not stdin.is_closing():
                stdin.close()
        await self._settle_write(grant["id"], "failed" if failure else "written")
        if failure is not None:
            return AgentToolResult(content=[TextContent(failure)],
                                   details={**record.snapshot(), "written": 0, "approval_id": str(grant["id"])},
                                   is_error=True)
        return AgentToolResult(
            content=[TextContent(f"Wrote {len(chars)} character(s) to native process {record.id}"
                                 + (" and closed stdin" if close else "")
                                 + f". Next since: {record.cursor}")],
            details={**record.snapshot(), "written": len(chars), "closed_stdin": close,
                     "approval_id": str(grant["id"])})

    async def _settle_write(self, approval_id, status: str) -> None:
        try:
            async with self.database.connection() as connection:
                await connection.execute(
                    "UPDATE native_stdin_writes SET status=%s WHERE approval_id=%s AND status='pending'",
                    (status, approval_id))
        except Exception:
            # The row stays 'pending'; restart recovery marks it unknown.
            pass

    async def _stop(self, record) -> AgentToolResult:
        return await stop_process(self.processes, record)

    def _unknown(self, run_id) -> str:
        known = [record.id for record in self.processes.owned(run_id)]
        listed = ", ".join(known) if known else "none"
        return (f"No native process with that id in this run (known: {listed}). Ids are only valid "
                "for the run that started them and do not survive a controller restart.")
