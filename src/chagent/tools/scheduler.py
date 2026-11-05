"""Bounded tool batches with exclusive barriers and ordered committed results."""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic_ns, time_ns
from uuid import UUID

from chagent.domain.messages import TextContent, ToolCall, ToolResultMessage
from chagent.domain.runs import StoredMessage
from chagent.storage.repositories import RunRepository
from chagent.tools.base import AgentToolResult
from chagent.tools.context import ToolContext
from chagent.tools.approvals import STALE, ApprovalBroker
from chagent.tools.gate import HumanGate, ToolGate, WriteGate
from chagent.tools.executor import ToolExecutor
from chagent.tools.policy import ApprovalPolicy
from chagent.tools.progress import ProgressGuard, Decision


@dataclass(slots=True)
class ToolBatchResult:
    results: list[AgentToolResult]
    messages: list[ToolResultMessage]
    # A refused repeat means the attempt is going in circles; the runner ends
    # the run with ``repeated_tool_call`` instead of continuing to max_turns.
    stalled: bool = False


@dataclass(slots=True)
class ToolOutcome:
    result: AgentToolResult
    message: ToolResultMessage


class _WorkspaceGate:
    """Writer-preferring gate shared by batches using the same workspace."""

    def __init__(self):
        self.condition = asyncio.Condition()
        self.readers = self.waiting_writers = self.users = 0
        self.writer = False

    @asynccontextmanager
    async def hold(self, parallel):
        async with self.condition:
            if parallel:
                await self.condition.wait_for(lambda: not self.writer and not self.waiting_writers)
                self.readers += 1
            else:
                self.waiting_writers += 1
                try:
                    await self.condition.wait_for(lambda: not self.writer and not self.readers)
                    self.writer = True
                finally:
                    self.waiting_writers -= 1
                    self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                if parallel:
                    self.readers -= 1
                else:
                    self.writer = False
                self.condition.notify_all()


