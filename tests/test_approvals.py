"""Approval persistence: one question per attempt, one decision, one use."""

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.domain.messages import ToolCall, UserMessage
from chagent.storage.approvals import ApprovalRepository
from chagent.storage.database import Database
from chagent.storage.migrate import migrate


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class ApprovalRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_approval_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.runner = create_demo_runner(self.database)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.session = await self.runner.conversations.create_session(Path(self.temporary.name))
        self.approvals = ApprovalRepository(self.database, self.runner.runs)
        self.run_id = await self.runner.runs.begin(self.session, UserMessage("需要批准", 0))
        model_call = uuid4()
        async with self.database.transaction() as connection:
            # A tool call belongs to a model call; this test only needs the row.
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot) "
                "VALUES (%s, %s, 0, %s, %s)", (model_call, self.run_id, Jsonb({}), Jsonb({})))
        self.tool_id = (await self.runner.runs.queue_tool_calls(
            self.run_id, model_call, [ToolCall("c1", "exec_command", {"cmd": "ls"})]))[0]

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def create(self, *, tool_id=None, fingerprint="fp", ttl=60.0):
        return await self.approvals.create(
            run_id=self.run_id, tool_record_id=tool_id or self.tool_id, fingerprint=fingerprint,
            tool_name="exec_command", cwd="/workspace", subject="ls", ttl=ttl)

    async def test_create_is_idempotent_per_tool_attempt(self):
        first = await self.create()
        second = await self.create()
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(await self.approvals.pending(self.run_id)), 1)

    async def test_decide_is_a_conditional_update(self):
        request = await self.create()
        self.assertEqual(await self.approvals.decide(request.id, approve=True), "approved")
        self.assertEqual(await self.approvals.decide(request.id, approve=False), "conflict")
        stored = await self.approvals.get(request.id)
        self.assertEqual(stored.status, "approved")

    async def test_expiry_beats_a_late_approval(self):
        request = await self.create(ttl=0.05)
        await asyncio.sleep(0.15)
        self.assertEqual(await self.approvals.expire_due(run_id=self.run_id), 1)
        self.assertEqual(await self.approvals.decide(request.id, approve=True), "conflict")
        self.assertEqual((await self.approvals.get(request.id)).status, "expired")

    async def test_consuming_a_grant_is_single_use_and_bound_to_the_call(self):
        request = await self.create(fingerprint="fp-1")
        await self.approvals.decide(request.id, approve=True)
        self.assertFalse(await self.approvals.consume(request.id, run_id=self.run_id, fingerprint="other"))
        self.assertTrue(await self.approvals.consume(request.id, run_id=self.run_id, fingerprint="fp-1"))
        self.assertFalse(await self.approvals.consume(request.id, run_id=self.run_id, fingerprint="fp-1"))

    async def test_a_finished_run_cannot_be_approved(self):
        request = await self.create()
        await self.runner.runs.finish(self.run_id, "completed")
        self.assertEqual(await self.approvals.decide(request.id, approve=True), "conflict")
        self.assertTrue(await self.approvals.cancel_pending(self.run_id))

    async def test_cancelling_pending_questions_emits_an_event(self):
        await self.create()
        self.assertEqual(await self.approvals.cancel_pending(self.run_id, "run_cancelled"), 1)
        kinds = [event["type"] for event in await self.runner.runs.events(self.run_id)]
        self.assertIn("approval_requested", kinds)
        self.assertIn("approval_cancelled", kinds)

    async def test_decisions_and_consumption_are_audited(self):
        request = await self.create()
        await self.approvals.decide(request.id, approve=True, note="test")
        await self.approvals.consume(request.id, run_id=self.run_id, fingerprint="fp")
        kinds = [event["type"] for event in await self.runner.runs.events(self.run_id)]
        for expected in ("approval_requested", "approval_decided", "approval_consumed"):
            self.assertIn(expected, kinds)

    async def test_waiting_projection_uses_the_pending_row(self):
        self.assertEqual(await self.approvals.waiting_for(self.run_id), {"waiting_for": None})
        request = await self.create()
        view = await self.approvals.waiting_for(self.run_id)
        self.assertEqual(view["waiting_for"], "approval")
        self.assertEqual(view["approval_id"], str(request.id))
        self.assertEqual(view["approval"]["subject"], "ls")


if __name__ == "__main__":
    unittest.main()
