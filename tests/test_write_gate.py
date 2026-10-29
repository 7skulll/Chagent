"""One ownership gate per write: statement counts and rejected writes.

Every business write used to probe the run with an ownership query and then
lock the same row again for the event insert. These tests pin the shape that
replaced it — one gated statement, or a write that carries the condition
itself — so the extra statement cannot quietly come back, and so a lost owner
still writes nothing.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.config import Settings
from chagent.context.builder import BasicContextBuilder
from chagent.domain.messages import UserMessage
from chagent.domain.summaries import SessionSummary
from chagent.models.base import Model, ModelRequest
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import ConversationRepository, RunOwnershipLost, RunRepository
from chagent.storage.summaries import SummaryRepository


class _Counted:
    def __init__(self, connection, counter):
        self._connection, self._counter = connection, counter

    async def execute(self, *args, **kwargs):
        self._counter["sql"] += 1
        return await self._connection.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class CountingDatabase(Database):
    def __init__(self, settings):
        self.counter = {"sql": 0}
        super().__init__(settings)

    @asynccontextmanager
    async def connection(self):
        async with super().connection() as connection:
            yield _Counted(connection, self.counter)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class WriteGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.schema = "chagent_gate_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = CountingDatabase(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.conversations = ConversationRepository(self.database)
        self.runs, self.summaries = RunRepository(self.database), SummaryRepository(self.database)
        self.session = await self.conversations.create_session(Path(self.directory.name))
        self.run_id = await self.runs.begin(self.session, UserMessage("gate", 0))
        self.addAsyncCleanup(self.runs.close_events, self.run_id)
        self.model = Model(id="gate", provider="local", api="fake", context_window=8192, max_tokens=256)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def statements(self, action) -> int:
        self.database.counter["sql"] = 0
        await action()
        return self.database.counter["sql"]

    async def built(self):
        history = await self.conversations.messages(self.session, current_run=self.run_id)
        return await BasicContextBuilder().build(history, self.model, [])

    async def steal_ownership(self) -> None:
        """Another process took the run over; the local token no longer matches."""
        await self.admin.execute(sql.SQL("UPDATE {}.runs SET owner_token = gen_random_uuid() WHERE id = %s")
                                 .format(sql.Identifier(self.schema)), (self.run_id,))

    async def restore_ownership(self) -> None:
        await self.admin.execute(
            sql.SQL("UPDATE {}.runs SET owner_token = %s WHERE id = %s").format(sql.Identifier(self.schema)),
            (self.database.run_owners[self.run_id], self.run_id))

    async def test_single_event_costs_one_gate_and_one_insert(self):
        async def write():
            await self.runs.event(self.run_id, "tool_progress", {"text": "x"})

        self.assertEqual(await self.statements(write), 2)
        self.assertIn("tool_progress", [event["type"] for event in await self.runs.events(self.run_id)])

    async def test_business_write_pays_for_the_gate_only_once(self):
        built = await self.built()

        async def write():
            await self.runs.start_model_call(self.run_id, 0, built)

        self.assertEqual(await self.statements(write), 3)
        self.assertEqual([event["type"] for event in await self.runs.events(self.run_id)][-1], "model_started")

    async def test_terminal_write_carries_the_condition_instead_of_probing(self):
        self.assertEqual(await self.statements(lambda: self.runs.finish(self.run_id, "completed")), 6)
        row = await self.runs.get(self.run_id)
        self.assertEqual((row["status"], row["end_reason"]), ("completed", "model_stop"))

    async def test_stale_owner_writes_nothing_and_keeps_the_run_open(self):
        await self.steal_ownership()
        try:
            with self.assertRaises(RunOwnershipLost):
                await self.runs.event(self.run_id, "tool_progress", {"text": "late"})
            built = await self.built()
            with self.assertRaises(RunOwnershipLost):
                await self.runs.start_model_call(self.run_id, 0, built)
            with self.assertRaises(RunOwnershipLost):
                await self.runs.finish(self.run_id, "cancelled")
        finally:
            await self.restore_ownership()
        self.assertEqual((await self.runs.get(self.run_id))["status"], "running")
        self.assertEqual([event["type"] for event in await self.runs.events(self.run_id)], ["run_started"])

    async def test_expired_lease_is_reported_as_lost_ownership(self):
        async with self.database.connection() as connection:
            await connection.execute(
                "UPDATE runs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = %s",
                (self.run_id,))
        with self.assertRaises(RunOwnershipLost):
            await self.runs.finish(self.run_id, "completed")
        self.assertEqual((await self.runs.get(self.run_id))["status"], "running")

    async def test_second_terminal_write_is_a_conflict_not_an_ownership_loss(self):
        await self.runs.finish(self.run_id, "cancelled")
        with self.assertRaises(RuntimeError) as error:
            await self.runs.finish(self.run_id, "failed")
        self.assertNotIsInstance(error.exception, RunOwnershipLost)

    async def test_compaction_calls_write_conditionally(self):
        request = ModelRequest(model=self.model, purpose="summary", max_tokens=64,
                               messages=[UserMessage("history", 0)])

        async def start():
            await self.summaries.start_call(uuid4(), self.run_id, 0, request)

        self.assertEqual(await self.statements(start), 1)
        identity = await self.summaries.start_call(uuid4(), self.run_id, 1, request)

        async def finish():
            await self.summaries.finish_call(identity, "completed", None, 12)

        self.assertEqual(await self.statements(finish), 2)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT status, duration_ms FROM compaction_calls WHERE id = %s", (identity,))
            self.assertEqual(dict(await cursor.fetchone()), {"status": "completed", "duration_ms": 12})

        await self.steal_ownership()
        try:
            with self.assertRaises(RunOwnershipLost):
                await self.summaries.start_call(uuid4(), self.run_id, 2, request)
            # A call opened before the takeover cannot be closed after it.
            await self.restore_ownership()
            pending = await self.summaries.start_call(uuid4(), self.run_id, 3, request)
            await self.steal_ownership()
            with self.assertRaises(RunOwnershipLost):
                await self.summaries.finish_call(pending, "failed", None, 1)
        finally:
            await self.restore_ownership()

    async def test_checkpoint_is_rejected_by_the_gate_before_any_validation(self):
        summary = SessionSummary(
            id=uuid4(), session_id=self.session, revision=1, previous_summary_id=None,
            covered_through_sequence=1, first_kept_message_id=uuid4(), content="",
            model=self.model.id, provider=self.model.provider, prompt_version="v1",
            compaction_id=uuid4(), source_message_ids=[str(uuid4())],
            source_run_id=None, protected_input_id=None,
        )
        await self.steal_ownership()
        try:
            with self.assertRaises(RunOwnershipLost):
                await self.summaries.save(summary, self.run_id)
        finally:
            await self.restore_ownership()
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS total FROM session_summaries WHERE session_id = %s", (self.session,))
            self.assertEqual((await cursor.fetchone())["total"], 0)
