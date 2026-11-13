"""Batched event persistence: ordering, bounds, failure and shutdown."""

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.config import Settings
from chagent.domain.messages import UserMessage
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import ConversationRepository, RunOwnershipLost, RunRepository


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class EventBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.schema = "chagent_event_batch_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.conversations = ConversationRepository(self.database)
        self.runs = RunRepository(self.database)
        self.session = await self.conversations.create_session(Path(self.directory.name))
        self.run_id = await self.runs.begin(self.session, UserMessage("batch", 0))
        self.addAsyncCleanup(self.runs.close_events, self.run_id)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def types(self, run_id=None):
        return [event["type"] for event in await self.runs.events(run_id or self.run_id)]

    async def flush_tasks(self):
        return [task for task in asyncio.all_tasks()
                if not task.done() and task.get_name().startswith("run-event-flush-")]

    async def test_tool_lifecycle_events_bind_to_stable_record_identity(self):
        from psycopg.types.json import Jsonb
        from chagent.domain.messages import TextContent, ToolCall, ToolResultMessage
        from chagent.tools.base import AgentToolResult

        model_id = uuid4()
        async with self.database.transaction() as connection:
            await connection.execute(
                'INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot) '
                'VALUES (%s, %s, 0, %s, %s)',
                (model_id, self.run_id, Jsonb({}), Jsonb({})))
        calls = [ToolCall('done', 'echo', {}), ToolCall('interrupted', 'echo', {}),
                 ToolCall('cancelled', 'echo', {})]
        identities = await self.runs.queue_tool_calls(self.run_id, model_id, calls)
        await self.runs.start_tool_call(self.run_id, model_id, calls[0], record_id=identities[0])
        content = [TextContent('ok')]
        await self.runs.save_tool_result(
            self.run_id, self.session, identities[0], AgentToolResult(content),
            ToolResultMessage('done', 'echo', content, False, 0))
        await self.runs.start_tool_call(self.run_id, model_id, calls[1], record_id=identities[1])
        await self.runs.finish(self.run_id, 'cancelled')
        lifecycle = [e for e in await self.runs.events(self.run_id)
                     if e['type'] in {'tool_started', 'tool_completed', 'tool_interrupted', 'tool_cancelled'}]
        expected = {'done': identities[0], 'interrupted': identities[1], 'cancelled': identities[2]}
        self.assertEqual(len(lifecycle), 5)
        for record in lifecycle:
            payload = record['payload']
            self.assertEqual(payload['tool_record_id'], str(expected[payload['tool_call_id']]))
            if record['type'] == 'tool_started':
                self.assertEqual(payload['model_call_id'], str(model_id))

    async def test_emitted_events_stay_buffered_until_a_flush(self):
        await self.runs.emit(self.run_id, "text_delta", {"delta": "一", "count": 1})
        await self.runs.emit(self.run_id, "text_delta", {"delta": "二", "count": 1})
        self.assertEqual(await self.types(), ["run_started"])
        await self.runs.flush_events(self.run_id)
        persisted = await self.runs.events(self.run_id)
        self.assertEqual([event["type"] for event in persisted][-2:], ["text_delta", "text_delta"])
        self.assertEqual([event["sequence"] for event in persisted], list(range(1, len(persisted) + 1)))

    async def test_background_task_commits_without_an_explicit_flush(self):
        await self.runs.emit(self.run_id, "text_delta", {"delta": "后台", "count": 1})
        for _ in range(50):
            if "text_delta" in await self.types():
                break
            await asyncio.sleep(.02)
        self.assertIn("text_delta", await self.types())

    async def test_direct_event_after_buffered_ones_keeps_sequence_order(self):
        await self.runs.emit(self.run_id, "text_delta", {"delta": "先", "count": 1})
        await self.runs.emit(self.run_id, "text_delta", {"delta": "后", "count": 1})
        await self.runs.event(self.run_id, "tool_queued", {"tool_call_id": "c1", "name": "echo"})
        persisted = await self.runs.events(self.run_id)
        self.assertEqual([event["type"] for event in persisted],
                         ["run_started", "text_delta", "text_delta", "tool_queued"])
        self.assertEqual([event["sequence"] for event in persisted], [1, 2, 3, 4])
        self.assertEqual("".join(event["payload"]["delta"] for event in persisted
                                 if event["type"] == "text_delta"), "先后")

    async def test_terminal_event_is_written_last_even_with_a_pending_tail(self):
        await self.runs.emit(self.run_id, "text_delta", {"delta": "尾", "count": 1})
        await self.runs.finish(self.run_id, "cancelled", end_reason="user_cancelled")
        persisted = await self.runs.events(self.run_id)
        self.assertEqual(persisted[-1]["type"], "run_cancelled")
        self.assertIn("text_delta", [event["type"] for event in persisted])
        self.assertEqual([event["sequence"] for event in persisted], list(range(1, len(persisted) + 1)))

    async def test_rejected_batch_is_dropped_but_the_terminal_state_is_still_written(self):
        async with self.database.connection() as connection:
            await connection.execute("""
                CREATE FUNCTION reject_text() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.type = 'text_delta' THEN RAISE EXCEPTION 'controlled rejection'; END IF;
                RETURN NEW; END $$;
                CREATE CONSTRAINT TRIGGER reject_text AFTER INSERT ON run_events
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_text();
            """)
        await self.runs.emit(self.run_id, "text_delta", {"delta": "丢", "count": 1})
        with self.assertRaises(Exception):
            await self.runs.flush_events(self.run_id)
        await self.runs.finish(self.run_id, "failed", "controlled", end_reason="storage_error")
        self.assertEqual(await self.types(), ["run_started", "run_failed"])
        self.assertEqual((await self.runs.get(self.run_id))["status"], "failed")

    async def test_lost_ownership_is_not_swallowed_by_the_terminal_flush(self):
        await self.runs.emit(self.run_id, "text_delta", {"delta": "旧", "count": 1})
        self.database.run_owners[self.run_id] = uuid4()
        with self.assertRaises(RunOwnershipLost):
            await self.runs.finish(self.run_id, "completed")
        self.assertEqual((await self.runs.get(self.run_id))["status"], "running")
        self.database.run_owners.pop(self.run_id, None)

    async def test_close_stops_the_flush_task_and_persists_the_tail(self):
        await self.runs.emit(self.run_id, "text_delta", {"delta": "尾", "count": 1})
        self.assertEqual(len(await self.flush_tasks()), 1)
        await self.runs.close_events(self.run_id)
        self.assertEqual(await self.flush_tasks(), [])
        self.assertIn("text_delta", await self.types())
        self.assertNotIn(self.run_id, self.runs._streams)

    async def test_buffer_is_bounded_and_waits_for_a_commit(self):
        stream = self.runs._stream(self.run_id)
        stream.capacity = 4
        try:
            for index in range(4):
                await self.runs.emit(self.run_id, "text_delta", {"delta": str(index), "count": 1})
            # The fifth append crosses the bound and therefore has to commit.
            await asyncio.wait_for(self.runs.emit(self.run_id, "text_delta", {"delta": "5", "count": 1}), 2)
            self.assertLessEqual(stream.pending, 4)
        finally:
            await self.runs.close_events(self.run_id)
