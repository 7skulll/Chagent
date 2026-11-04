"""Model/tool loop with commit-driven state, bounded batches and explicit endings."""

import asyncio
import logging
import math
from collections.abc import Callable
from time import time_ns
from uuid import UUID

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from chagent.boundaries import validate_workspace
from chagent.agent.budget import RunBudget
from chagent.agent.types import FinishDecision, RunState
from chagent.agent.stream_writer import TextStreamWriter
from chagent.context.builder import ContextBudgetError, ContextBuilder
from chagent.context.manager import ContextManager
from chagent.memory.service import MemoryService
from chagent.domain.memories import MemoryProposal
from chagent.domain.messages import AssistantMessage, TextContent, ToolCall, UserMessage
from chagent.domain.recovery import build_recovery_note
from chagent.domain.runs import RunEndReason, RunResult
from chagent.models.base import Model, ModelClient, ModelRequest
from chagent.models.events import ContentDeltaEvent, DoneEvent, ErrorEvent, StartEvent
from chagent.storage.repositories import ConversationRepository, RunRepository, RunOwnershipLost
from chagent.tools.executor import ToolExecutor
from chagent.tools.base import AgentToolResult
from chagent.tools.context import ToolContext
from chagent.tools.scheduler import ToolScheduler

logger = logging.getLogger(__name__)


class RunFailedError(RuntimeError):
    def __init__(self, run_id: UUID, reason: str, end_reason: RunEndReason = "execution_error"):
        self.run_id = run_id
        self.end_reason = end_reason
        super().__init__(reason)


class RunExecutionError(RuntimeError):
    """A classified runtime failure, separate from tool-level error results."""

    def __init__(self, reason: str, end_reason: RunEndReason):
        self.end_reason = end_reason
        super().__init__(reason)


def decide_next_step(results: list[AgentToolResult]) -> FinishDecision:
    if not results:
        return FinishDecision("complete", "model_stop")
    if all(result.terminate for result in results):
        return FinishDecision("complete", "tool_requested_stop")
    return FinishDecision("continue", "tool_results_ready")