class ToolScheduler:
    def __init__(self, runs: RunRepository, *, max_concurrency: int = 4,
                 progress: ProgressGuard | None = None,
                 policy: ApprovalPolicy | None = None,
                 approvals: ApprovalBroker | None = None,
                 sandbox_mode: str = "off", gates: tuple[ToolGate, ...] | None = None):
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or not 1 <= max_concurrency <= 32:
            raise ValueError("Tool concurrency must be an integer between 1 and 32")
        self.runs, self.max_concurrency = runs, max_concurrency
        self.progress = progress if progress is not None else ProgressGuard()
        # Approvals are asked before execution resources are taken; see _ask.
        self.policy = policy if policy is not None else ApprovalPolicy()
        self.approvals = approvals
        self.sandbox_mode = sandbox_mode
        self.gates = gates
        # Shared across concurrent sessions owned by this runner.
        self._slots = asyncio.Semaphore(max_concurrency)
        self._gates: dict[Path, _WorkspaceGate] = {}

    def release(self, run_id: UUID) -> None:
        self.progress.release(run_id)

    def default_gates(self) -> tuple[ToolGate, ...]:
        """The questions a run asks when approvals are on.

        Commands and workspace writes answer different questions, so they stay
        two gates: a shape may allow one and still want the other. ``never``
        asks neither, which is what keeps an upgrade from blocking existing runs.
        """
        if self.sandbox_mode == "managed-container":
            return (HumanGate(self.policy, self.approvals, self.sandbox_mode),)
        if self.policy.mode == "never":
            return ()
        return (HumanGate(self.policy, self.approvals, self.sandbox_mode),
                WriteGate(self.policy, self.approvals))

    async def cancel_approvals(self, run_id: UUID) -> None:
        if self.approvals is not None:
            await self.approvals.cancel(run_id)

    def _groups(self, calls, executor):
        groups = []
        for index, call in enumerate(calls):
            tool = executor.registry.get(call.name)
            parallel = tool is not None and tool.execution_mode == "parallel" and not tool.requires_host_execution
            if parallel and groups and groups[-1][0]:
                groups[-1][1].append(index)
            else:
                groups.append((parallel, [index]))
        return groups

    async def execute_batch(self, calls: list[ToolCall], *, context: ToolContext, model_call_id: UUID,
                            executor: ToolExecutor,
                            on_committed: Callable[[StoredMessage], None],
                            budget=None) -> ToolBatchResult:
        if context.run_id is None or context.session_id is None:
            raise ValueError("Tool batches require a run and session")
        identities = await self.runs.queue_tool_calls(context.run_id, model_call_id, calls)
        signal = asyncio.Event()
        outcomes: dict[int, ToolOutcome] = {}
        committed: dict[int, StoredMessage] = {}
        attempted: set[int] = set()
        stalled = False
        workers: list[asyncio.Task] = []
        commit_task = None
        failure = asyncio.get_running_loop().create_future()

        async def commit(index):
            outcome = outcomes[index]
            attempted.add(index)
            record = await self.runs.save_tool_result(context.run_id, context.session_id, identities[index],
                                                      outcome.result, outcome.message)
            on_committed(record)
            committed[index] = record

        async def wait_ready(ready):
            await asyncio.wait({ready, failure}, return_when=asyncio.FIRST_COMPLETED)
            if failure.done():
                raise failure.result()

        async def execute_one(index):
            call = calls[index]
            updates = asyncio.Queue(maxsize=1)
            reporter = None
            accepting = True

            async def report_updates():
                while (preview := await updates.get()) is not None:
                    await self.runs.event(context.run_id, "tool_progress", {
                        "tool_call_id": call.id, "name": call.name, "text": preview})

            def report_failure(task):
                if not task.cancelled():
                    error = task.exception()
                    if error is not None and not failure.done() and not signal.is_set():
                        failure.set_result(error)

            def on_update(partial):
                nonlocal reporter
                if not accepting:
                    return
                # Progress is a bounded preview, never the authoritative result.
                preview = "\n".join(getattr(block, "text", "")[:2048] for block in partial.content[:4])[:2048]
                if updates.full():
                    updates.get_nowait()
                updates.put_nowait(preview)
                if reporter is None:
                    reporter = asyncio.create_task(report_updates(), name=f"tool-progress-{call.id}")
                    reporter.add_done_callback(report_failure)

            started = monotonic_ns()
            try:
                result = await executor.execute(call, context=context, signal=signal, on_update=on_update)
                self.progress.completed(context.run_id, call, executor.registry.get(call.name), result)
                outcomes[index] = ToolOutcome(result, ToolResultMessage(
                    tool_call_id=call.id, tool_name=call.name, content=result.content,
                    is_error=result.is_error, timestamp=time_ns() // 1_000_000,
                    details=result.details, usage=result.usage,
                    duration_ms=(monotonic_ns() - started) // 1_000_000))
                accepting = False
                if reporter is not None:
                    await updates.put(None)
                    await reporter
            finally:
                accepting = False
                if reporter is not None:
                    if not reporter.done():
                        reporter.cancel()
                    await asyncio.gather(reporter, return_exceptions=True)

        async def run_group(parallel, indices):
            nonlocal workers, commit_task
            width = min(self.max_concurrency, len(indices)) if parallel else 1
            iterator = iter(indices)
            ready = {index: asyncio.get_running_loop().create_future() for index in indices}
            # At most width executed-but-uncommitted outcomes: a slow first
            # result cannot cause unbounded buffering behind it.
            window = asyncio.Semaphore(width)

            async def worker():
                nonlocal stalled
                try:
                    for index in iterator:
                        await window.acquire()
                        async with self._slots:
                            if signal.is_set():
                                return
                            call = calls[index]
                            if index in refused:
                                # No slot needed for a decision already made; the
                                # refusal is committed like any other tool error.
                                outcomes[index] = refused_outcome(call, refused[index])
                                ready[index].set_result(None)
                                continue
                            granted = grants.get(index)
                            if granted is not None:
                                spent = await self.approvals.consume(
                                    granted, run_id=context.run_id, tool_record_id=identities[index],
                                    tool_name=call.name,
                                    arguments=call.arguments, cwd=str(context.workspace))
                                if not spent:
                                    outcomes[index] = refused_outcome(
                                        call, approval_result(STALE, granted, "stale"))
                                    ready[index].set_result(None)
                                    continue
                            decision: Decision = self.progress.check(
                                context.run_id, call, executor.registry.get(call.name))
                            if not decision.execute:
                                # Never executed: no tool_started event, and the
                                # result is committed through the same path as a
                                # normal tool error so the transcript stays whole.
                                stalled = stalled or decision.stop
                                outcomes[index] = ToolOutcome(decision.result, ToolResultMessage(
                                    tool_call_id=call.id, tool_name=call.name,
                                    content=decision.result.content, is_error=True,
                                    timestamp=time_ns() // 1_000_000, details=decision.result.details))
                                ready[index].set_result(None)
                                continue
                            await self.runs.start_tool_call(context.run_id, model_call_id, call,
                                                            record_id=identities[index])
                            await execute_one(index)
                            ready[index].set_result(None)
                except BaseException as error:
                    if not failure.done() and not signal.is_set():
                        failure.set_result(error)

            workers = [asyncio.create_task(worker(), name=f"tool-worker-{context.run_id}-{i}") for i in range(width)]
            try:
                for index in indices:
                    await wait_ready(ready[index])
                    # Shield only persistence, not execution. Cancellation waits
                    # for this commit's outcome before attempting cleanup.
                    commit_task = asyncio.create_task(commit(index))
                    await asyncio.shield(commit_task)
                    commit_task = None
                    window.release()
                await asyncio.gather(*workers)
                workers = []
            except BaseException:
                signal.set()
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                if commit_task is not None:
                    await asyncio.gather(commit_task, return_exceptions=True)
                # Preserve known finished work even if an earlier call was cancelled.
                # Never re-execute a tool or retry an uncertain persistence attempt.
                for index in sorted(outcomes.keys() - attempted):
                    try:
                        await commit(index)
                    except Exception:
                        break  # Remaining running rows become unknown in finish().
                raise

        workspace = context.workspace.resolve()
        gate = self._gates.setdefault(workspace, _WorkspaceGate())
        gate.users += 1
        try:
            for parallel, indices in self._groups(calls, executor):
                # Questions are asked before a slot, the workspace gate or an
                # execution slot is taken, so an unanswered one blocks only this run.
                refused, grants = await self._ask(calls, indices, context, executor, identities,
                                                  signal, budget)
                async with gate.hold(parallel):
                    # Cleanup must finish while still holding the gate: cancelled
                    # read threads may still be accessing this workspace.
                    await run_group(parallel, indices)
        finally:
            gate.users -= 1
            if not gate.users:
                del self._gates[workspace]

        return ToolBatchResult([outcomes[i].result for i in range(len(calls))],
                               [committed[i].message for i in range(len(calls))], stalled)

    async def _ask(self, calls, indices, context, executor, identities, signal, budget):
        """Run optional gates before taking execution resources."""
        refused: dict[int, object] = {}
        grants: dict[int, UUID] = {}
        gates = self.gates if self.gates is not None else self.default_gates()
        for index in indices:
            call = calls[index]
            for gate in gates:
                try:
                    decision = await gate.check(call, context=context, tool_record_id=identities[index],
                                                signal=signal, budget=budget)
                except Exception as error:
                    refused[index] = approval_result("Tool gate is unavailable; the tool was not executed.",
                                                     None, "unavailable")
                    await self.runs.event(context.run_id, "tool_call_refused", {
                        "tool_call_id": call.id, "name": call.name,
                        "gate": gate.name, "reason": "unavailable",
                        "error_type": type(error).__name__,
                    })
                    break
                if not decision.allow:
                    refused[index] = approval_result(decision.reason, decision.approval_id, decision.code)
                    break
                if decision.approval_id is not None:
                    grants[index] = decision.approval_id
        return refused, grants


def approval_result(message: str, approval_id, reason: str):
    """A refusal that says what happened, so the model does not retry blindly."""
    return AgentToolResult(
        content=[TextContent(message)], is_error=True,
        details={"approval": reason, "approval_id": str(approval_id) if approval_id else None,
                 "execution_started": False},
    )


def refused_outcome(call: ToolCall, result: AgentToolResult) -> ToolOutcome:
    return ToolOutcome(result, ToolResultMessage(
        tool_call_id=call.id, tool_name=call.name, content=result.content, is_error=True,
        timestamp=time_ns() // 1_000_000, details=result.details))
