"""Long-lived host processes owned by one run, read by cursor, fed by stdin.

One-shot ``exec_command`` is the right shape for most work and the wrong shape
for all of it: the invocation budget caps a command at 30 seconds, so installing
a dependency, building a project or driving an interactive tool is impossible.
This module keeps the child alive across tool calls without turning the host
into a job server.

Four boundaries are deliberate, and each is cheaper to state than to discover:

* **Ownership is per run and lives in this process.** A restart adopts nothing,
  so a stale ``process_id`` is answered as unknown rather than as a guessed PID.
* **Output is a bounded window.** Characters dropped from the front are counted
  and reported, never silently rewritten; a ``since`` that predates the window
  is reported as a gap instead of returning the wrong bytes.
* **The workspace gate covers the invocation, not the process.** A live session
  can overlap a later file tool, exactly like a terminal left running. Run end
  terminates every process the run still owns.
* **stdin is a pipe only for session processes.** A one-shot command keeps
  ``/dev/null``, so ``cat`` cannot hang an invocation that never yields.

See ``docs/执行边界设计.md`` §12 for the contract and the capability table.
"""

import asyncio
import codecs
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal as signals
from time import monotonic
from uuid import UUID, uuid4

from chagent.domain.messages import JsonObject, TextContent
from chagent.tools.base import AgentTool, AgentToolResult, AgentToolUpdateCallback
from chagent.tools.context import ToolContext


#: Characters retained per process. Older output is dropped and counted.
DEFAULT_WINDOW_CHARS = 1_048_576
#: Characters returned by one read. A larger page is nearly always a mistake:
#: the model pays for it in the next request and cannot read it anyway.
PAGE_CHARS = 20_000
#: Live processes one run may hold at once.
DEFAULT_MAX_PER_RUN = 4
#: Hard wall clock for one process, independent of how often it is read.
DEFAULT_WALL_TIMEOUT = 600.0
STOP_GRACE = 2.0
#: How long a read waits for the exit code once output has ended.
EOF_GRACE = 0.5
#: How long a read waits for it when output arrived but the pipe is still open.
EXIT_GRACE = 0.05


@dataclass(slots=True)
class ManagedProcess:
    """One child, its bounded output window, and its absolute cursors."""

    id: str
    run_id: UUID
    command: str
    cwd: Path
    argv: list[str]
    sandbox: dict
    started: float
    process: asyncio.subprocess.Process
    window: int
    chunks: deque[str] = field(default_factory=deque)
    # Decoded incrementally: a UTF-8 character split across two reads must not
    # become two replacement characters at every boundary.
    decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"))
    window_start: int = 0      # absolute cursor of chunks[0]
    cursor: int = 0            # characters produced since start
    dropped: int = 0           # characters removed from the front
    exit_code: int | None = None
    timed_out: bool = False
    stopped: bool = False
    # stdout reached EOF. The exit code arrives one loop tick later, so a read
    # that already has the last output waits briefly for it instead of
    # reporting "running" and making the model ask again.
    eof: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    new_output: asyncio.Event = field(default_factory=asyncio.Event)
    reader: asyncio.Task | None = None
    waiter: asyncio.Task | None = None
    deadline: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return not self.finished.is_set()

    def feed(self, chunk: bytes, *, final: bool = False) -> None:
        text = self.decoder.decode(chunk, final)
        if text:
            self.chunks.append(text)
            self.cursor += len(text)
            self._trim()
        if text or final:
            self.new_output.set()

    def _trim(self) -> None:
        # Whole chunks are dropped first: a partial chunk would make every
        # cursor arithmetic a rescan. One writer can produce a single chunk
        # larger than the window, so the tail of that chunk is kept instead.
        while self.cursor - self.window_start > self.window:
            if len(self.chunks) > 1:
                removed = self.chunks.popleft()
                self.window_start += len(removed)
                self.dropped += len(removed)
                continue
            chunk = self.chunks.popleft()
            cut = self.cursor - self.window_start - self.window
            self.chunks.append(chunk[cut:])
            self.window_start += cut
            self.dropped += cut
            break

    def page(self, since: int) -> tuple[str, int | None]:
        """Retained text from ``since``, plus the cursor that was already dropped."""
        since = max(0, min(since, self.cursor))
        missed = None if since >= self.window_start else self.window_start
        text = "".join(self.chunks)[max(since, self.window_start) - self.window_start:]
        return text, missed

    def snapshot(self) -> dict:
        return {
            "process_id": self.id, "command": self.command, "cwd": str(self.cwd),
            "running": self.running, "exit_code": self.exit_code,
            "cursor": self.cursor, "dropped": self.dropped,
            "elapsed_seconds": round(monotonic() - self.started, 3),
            "timed_out": self.timed_out, "stopped": self.stopped,
        }


