"""Sequential model/tool loop with commit-driven state and explicit endings."""

import asyncio
import math
from collections.abc import Callable
from time import monotonic_ns, time_ns
from uuid import UUID

from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout

from chagent.agent.types import FinishDecision, RunState
from chagent.context.builder import ContextBudgetError, ContextBuilder
from chagent.context.manager import ContextManager
from chagent.memory.service import MemoryService
from chagent.domain.messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from chagent.domain.runs import RunEndReason, RunResult
from chagent.models.base import Model, ModelClient, ModelRequest
from chagent.models.events import ContentDeltaEvent, DoneEvent, ErrorEvent, StartEvent
from chagent.storage.repositories import ConversationRepository, RunRepository
from chagent.tools.executor import ToolExecutor
from chagent.tools.base import AgentToolResult
from chagent.tools.context import ToolContext


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
    def __init__(
        self, model: Model, client: ModelClient, context: ContextBuilder,
        tools: ToolExecutor, conversations: ConversationRepository, runs: RunRepository,
        max_turns: int = 8, timeout: float = 60,
        context_manager: ContextManager | None = None,
        memory_service: MemoryService | None = None,
    ):
        if max_turns < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Run limits must be positive and finite")
        self.model = model
        self.client = client
        self.context = context
        self.context_manager = context_manager
        self.memory_service = memory_service
        self.tools = tools
        self.conversations = conversations
        self.runs = runs
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
        run_id = await self.runs.begin(
            session_id, UserMessage(content=text, timestamp=time_ns() // 1_000_000),
        )
        task = asyncio.current_task()
        if task is not None:
            self._tasks[run_id] = task
        try:
            if on_started is not None:
                on_started(run_id)
            async with asyncio.timeout(self.timeout):
                state = await self._load_state(run_id, session_id)
                self._states[run_id] = state
                result = await self._loop(state)
                proposals = (await self.memory_service.prepare_completed(
                    session_id, run_id, self.model, self.client, state=state)
                    if self.memory_service and result.message is not None else [])
            async def commit(connection):
                await self.memory_service.commit(connection, session_id, run_id, proposals)
            await self.runs.finish(run_id, "completed", end_reason=result.end_reason,
                                   on_completed=commit if proposals else None)
        except asyncio.CancelledError:
            if (await self.runs.get(run_id))["status"] == "running":
                await self.runs.finish(run_id, "cancelled", "Run cancelled", end_reason="user_cancelled")
            raise
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
            if (await self.runs.get(run_id))["status"] == "running":
                status = "cancelled" if end_reason == "model_aborted" else "failed"
                await self.runs.finish(run_id, status, reason, end_reason=end_reason)
            raise RunFailedError(run_id, reason, end_reason) from error
        else:
            return result
        finally:
            self._tasks.pop(run_id, None)
            self._states.pop(run_id, None)
            if self.context_manager is not None:
                self.context_manager.release(run_id)

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
            results, result_messages = [], []
            for call in calls:
                record_id = await self.runs.start_tool_call(run_id, model_call_id, call)
                started = monotonic_ns()
                result = await self.tools.execute(call, context=tool_context)
                result_message = ToolResultMessage(
                    tool_call_id=call.id, tool_name=call.name, content=result.content,
                    is_error=result.is_error, timestamp=time_ns() // 1_000_000,
                    details=result.details, usage=result.usage,
                    duration_ms=(monotonic_ns() - started) // 1_000_000,
                )
                record = await self.runs.save_tool_result(run_id, session_id, record_id, result, result_message)
                state.append_committed(record)
                results.append(result)
                result_messages.append(record.message)
            decision = decide_next_step(results)
            if decision.action == "complete":
                return RunResult(run_id, session_id, None, decision.reason, result_messages)
        raise RunExecutionError("Maximum model turns exceeded", "turn_limit")

    async def _response(self, run_id: UUID, request: ModelRequest) -> AssistantMessage:
        started = False
        final: AssistantMessage | None = None
        async for event in self.client.stream(request):
            if final is not None:
                raise RunExecutionError("Model emitted an event after its terminal event", "model_protocol_error")
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
                await self.runs.event(run_id, "text_delta", {"delta": event.delta})
        if final is None:
            raise RunExecutionError("Model stream ended without a terminal event", "model_protocol_error")
        return final