class AgentRunner:
    heartbeat_interval = 5.0

    def __init__(
        self, model: Model, client: ModelClient, context: ContextBuilder,
        tools: ToolExecutor, conversations: ConversationRepository, runs: RunRepository,
        max_turns: int = 8, timeout: float = 60,
        context_manager: ContextManager | None = None,
        memory_service: MemoryService | None = None,
        max_tool_concurrency: int = 4,
        memory_worker=None,
    ):
        if max_turns < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Run limits must be positive and finite")
        self.model = model
        self.client = client
        self.context = context
        self.context_manager = context_manager
        self.memory_service = memory_service
        self.memory_worker = memory_worker
        self.tools = tools
        #: Legacy mode mapping. Unified production runners map both values to
        #: the same executor; diagnostic runners can keep separate faces.
        self.tool_faces: dict[str, ToolExecutor] | None = None
        self.conversations = conversations
        self.runs = runs
        self.scheduler = ToolScheduler(runs, max_concurrency=max_tool_concurrency)
        self.max_turns = max_turns
        self.timeout = timeout
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._states: dict[UUID, RunState] = {}
        # Set by bootstrap; absent in tests that drive the loop directly.
        self.sandbox = None
        # Session processes outlive their invocation, never their run.
        self.processes = None
        self._workspace_identities = {}

    def cancel(self, run_id: UUID) -> bool:
        task = self._tasks.get(run_id)
        # A second cancellation must not interrupt persistence of the terminal state.
        return (True if task.cancelling() else task.cancel()) if task is not None else False

    async def run(self, session_id: UUID, text: str = "", *, supersede_from: int | None = None,
                  on_started: Callable[[UUID], None] | None = None) -> RunResult:
        """Drive one run to a terminal state.

        ``supersede_from`` retires a suffix of the transcript in the same
        transaction that opens the run: a regeneration passes it with no text
        (the question is already there), an edit passes it with the new text.
        """
        if not text.strip() and supersede_from is None:
            raise ValueError("Input must not be empty")
        # Also recover on CLI entry and before a new web run is admitted.
        while await self.runs.reap_expired():
            pass
        message = (UserMessage(content=text, timestamp=time_ns() // 1_000_000)
                   if text.strip() else None)
        run_id = await self.runs.begin(session_id, message, supersede_from=supersede_from)
        task = asyncio.current_task()
        if task is not None:
            self._tasks[run_id] = task
        heartbeat_error = None

        async def heartbeat():
            nonlocal heartbeat_error
            try:
                while True:
                    await asyncio.sleep(self.heartbeat_interval)
                    await self.runs.heartbeat(run_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                heartbeat_error = error
                if task is not None:
                    task.cancel()

        heartbeat_task = asyncio.create_task(heartbeat(), name=f"run-heartbeat-{run_id}")
        # A pausable budget rather than asyncio.timeout: waiting for a human is
        # not the run spending its execution time (see agent/budget.py).
        budget = RunBudget(self.timeout, lambda: task.cancel() if task is not None else None)
        try:
            budget.start()
            if on_started is not None:
                on_started(run_id)
            state = await self._load_state(run_id, session_id)
            self._states[run_id] = state
            command = (self.memory_service.explicit_command(state)
                       if self.memory_service is not None else None)
            result = (await self._explicit_memory(state, command) if command is not None
                      else await self._loop(state, budget))
            async def commit(connection):
                # Explicit memories are deterministic; automatic extraction only
                # enqueues a job so this answer never waits for a second model call.
                await self.memory_service.on_completed(
                    connection, session_id, run_id, state=state, has_answer=result.message is not None)
            await self.runs.finish(run_id, "completed", end_reason=result.end_reason,
                                   on_completed=commit if self.memory_service is not None else None)
            if self.memory_worker is not None and result.message is not None:
                self.memory_worker.wake()
        except asyncio.CancelledError:
            if heartbeat_error is not None:
                raise RunFailedError(run_id, "Run heartbeat failed; ownership is uncertain", "storage_error") from heartbeat_error
            if budget.expired:
                await self._close_if_running(run_id, "failed", "Run exceeded its time budget", "run_timeout")
                raise RunFailedError(run_id, "Run exceeded its time budget", "run_timeout") from None
            await self._close_if_running(run_id, "cancelled", "Run cancelled", "user_cancelled")
            raise
        except RunOwnershipLost as error:
            raise RunFailedError(run_id, str(error), "storage_error") from error
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
            if isinstance(error, RunExecutionError):
                end_reason = error.end_reason
            elif isinstance(error, ContextBudgetError):
                end_reason = "context_budget_exhausted"
            elif isinstance(error, TimeoutError):
                end_reason = "run_timeout"
            elif isinstance(error, (DatabaseError, PoolTimeout)):
                end_reason = "storage_error"
            else:
                end_reason = "execution_error"
            status = "cancelled" if end_reason == "model_aborted" else "failed"
            await self._close_if_running(run_id, status, reason, end_reason)
            raise RunFailedError(run_id, reason, end_reason) from error
        else:
            return result
        finally:
            budget.stop()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            processes_reaped = self.processes is None
            if self.processes is not None:
                try:
                    # A session process deliberately outlives its tool call, so
                    # nothing else would reap it: the run's end is the only
                    # promise the child gets. A cancelled run must still do this
                    # before its terminal state is written.
                    await self.processes.terminate_run(run_id)
                    processes_reaped = True
                except BaseException as error:
                    # Keeping the run's outcome and terminal state matters more
                    # than the failure here, but a leaked process group is worth
                    # a line: nothing else will reap it.
                    logger.warning("Could not terminate run processes (%s)", type(error).__name__)
            if self.sandbox is not None and processes_reaped:
                try:
                    self.sandbox.release(run_id)
                except OSError:
                    logger.exception("Could not remove run scratch")
            try:
                # Stops the flush task and commits or drops the tail. A cancelled
                # run may raise here; it must never mask the run's own outcome.
                await self.runs.close_events(run_id)
            except BaseException as error:
                logger.warning("Could not close the event batch (%s)", type(error).__name__)
            self.runs.database.run_owners.pop(run_id, None)
            self._tasks.pop(run_id, None)
            self._states.pop(run_id, None)
            if self.context_manager is not None:
                self.context_manager.release(run_id)
            try:
                # A pending question must not outlive its run.
                await self.scheduler.cancel_approvals(run_id)
            except BaseException as error:
                logger.warning("Could not cancel pending approvals (%s)", type(error).__name__)
            try:
                async with self.runs.database.connection() as connection:
                    await connection.execute("UPDATE file_outputs SET status='cancelled',payload=NULL WHERE run_id=%s AND status='pending' AND EXISTS (SELECT 1 FROM runs WHERE id=%s AND status='cancelled')", (run_id,run_id))
            except Exception as error:
                # Pending outputs then stay pending until expiry; say so rather
                # than leaving a row the user cannot explain.
                logger.warning("Could not cancel pending file outputs (%s)", type(error).__name__)
            self.scheduler.release(run_id)
            if self.memory_service is not None:
                self.memory_service.release(run_id)

    async def _close_if_running(self, run_id: UUID, status: str, reason: str, end_reason: RunEndReason) -> None:
        """Close a run from an exception handler without leaking storage errors.

        ``finish`` re-checks ownership, and an exception raised inside an
        ``except`` block is not caught by a sibling ``except RunOwnershipLost``:
        it would escape ``run`` as an unclassified exception. Ownership loss is
        reported as a classified failure instead, and the row is left to the
        reaper rather than falsely marked cancelled.
        """
        row = await self.runs.get(run_id)
        if row is None or row["status"] != "running":
            return
        try:
            await self.runs.finish(run_id, status, reason, end_reason=end_reason)
        except RunOwnershipLost as error:
            raise RunFailedError(run_id, str(error), "storage_error") from error

    def executor_for(self, mode: str | None) -> ToolExecutor:
        """The tool face one run may use, chosen once and never switched.

        Disabled modes are refused here as well as by the HTTP layer, so
        restoring an old session through Python cannot silently change its tools.
        """
        faces = self.tool_faces
        if faces is None:
            return self.tools
        if (mode or "work") not in faces:
            raise ValueError("Session mode is disabled")
        return faces[mode or "work"]

    def prompt_mode_for(self, mode: str | None) -> str | None:
        if getattr(self, "unified", False):
            return "unified"
        return mode if self.tool_faces is not None else None

    async def _load_state(self, run_id: UUID, session_id: UUID) -> RunState:
        session = await self.conversations.get_session(session_id)
        root = validate_workspace(session.workspace)
        info = root.stat()
        identity = (info.st_dev, info.st_ino)
        previous = self._workspace_identities.setdefault(session_id, identity)
        if previous != identity:
            raise PermissionError("Session workspace directory was replaced")
        history = await self.conversations.messages(session_id, current_run=run_id, project_artifacts=True)
        task_state = await self.conversations.task_state(session_id)
        summary = await self.context_manager.summaries.latest(session_id, current_run=run_id) if self.context_manager else None
        note = build_recovery_note(await self.runs.recovery_facts(session_id, current_run=run_id))
        if note is not None:
            # Audit only: the note itself is rebuilt from structured records and
            # never persisted as a message, so it cannot be replayed or paired
            # with a tool call.
            await self.runs.event(run_id, "recovery_projected", {
                "fact_count": note.fact_count, "source_runs": list(note.run_ids),
            })
        state = RunState(run_id, session, history, task_state, summary, recovery=note, workspace_identity=identity)
        if self.sandbox is not None:
            settings = self.sandbox.settings
            state.environment = (
                "Boundary: container. Commands run inside the container, can access the network, "
                "and see only mounted workspaces. Maven, Java and Node are installed. "
                "Use process(action='read' or 'stop') for long-running commands; do not use nohup."
                if settings.containerized else
                "Boundary: isolated. Commands run in a writable workspace sandbox with network disabled."
                if settings.mode == "isolated" else None
            )
        if self.memory_service is not None and self.memory_service.explicit_command(state) is not None:
            # The command is answered without a model, so nothing reads the
            # workspace: skipping the scan keeps it out of large trees' way.
            return state
        if self.sandbox is not None:
            # Run-static, like the environment snapshot: the probe is cached per
            # policy, so this costs one subprocess sweep per process, not per run.
            await self.runs.event(run_id, "sandbox_applied", await self.sandbox.audit(session.workspace, owner=run_id))
        return state

    async def _explicit_memory(self, state: RunState, proposal: MemoryProposal) -> RunResult:
        """Answer a deterministic memory command without a provider call.

        The row itself is written by ``on_completed`` inside the completion
        transaction, so a model turn could only restate it. Skipping the model
        is not an optimisation: handed the command plus a workspace snapshot, a
        model reliably writes its own ``MEMORY.md`` as well, and that file
        returns on the next run through ``AGENTS.md`` as a second, unreviewed
        copy of the same fact.
        """
        run_id, session_id = state.run_id, state.session.id
        answer = f"已记录到长期记忆（{proposal.scope}）：{proposal.key}"
        message = AssistantMessage([TextContent(answer)], self.model.api, self.model.provider,
                                   self.model.id, time_ns() // 1_000_000, "stop")
        await self.runs.emit(run_id, "text_delta", {"delta": answer, "count": 1})
        await self.runs.event(run_id, "memory_command_resolved",
                              {"scope": proposal.scope, "key": proposal.key})
        state.append_committed(await self.runs.save_notice(run_id, session_id, message))
        await self.runs.flush_events(run_id)
        return RunResult(run_id, session_id, message, "explicit_memory")

    async def _loop(self, state: RunState, budget=None) -> RunResult:
        run_id, session_id = state.run_id, state.session.id
        # Legacy session mode still selects its stored face in diagnostic builds.
        # Production unified runners map both legacy values to one executor.
        executor = self.executor_for(state.session.mode)
        prompt_mode = self.prompt_mode_for(state.session.mode)
        tool_context = ToolContext(
            workspace=state.session.workspace, session_id=session_id, run_id=run_id,
            workspace_identity=state.workspace_identity,
        )
        for iteration in range(self.max_turns):
            from chagent.tools.workspace import WorkspaceFS
            WorkspaceFS(state.session.workspace, expected_identity=state.workspace_identity)
            state.iteration = iteration
            if state.tool_revision != executor.registry.revision:
                state.tools = executor.registry.definitions()
                state.tool_revision = executor.registry.revision
                state.context_revision += 1
            try:
                if self.context_manager is not None:
                    built = await self.context_manager.prepare(session_id, run_id, self.model, self.client,
                                                                state.tools, state=state, prompt_mode=prompt_mode)
                else:
                    built = await self.context.build(state.history, self.model, state.tools,
                                                     task_state=state.task_state, current_run_id=run_id,
                                                     recovery=state.recovery, environment=state.environment)
            except ContextBudgetError as error:
                await self.runs.event(run_id, "context_rejected", {
                    "reason": str(error), "required_tokens": error.required_tokens,
                    "input_budget": error.input_budget,
                })
                raise
            if self.memory_service is not None:
                built = await self.memory_service.enrich(built, session_id, run_id, state=state)
            model_call_id = await self.runs.start_model_call(run_id, iteration, built)
            state.active_model_call_id = model_call_id
            message = await self._response(run_id, built.request)
            record = await self.runs.save_response(run_id, session_id, model_call_id, message)
            state.append_committed(record)
            state.active_model_call_id = None
            if message.stop_reason in {"error", "aborted"}:
                raise RunExecutionError(message.error_message or "Model request failed",
                                        "model_aborted" if message.stop_reason == "aborted" else "model_error")
            calls = [block for block in message.content if isinstance(block, ToolCall)]
            if message.stop_reason == "length":
                # Even parseable arguments may be silently incomplete after truncation.
                raise RunExecutionError("Model output was truncated; tools were not executed", "output_limit")
            if not calls:
                if message.stop_reason != "stop":
                    raise RunExecutionError("Model requested tools but supplied no calls", "model_protocol_error")
                decision = decide_next_step([])
                return RunResult(run_id, session_id, message, decision.reason)
            if message.stop_reason != "toolUse":
                raise RunExecutionError("Tool calls require toolUse stop reason", "model_protocol_error")
            if len({call.id for call in calls}) != len(calls):
                raise RunExecutionError("Duplicate tool call IDs in one response", "model_protocol_error")
            batch = await self.scheduler.execute_batch(calls, context=tool_context, model_call_id=model_call_id,
                                                        executor=executor, on_committed=state.append_committed,
                                                        budget=budget)
            if batch.stalled:
                # The model was already told the call is unchanged; repeating it
                # again converts a stalled attempt into an explicit ending.
                raise RunExecutionError("Repeated tool calls made no progress", "repeated_tool_call")
            decision = decide_next_step(batch.results)
            if decision.action == "complete":
                return RunResult(run_id, session_id, None, decision.reason, batch.messages)
        raise RunExecutionError("Maximum model turns exceeded", "turn_limit")

    async def _response(self, run_id: UUID, request: ModelRequest) -> AssistantMessage:
        async def save(delta: str, count: int):
            # Buffered: the provider stream is never gated by a commit. Order is
            # kept because every other write to this run flushes the buffer
            # first, and this response flushes before it returns.
            await self.runs.emit(run_id, "text_delta", {"delta": delta, "count": count})

        writer = TextStreamWriter(save)

        async def consume():
            started = False
            final: AssistantMessage | None = None
            stream = self.client.stream(request)
            try:
                async for event in stream:
                    if final is not None:
                        raise RunExecutionError("Model emitted an event after its terminal event", "model_protocol_error")
                    if not isinstance(event, ContentDeltaEvent) or event.type != "text_delta":
                        await writer.flush()
                    if isinstance(event, StartEvent):
                        if started:
                            raise RunExecutionError("Model emitted duplicate start events", "model_protocol_error")
                        started = True
                    elif isinstance(event, ErrorEvent):
                        if event.error.stop_reason != event.reason:
                            raise RunExecutionError("Inconsistent model error reason", "model_protocol_error")
                        final = event.error
                    elif not started:
                        raise RunExecutionError("Model emitted an update before start", "model_protocol_error")
                    elif isinstance(event, DoneEvent):
                        if event.message.stop_reason != event.reason:
                            raise RunExecutionError("Inconsistent model completion reason", "model_protocol_error")
                        final = event.message
                    elif isinstance(event, ContentDeltaEvent) and event.type == "text_delta":
                        await writer.append(event.delta)
                if final is None:
                    raise RunExecutionError("Model stream ended without a terminal event", "model_protocol_error")
                await writer.close()
                return final
            finally:
                if close := getattr(stream, "aclose", None):
                    await close()

        consumer = asyncio.create_task(consume(), name=f"model-stream-{run_id}")
        failure = self.runs.event_failure(run_id)
        waiters = {consumer, writer.task, failure}
        try:
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if writer.task in done:
                # A failed write interrupts a blocked provider, even when the
                # producer has not filled the buffer or received another delta.
                await writer.task
            if failure in done:
                # A rejected event batch must stop the run: output produced from
                # here on could never be recorded.
                await failure
            message = await consumer
            # One barrier per response: every delta of this answer is committed
            # before model_completed, so replay never shows text after the end.
            await self.runs.flush_events(run_id)
            return message
        except BaseException:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            try:
                # Best effort only: preserve the original cancel/model/storage
                # error, and never let an unavailable DB retain this writer.
                async with asyncio.timeout(2):
                    await writer.close()
            except BaseException:
                await writer.stop()
            raise
        finally:
            failure.cancel()
            await asyncio.gather(failure, return_exceptions=True)