class ProcessLimitError(RuntimeError):
    """The run already holds its maximum number of live processes."""


class ProcessManager:
    """Children owned by a run, with a wall clock and run-end reclamation."""

    def __init__(self, *, window_chars: int = DEFAULT_WINDOW_CHARS,
                 max_per_run: int = DEFAULT_MAX_PER_RUN,
                 wall_timeout: float = DEFAULT_WALL_TIMEOUT):
        if window_chars < 1:
            raise ValueError("Process output window must be positive")
        if isinstance(max_per_run, bool) or not isinstance(max_per_run, int) or not 1 <= max_per_run <= 16:
            raise ValueError("Max processes per run must be an integer between 1 and 16")
        if wall_timeout <= 0:
            raise ValueError("Process wall clock must be positive")
        self.window_chars, self.max_per_run, self.wall_timeout = window_chars, max_per_run, wall_timeout
        self._records: dict[str, ManagedProcess] = {}

    # -- lifetime -------------------------------------------------------

    async def start(self, *, run_id: UUID, command: str, cwd: Path, argv: list[str],
                    sandbox: dict | None = None, window_chars: int | None = None) -> ManagedProcess:
        live = [record for record in self.owned(run_id) if record.running]
        if len(live) >= self.max_per_run:
            ids = ", ".join(record.id for record in live)
            raise ProcessLimitError(
                f"This run already has {len(live)} live processes ({ids}); "
                "stop one with process(action='stop') before starting another")
        self._prune(run_id)
        process = await self._spawn(argv, cwd)
        record = ManagedProcess(
            id=uuid4().hex[:8], run_id=run_id, command=command, cwd=cwd, argv=list(argv),
            sandbox=sandbox or {}, started=monotonic(), process=process,
            window=max(1, window_chars or self.window_chars),
        )
        self._records[record.id] = record
        record.reader = asyncio.create_task(self._pump(record), name=f"process-reader-{record.id}")
        record.waiter = asyncio.create_task(self._reap(record), name=f"process-waiter-{record.id}")
        record.deadline = asyncio.create_task(self._expire(record), name=f"process-deadline-{record.id}")
        return record

    @staticmethod
    async def _spawn(argv: list[str], cwd: Path) -> asyncio.subprocess.Process:
        # Shield the spawn: cancellation must not lose a child that already
        # started, or the run would own a process it cannot reap.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        ))
        try:
            return await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signals.SIGKILL)
            await process.communicate()
            raise

    async def _pump(self, record: ManagedProcess) -> None:
        try:
            while chunk := await record.process.stdout.read(4096):
                record.feed(chunk)
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            record.feed(b"", final=True)
            record.eof = True

    async def _reap(self, record: ManagedProcess) -> None:
        try:
            await record.process.wait()
        finally:
            record.exit_code = record.process.returncode
            record.finished.set()
            record.new_output.set()
            deadline = record.deadline
            if deadline is not None and deadline is not asyncio.current_task():
                deadline.cancel()
            if record.process.stdin is not None and not record.process.stdin.is_closing():
                with suppress(Exception):
                    record.process.stdin.close()

    async def _expire(self, record: ManagedProcess) -> None:
        """The wall clock is the only limit a session process answers to.

        ``timeout_ms`` bounds one invocation; a process that outlives its
        invocation would otherwise be unbounded, and run end is not a promise
        (a process can be started and never read).
        """
        try:
            await asyncio.sleep(self.wall_timeout)
        except asyncio.CancelledError:
            return
        if record.running:
            record.timed_out = True
            await self.stop(record)

    def owned(self, run_id: UUID) -> list[ManagedProcess]:
        return [record for record in self._records.values() if record.run_id == run_id]

    def get(self, process_id: str, run_id: UUID) -> ManagedProcess | None:
        record = self._records.get(process_id)
        # Another run's process is indistinguishable from a missing one: naming
        # it would confirm a handle the caller may not hold.
        return record if record is not None and record.run_id == run_id else None

    def _prune(self, run_id: UUID) -> None:
        """Bound the table: finished records are the first to go."""
        finished = [record for record in self.owned(run_id) if not record.running]
        if len(finished) > self.max_per_run * 2 or len(self._records) > 32:
            for record in finished:
                self.forget(record)

    def forget(self, record: ManagedProcess) -> None:
        self._records.pop(record.id, None)
        for task in (record.reader, record.waiter, record.deadline):
            if task is not None and not task.done():
                task.cancel()

    async def stop(self, record: ManagedProcess) -> None:
        """Kill the process group, then drain what the child had already written."""
        record.stopped = not record.timed_out
        if record.running:
            with suppress(ProcessLookupError):
                os.killpg(record.process.pid, signals.SIGKILL)
        await self._settle(record)

    async def _settle(self, record: ManagedProcess) -> None:
        try:
            async with asyncio.timeout(STOP_GRACE):
                await record.finished.wait()
                if record.reader is not None:
                    await asyncio.shield(record.reader)
        except (TimeoutError, asyncio.CancelledError):
            pass

    async def terminate_run(self, run_id: UUID) -> int:
        """Reclaim every process this run owns. Called from the run's finally."""
        records = self.owned(run_id)
        for record in records:
            if record.running:
                await self.stop(record)
        for record in records:
            self.forget(record)
        return len(records)


