"""Acceptance checks for ending contracts and commit-driven invocation state."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.runner import RunFailedError
from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.domain.memories import MemoryPolicy
from chagent.domain.messages import AssistantMessage, ToolCall, UserMessage
from chagent.domain.runs import StoredMessage
from chagent.models.events import DoneEvent, ErrorEvent, StartEvent
from chagent.models.fake import FakeModelClient
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.tools.echo import EchoTool
from test_long_runs import LONG_RUN_WINDOW, LONG_TOOL_RESULT_CHARS, EmitTool, LongClient


class StopTool(EchoTool):
    def __init__(self):
        super().__init__()
        self.name = "stop"

    async def execute(self, *args, **kwargs):
        result = await super().execute(*args, **kwargs)
        result.terminate = True
        return result


class BatchClient(FakeModelClient):
    def __init__(self, tools):
        super().__init__()
        self.tools, self.requests = tools, []

    async def stream(self, request):
        self.requests.append(deepcopy(request))
        if isinstance(request.messages[-1], UserMessage):
            message = AssistantMessage([], request.model.api, request.model.provider, request.model.id, 0, "pending")
            yield StartEvent(message)
            message = deepcopy(message)
            message.content = [ToolCall(f"batch-{i}", name, {"text": name}) for i, name in enumerate(self.tools)]
            message.stop_reason = "toolUse"
            yield DoneEvent("toolUse", message)
        else:
            async for event in super().stream(request):
                yield event


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class RunStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_state_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.runner = create_demo_runner(self.database, memory_policy=MemoryPolicy(enabled=False))
        # Empty workspace: the run-static environment snapshot is part of the
        # prompt and these tests size model windows for compaction arithmetic.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.session = await self.runner.conversations.create_session(Path(self.temporary.name))

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def ending(self, identity, status, reason):
        row = await self.runner.runs.get(identity)
        self.assertEqual((row["status"], row["end_reason"]), (status, reason))
        event = (await self.runner.runs.events(identity))[-1]
        self.assertEqual(event["type"], "run_" + status)
        self.assertEqual(event["payload"]["end_reason"], reason)
        self.assertEqual(self.runner._states, {})
        self.assertEqual(self.runner._tasks, {})

    def capture_states(self):
        states = []
        original = self.runner.context_manager.prepare

        async def prepare(*args, **kwargs):
            states.append(kwargs["state"])
            return await original(*args, **kwargs)

        self.runner.context_manager.prepare = prepare
        return states

    async def test_natural_ending_matches_result_database_and_event(self):
        result = await self.runner.run(self.session, "hello")
        self.assertEqual(result.end_reason, "model_stop")
        self.assertIsNotNone(result.message)
        self.assertEqual(result.tool_results, [])
        await self.ending(result.run_id, "completed", "model_stop")

    async def test_tool_stop_has_no_fabricated_reply_and_can_continue_after_restart(self):
        self.runner.tools.registry.register(StopTool())
        client = BatchClient(["stop", "stop"])
        self.runner.client = client
        result = await self.runner.run(self.session, "stop")
        self.assertIsNone(result.message)
        self.assertEqual(result.end_reason, "tool_requested_stop")
        self.assertEqual(len(result.tool_results), 2)
        self.assertEqual(len(client.requests), 1)
        await self.ending(result.run_id, "completed", "tool_requested_stop")
        history = await self.runner.conversations.messages(self.session)
        self.assertEqual([item.message.role for item in history], ["user", "assistant", "toolResult", "toolResult"])
        fresh = create_demo_runner(self.database, memory_policy=MemoryPolicy(enabled=False))
        resumed = await fresh.run(self.session, "continue")
        self.assertEqual(resumed.end_reason, "model_stop")
        self.assertEqual(len(await fresh.conversations.messages(self.session)), 8)

    async def test_mixed_stop_batch_continues_like_pi(self):
        self.runner.tools.registry.register(StopTool())
        client = BatchClient(["stop", "echo"])
        self.runner.client = client
        result = await self.runner.run(self.session, "mixed batch")
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(result.end_reason, "model_stop")
        await self.ending(result.run_id, "completed", "model_stop")

    async def test_turn_budget_is_distinct_from_tool_stop(self):
        self.runner.max_turns = 1
        with self.assertRaises(RunFailedError) as error:
            await self.runner.run(self.session, "one iteration")
        self.assertEqual(error.exception.end_reason, "turn_limit")
        await self.ending(error.exception.run_id, "failed", "turn_limit")

    async def test_context_budget_has_explicit_reason(self):
        self.runner.model.context_window = 1000
        with self.assertRaises(RunFailedError) as error:
            await self.runner.run(self.session, "x" * 2000)
        await self.ending(error.exception.run_id, "failed", "context_budget_exhausted")

    async def test_timeout_and_user_cancel_are_distinct(self):
        class BlockedClient:
            def __init__(self):
                self.entered = asyncio.Event()

            async def stream(self, request):
                self.entered.set()
                await asyncio.Event().wait()
                yield  # pragma: no cover

        self.runner.client = BlockedClient()
        self.runner.timeout = 0.2
        with self.assertRaises(RunFailedError) as error:
            await self.runner.run(self.session, "timeout")
        await self.ending(error.exception.run_id, "failed", "run_timeout")
        self.runner.timeout = 5
        self.runner.client = BlockedClient()
        identities = []
        task = asyncio.create_task(self.runner.run(self.session, "cancel", on_started=identities.append))
        await asyncio.wait_for(self.runner.client.entered.wait(), 5)
        self.runner.cancel(identities[0])
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.ending(identities[0], "cancelled", "user_cancelled")

    async def test_provider_error_and_abort_are_distinct(self):
        class FailedClient:
            def __init__(self, reason):
                self.reason = reason

            async def stream(self, request):
                yield ErrorEvent(self.reason, AssistantMessage([], "fake", "test", "test", 0, self.reason,
                                                               error_message="controlled provider stop"))

        for stop, status, reason in (("error", "failed", "model_error"), ("aborted", "cancelled", "model_aborted")):
            self.runner.client = FailedClient(stop)
            with self.assertRaises(RunFailedError) as error:
                await self.runner.run(self.session, stop)
            await self.ending(error.exception.run_id, status, reason)

    async def test_truncated_and_incomplete_stream_never_execute_tools(self):
        class BrokenClient(FakeModelClient):
            def __init__(self, truncate):
                super().__init__()
                self.truncate = truncate

            async def stream(self, request):
                async for event in super().stream(request):
                    if isinstance(event, DoneEvent):
                        if not self.truncate:
                            return
                        event.reason = event.message.stop_reason = "length"
                    yield event

        for truncate, reason in ((True, "output_limit"), (False, "model_protocol_error")):
            self.runner.client = BrokenClient(truncate)
            with self.assertRaises(RunFailedError) as error:
                await self.runner.run(self.session, reason)
            await self.ending(error.exception.run_id, "failed", reason)
            self.assertNotIn("tool_started", [event["type"] for event in await self.runner.runs.events(error.exception.run_id)])

    async def test_history_loaded_once_with_memory_enabled_and_no_state_leak(self):
        self.runner.model.context_window = 100000
        self.runner.memory_service.policy = MemoryPolicy(extract=False)
        self.runner.tools.registry.register(EmitTool("small result"))
        self.runner.client = LongClient(steps=8)
        original = self.runner.conversations.messages
        self.runner.conversations.messages = AsyncMock(wraps=original)
        self.runner.conversations.task_state = AsyncMock(wraps=self.runner.conversations.task_state)
        self.runner.context_manager.summaries.latest = AsyncMock(wraps=self.runner.context_manager.summaries.latest)
        self.runner.tools.registry.definitions = Mock(wraps=self.runner.tools.registry.definitions)
        states = self.capture_states()
        result = await self.runner.run(self.session, "eight tool steps")
        self.assertEqual(len(self.runner.client.requests), 9)
        self.assertEqual(self.runner.conversations.messages.await_count, 1)
        self.assertEqual(self.runner.conversations.task_state.await_count, 1)
        self.assertEqual(self.runner.context_manager.summaries.latest.await_count, 1)
        self.assertEqual(self.runner.tools.registry.definitions.call_count, 1)
        self.assertTrue(all(state is states[0] for state in states))
        self.assertEqual(states[0].history, await original(self.session, current_run=result.run_id, project_artifacts=True))
        await self.ending(result.run_id, "completed", "model_stop")

    async def reject_at_commit(self, table):
        async with self.database.connection() as connection:
            await connection.execute(sql.SQL("""
                CREATE FUNCTION reject_completed() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF NEW.status = 'completed' THEN RAISE EXCEPTION 'injected commit rejection'; END IF;
                  RETURN NEW;
                END $$;
                CREATE CONSTRAINT TRIGGER reject_commit AFTER UPDATE ON {}
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_completed();
            """).format(sql.Identifier(table)))

    async def test_response_commit_failure_does_not_advance_state(self):
        states = self.capture_states()
        await self.reject_at_commit("model_calls")
        with self.assertRaises(RunFailedError) as error:
            await self.runner.run(self.session, "response rollback")
        await self.ending(error.exception.run_id, "failed", "storage_error")
        self.assertEqual([item.message.role for item in states[0].history], ["user"])
        self.assertEqual(states[0].history, await self.runner.conversations.messages(self.session))
        self.assertNotIn("model_completed", [event["type"] for event in await self.runner.runs.events(error.exception.run_id)])

    async def test_terminal_commit_failure_cannot_publish_success(self):
        await self.reject_at_commit("runs")
        with self.assertRaises(RunFailedError) as error:
            await self.runner.run(self.session, "terminal rollback")
        await self.ending(error.exception.run_id, "failed", "storage_error")
        events = await self.runner.runs.events(error.exception.run_id)
        self.assertNotIn("run_completed", [event["type"] for event in events])

    async def test_tool_commit_failure_rolls_back_artifact_and_state(self):
        states = self.capture_states()
        self.runner.tools.registry.register(EmitTool("large output " * 1000))
        self.runner.client = LongClient(steps=1)
        await self.reject_at_commit("tool_calls")
        with self.assertRaises(RunFailedError) as error:
            await self.runner.run(self.session, "tool rollback")
        await self.ending(error.exception.run_id, "failed", "storage_error")
        self.assertEqual([item.message.role for item in states[0].history], ["user", "assistant"])
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT count(*) AS total FROM tool_artifacts")
            self.assertEqual((await cursor.fetchone())["total"], 0)
            cursor = await connection.execute("SELECT status FROM tool_calls WHERE run_id = %s", (error.exception.run_id,))
            self.assertEqual((await cursor.fetchone())["status"], "unknown")
        self.assertEqual(len(self.runner.client.requests), 1)

    async def test_large_artifact_projection_matches_restart(self):
        self.runner.model.context_window = 100000
        self.runner.tools.registry.register(EmitTool("big result\n" * 2000))
        self.runner.client = LongClient(steps=1)
        states = self.capture_states()
        result = await self.runner.run(self.session, "large result")
        projected = await self.runner.conversations.messages(self.session, current_run=result.run_id, project_artifacts=True)
        self.assertEqual(states[0].history, projected)
        self.assertEqual(states[0].history[2].message.content, [])
        self.assertIsNotNone(states[0].history[2].message.artifact)
        restored = create_demo_runner(self.database, memory_policy=MemoryPolicy(enabled=False))
        resumed = await restored.run(self.session, "continue")
        self.assertEqual(resumed.end_reason, "model_stop")

    async def test_compaction_reuses_committed_checkpoint_without_history_reload(self):
        self.runner.model.context_window = LONG_RUN_WINDOW
        self.runner.tools.registry.register(EmitTool("tool output " + "x" * LONG_TOOL_RESULT_CHARS))
        self.runner.client = LongClient(steps=6)
        original = self.runner.conversations.messages
        self.runner.conversations.messages = AsyncMock(wraps=original)
        self.runner.context_manager.summaries.latest = AsyncMock(wraps=self.runner.context_manager.summaries.latest)
        states = self.capture_states()
        result = await self.runner.run(self.session, "preserve original task input")
        summaries = await self.runner.context_manager.summaries.list(self.session)
        self.assertGreaterEqual(len(summaries), 2)
        self.assertEqual(self.runner.conversations.messages.await_count, 1)
        self.assertEqual(self.runner.context_manager.summaries.latest.await_count, 1)
        self.assertEqual(states[0].summary.id, summaries[0].id)
        self.assertEqual(states[0].history, await original(self.session, current_run=result.run_id, project_artifacts=True))

    async def test_registry_revision_refreshes_declarations_next_iteration(self):
        echo = self.runner.tools.registry.get("echo")
        execute = echo.execute

        async def register_tool(*args, **kwargs):
            extra = EchoTool()
            extra.name = "extra_echo"
            self.runner.tools.registry.register(extra)
            return await execute(*args, **kwargs)

        echo.execute = register_tool
        self.runner.client = BatchClient(["echo"])
        await self.runner.run(self.session, "dynamic registry")
        first, second = self.runner.client.requests
        self.assertNotIn("extra_echo", [tool.name for tool in first.messages[0].tools_added])
        self.assertIn("extra_echo", [tool.name for tool in second.messages[0].tools_added])

    async def test_concurrent_sessions_have_independent_invocation_state(self):
        second_workspace = Path(self.temporary.name) / "other"
        second_workspace.mkdir()
        other = await self.runner.conversations.create_session(second_workspace)
        states = self.capture_states()
        results = await asyncio.gather(self.runner.run(self.session, "first session input"),
                                       self.runner.run(other, "second session input"))
        by_run = {state.run_id: state for state in states}
        self.assertEqual(len(by_run), 2)
        for result, expected in zip(results, ("first session input", "second session input")):
            state = by_run[result.run_id]
            self.assertEqual(state.current_input.message.content, expected)
            self.assertEqual(state.history, await self.runner.conversations.messages(result.session_id))
            await self.ending(result.run_id, "completed", "model_stop")

    async def test_reason_status_mismatch_cannot_commit(self):
        identity = await self.runner.runs.begin(self.session, UserMessage("test", 0))
        with self.assertRaises(ValueError):
            await self.runner.runs.finish(identity, "completed", end_reason="turn_limit")
        self.assertEqual((await self.runner.runs.get(identity))["status"], "running")
        await self.runner.runs.finish(identity, "cancelled")



class ObservedUsageStateTests(unittest.TestCase):
    """The provider's count is per-run state, dropped when the prompt changes shape."""

    def state(self):
        from chagent.agent.types import RunState
        from chagent.domain.sessions import Session, TaskState
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return RunState(run_id=uuid4(),
                        session=Session(uuid4(), uuid4(), Path("/tmp"), "t", now, now),
                        history=[StoredMessage(uuid4(), 7, UserMessage("hi", 0), None)],
                        task_state=TaskState())

    def test_usage_anchors_the_sequence_that_produced_it(self):
        from chagent.domain.usage import Usage
        state = self.state()
        state.record_usage(Usage(input=800, output=200, cache_read=0, cache_write=0, total_tokens=1_000),
                           after_sequence=7)
        self.assertEqual((state.observed.prompt_tokens, state.observed.after_sequence), (1_000, 7))
        self.assertEqual(state.observed.input_tokens, 800, "the prompt half calibrates the heuristic")

    def test_the_recalled_memory_block_is_not_counted_twice(self):
        """It is re-added by enrich on every turn, so it cannot stay in the anchor."""
        from chagent.domain.usage import Usage
        state = self.state()
        state.record_usage(Usage(input=800, output=200, cache_read=0, cache_write=0, total_tokens=1_000),
                           after_sequence=7, memory_tokens=250)
        self.assertEqual(state.observed.prompt_tokens, 750)

    def test_a_response_without_usage_clears_the_anchor(self):
        from chagent.domain.usage import Usage
        state = self.state()
        state.record_usage(Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2), after_sequence=7)
        state.record_usage(None, after_sequence=8)
        self.assertIsNone(state.observed)
        state.record_usage(Usage(input=0, output=0, cache_read=0, cache_write=0, total_tokens=0), after_sequence=8)
        self.assertIsNone(state.observed)

    def test_committing_a_message_does_not_move_the_anchor(self):
        from chagent.domain.usage import Usage
        state = self.state()
        state.record_usage(Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2), after_sequence=7)
        state.append_committed(StoredMessage(uuid4(), 8, UserMessage("more", 1), state.run_id))
        self.assertEqual(state.observed.after_sequence, 7)
