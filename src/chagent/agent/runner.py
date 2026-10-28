"""Model/tool loop with commit-driven state, bounded batches and explicit endings."""

import asyncio
import math
from collections.abc import Callable
from time import time_ns
from uuid import UUID

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from chagent.agent.types import FinishDecision, RunState
from chagent.agent.stream_writer import TextStreamWriter
from chagent.context.builder import ContextBudgetError, ContextBuilder
from chagent.context.manager import ContextManager
from chagent.memory.service import MemoryService
from chagent.domain.messages import AssistantMessage, ToolCall, UserMessage
from chagent.domain.runs import RunEndReason, RunResult
from chagent.models.base import Model, ModelClient, ModelRequest
from chagent.models.events import ContentDeltaEvent, DoneEvent, ErrorEvent, StartEvent
from chagent.storage.repositories import ConversationRepository, RunRepository, RunOwnershipLost
from chagent.tools.executor import ToolExecutor
from chagent.tools.base import AgentToolResult
from chagent.tools.context import ToolContext
from chagent.tools.scheduler import ToolScheduler


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
        self.conversations = conversations
        self.runs = runs
        self.scheduler = ToolScheduler(runs, max_concurrency=max_tool_concurrency)
        self.max_turns = max_turns
        self.timeout = timeout
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._states: dict[UUID, RunState] = {}

    def cancel(self, run_id: UUID) -> bool:
        task = self._tasks.get(run_id)
        # A second cancellation must not interrupt persistence of the terminal state.
        return (True if task.cancelling() else task.cancel()) if task is not None else False

    async def run(self, session_id: UUID, text: str, *, on_started: Callable[[UUID], None] | None = None) -> RunResult:
        if not text.strip():
            raise ValueError("Input must not be empty")
        # Also recover on CLI entry and before a new web run is admitted.
        while await self.runs.reap_expired():
            pass
        run_id = await self.runs.begin(
            session_id, UserMessage(content=text, timestamp=time_ns() // 1_000_000),
        )
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
        try:
            if on_started is not None:
                on_started(run_id)
            async with asyncio.timeout(self.timeout):
                state = await self._load_state(run_id, session_id)
                self._states[run_id] = state
                result = await self._loop(state)
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
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            self.runs.database.run_owners.pop(run_id, None)
            self._tasks.pop(run_id, None)
            self._states.pop(run_id, None)
            if self.context_manager is not None:
                self.context_manager.release(run_id)
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

    async def _load_state(self, run_id: UUID, session_id: UUID) -> RunState:
        session = await self.conversations.get_session(session_id)
        history = await self.conversations.messages(session_id, current_run=run_id, project_artifacts=True)
        task_state = await self.conversations.task_state(session_id)
        summary = await self.context_manager.summaries.latest(session_id, current_run=run_id) if self.context_manager else None
        return RunState(run_id, session, history, task_state, summary)

    async def _loop(self, state: RunState) -> RunResult:
        run_id, session_id = state.run_id, state.session.id
        tool_context = ToolContext(
            workspace=state.session.workspace, session_id=session_id, run_id=run_id,
        )
        for iteration in range(self.max_turns):
            state.iteration = iteration
            if state.tool_revision != self.tools.registry.revision:
                state.tools = self.tools.registry.definitions()
                state.tool_revision = self.tools.registry.revision
                state.context_revision += 1
            try:
                if self.context_manager is not None:
                    built = await self.context_manager.prepare(session_id, run_id, self.model, self.client,
                                                                state.tools, state=state)
                else:
                    built = await self.context.build(state.history, self.model, state.tools,
                                                     task_state=state.task_state, current_run_id=run_id)
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
                                                        executor=self.tools, on_committed=state.append_committed)
            decision = decide_next_step(batch.results)
            if decision.action == "complete":
                return RunResult(run_id, session_id, None, decision.reason, batch.messages)
        raise RunExecutionError("Maximum model turns exceeded", "turn_limit")

    async def _response(self, run_id: UUID, request: ModelRequest) -> AssistantMessage:
        async def save(delta: str, count: int):
            await self.runs.event(run_id, "text_delta", {"delta": delta, "count": count})

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
        try:
            done, _ = await asyncio.wait({consumer, writer.task}, return_when=asyncio.FIRST_COMPLETED)
            if writer.task in done:
                # A failed write interrupts a blocked provider, even when the
                # producer has not filled the buffer or received another delta.
                await writer.task
            return await consumer
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