def _status(record: ManagedProcess) -> str:
    if record.running:
        return "running"
    state = f"exited with code {record.exit_code}"
    if record.timed_out:
        state += " (wall clock expired, group terminated)"
    elif record.stopped:
        state += " (stopped by request)"
    return state


class ProcessTool(AgentTool):
    """Read, feed and stop the processes a run started with ``yield_time_ms``."""

    def __init__(self, manager: ProcessManager):
        super().__init__(
            name="process", label="Manage a running command",
            description=(
                "Inspect commands this run left running (exec_command with yield_time_ms). "
                "action=list shows them; action=read returns output at or after 'since' and the "
                "cursor to pass next, optionally waiting up to wait_ms for more; action=write sends "
                "'chars' to the command's stdin (close_stdin=true signals end of input); action=stop "
                "kills the process group. Anything still running is killed when the run ends."
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
            requires_workspace=True, requires_host_execution=True,
        )
        self.manager = manager

    async def execute(
        self, tool_call_id: str, params: JsonObject, signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None, *, context: ToolContext | None = None,
    ) -> AgentToolResult:
        if context is None or context.run_id is None:
            raise PermissionError("Processes belong to a run")
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        action = params["action"]
        if action == "list":
            return self._list(context.run_id)
        record = self.manager.get(params["process_id"], context.run_id)
        if record is None:
            raise LookupError(self._unknown(context.run_id))
        if action == "read":
            return await self._read(record, params)
        if action == "write":
            return await self._write(record, params)
        return await self._stop(record)

    def _list(self, run_id: UUID) -> AgentToolResult:
        records = self.manager.owned(run_id)
        if not records:
            return AgentToolResult(
                content=[TextContent("No processes in this run. Start one with "
                                     "exec_command and yield_time_ms.")],
                details={"processes": []})
        lines = ["Processes in this run:"]
        for record in records:
            lines.append(f"- {record.id} {_status(record)} {monotonic() - record.started:.1f}s "
                         f"cursor={record.cursor} {record.command[:120]}")
        return AgentToolResult(content=[TextContent("\n".join(lines))],
                               details={"processes": [record.snapshot() for record in records]})

    async def _read(self, record: ManagedProcess, params: JsonObject) -> AgentToolResult:
        since, wait_ms = params.get("since", 0), params.get("wait_ms", 0)
        if wait_ms and record.running and since >= record.cursor:
            # A bounded wait is how a model follows a build instead of polling
            # once per turn. The event is cleared before waiting and the cursor
            # re-checked after, so a stale set from earlier output cannot make
            # this return instantly, and output that arrives mid-clear is not
            # slept through.
            deadline = asyncio.get_running_loop().time() + wait_ms / 1000
            while record.running and record.cursor <= since:
                record.new_output.clear()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                with suppress(TimeoutError):
                    await asyncio.wait_for(record.new_output.wait(), remaining)
        if record.running:
            # The last line and the exit code usually land together, and the
            # exit code arrives through a different task than the output. A
            # short grace here saves the model a whole turn spent re-reading
            # just to learn whether the command finished.
            with suppress(TimeoutError):
                await asyncio.wait_for(record.finished.wait(),
                                       EOF_GRACE if record.eof else EXIT_GRACE)
        text, missed = record.page(since)
        truncated = len(text) > PAGE_CHARS
        text = text[:PAGE_CHARS]
        offset = max(min(since, record.cursor), record.window_start)
        next_cursor = offset + len(text)
        head = [f"Process: {record.id}", f"Command: {record.command[:200]}",
                f"Status: {_status(record)}"]
        if missed is not None:
            head.append(f"Gap: output before cursor {missed} was dropped "
                        f"(the window holds {record.window} characters)")
        if text:
            head.append(f"Output from cursor {offset}:")
        else:
            head.append("No new output" + (" yet; read again with wait_ms to block."
                                           if record.running else "."))
        head.append(f"Next since: {next_cursor}" + (" (more is already available)" if truncated else ""))
        return AgentToolResult(
            content=[TextContent("\n".join(head) + ("\n\n" + text if text else ""))],
            details={**record.snapshot(), "since": since, "next_since": next_cursor,
                     "truncated": truncated, "output": text})

    async def _write(self, record: ManagedProcess, params: JsonObject) -> AgentToolResult:
        chars, close = params.get("chars", ""), bool(params.get("close_stdin", False))
        if not chars and not close:
            raise ValueError("write needs 'chars' or close_stdin=true")
        if not record.running:
            return AgentToolResult(
                content=[TextContent(f"Process {record.id} already exited with code {record.exit_code}; "
                                     "stdin is closed. Read its remaining output instead.")],
                details={**record.snapshot(), "written": 0}, is_error=True)
        stdin = record.process.stdin
        if stdin is None:
            raise ValueError("This process has no stdin")
        if chars:
            stdin.write(chars.encode("utf-8"))
            await stdin.drain()
        if close and not stdin.is_closing():
            stdin.close()
        return AgentToolResult(
            content=[TextContent(f"Wrote {len(chars)} character(s) to {record.id}"
                                 + (" and closed stdin" if close else "")
                                 + f". Next since: {record.cursor}")],
            details={**record.snapshot(), "written": len(chars), "closed_stdin": close})

    async def _stop(self, record: ManagedProcess) -> AgentToolResult:
        if record.running:
            await self.manager.stop(record)
        tail, _ = record.page(max(0, record.cursor - 2000))
        return AgentToolResult(
            content=[TextContent(f"Stopped {record.id}; exit code {record.exit_code}. "
                                 f"{record.cursor} characters produced in total."
                                 + ("\n\nLast output:\n" + tail if tail else ""))],
            details=record.snapshot())

    def _unknown(self, run_id: UUID) -> str:
        known = [record.id for record in self.manager.owned(run_id)]
        listed = ", ".join(known) if known else "none"
        return (f"No process with that id in this run (known: {listed}). Ids are only valid "
                "for the run that started them and do not survive a restart.")
