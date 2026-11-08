"""Post-turn compaction: the answer is already committed when the summary is paid for."""

import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.domain.summaries import CompactionPolicy
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.summaries import SummaryRepository
from test_compaction import RecordingClient


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class PostTurnCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_post_turn_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}",
        )))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.runner = create_demo_runner(
            self.database, compaction_policy=CompactionPolicy(post_turn=True, post_turn_trigger=0.01),
        )
        self.summaries = SummaryRepository(self.database)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.session = await self.runner.conversations.create_session(Path(self.temporary.name))

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def calls(self, run_id):
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM compaction_calls WHERE run_id = %s ORDER BY iteration", (run_id,))
            return await cursor.fetchall()

    async def test_post_turn_compacts_the_run_it_just_finished(self):
        """The pass covers the finished run's closed exchanges, never its answer."""
        result = await self.runner.run(self.session, "x" * 400)
        await self.runner.context_manager.drain_post_turn()
        summary = await self.summaries.latest(self.session)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.source_run_id, result.run_id)
        self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")
        history = await self.runner.conversations.messages(self.session)
        # The answer that ends the run stays verbatim: it is what the user just read.
        self.assertGreater(history[-1].sequence, summary.covered_through_sequence)
        # ...and the request that produced it is quoted, because the checkpoint
        # covered the message that carried it.
        self.assertIn("x" * 100, summary.content)
        events = await self.runner.runs.events(result.run_id)
        completed = [event for event in events if event["type"] == "compaction_completed"]
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0]["payload"]["post_turn"])
        self.assertEqual(completed[0]["payload"]["implementation"], "summary")
        # The cost is attributed to the run that produced the sources.
        calls = await self.calls(result.run_id)
        self.assertTrue(calls)
        self.assertTrue(all(call["status"] == "completed" for call in calls))

    async def test_post_turn_never_delays_the_terminal_state(self):
        """The run returns while the summary is still in flight behind it."""
        self.runner.client = client = RecordingClient(mode="block")
        result = await asyncio.wait_for(self.runner.run(self.session, "x" * 400), timeout=10)
        self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")
        await asyncio.wait_for(client.entered.wait(), timeout=5)
        self.assertIsNone(await self.summaries.latest(self.session))
        for task in list(self.runner.context_manager._post_turn_tasks.values()):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failed_post_turn_is_audited_and_never_fails_the_run(self):
        self.runner.client = RecordingClient(mode="exception")
        result = await self.runner.run(self.session, "x" * 400)
        await self.runner.context_manager.drain_post_turn()
        self.assertIsNone(await self.summaries.latest(self.session))
        self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")
        calls = await self.calls(result.run_id)
        self.assertTrue(calls)
        self.assertTrue(all(call["status"] == "failed" for call in calls))
        self.assertNotIn("private-test-credential", str([call["error"] for call in calls]))

    async def test_below_the_trigger_no_summary_is_paid_for(self):
        manager = self.runner.context_manager
        manager.policy = replace(manager.policy, post_turn_trigger=0.79)
        manager.compactor.policy = manager.policy
        result = await self.runner.run(self.session, "small ask")
        await self.runner.context_manager.drain_post_turn()
        self.assertIsNone(await self.summaries.latest(self.session))
        self.assertEqual(await self.calls(result.run_id), [])

    async def test_disabled_policy_schedules_nothing(self):
        manager = self.runner.context_manager
        manager.policy = replace(manager.policy, post_turn=False)
        manager.compactor.policy = manager.policy
        result = await self.runner.run(self.session, "x" * 400)
        await manager.drain_post_turn()
        self.assertIsNone(await self.summaries.latest(self.session))
        self.assertEqual(await self.calls(result.run_id), [])
