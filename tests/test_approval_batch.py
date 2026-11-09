"""One page decision for several writes in the same model turn (batch 1A).

WHAT THIS PINS
    The plan asked for a batch that (a) keeps one approval row per call with its
    own fingerprint and single-use consumption, (b) applies one page decision to
    every row of the batch atomically, and (c) shows one card. The tests below
    cover the failure modes that make a batch dangerous: a decision that leaks to
    a sibling, a second decision on an answered batch, a sibling that expired, a
    cancelled run, two pages deciding at once, and a grant reused for another
    call.

WHAT IT IS NOT
    A browser test. The card itself is checked by the page tests; here the
    decision is made through the same repository call the API uses.
"""

import asyncio
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.runner import RunFailedError
from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.domain.memories import MemoryPolicy
from chagent.domain.messages import AssistantMessage, ToolCall, UserMessage
from chagent.models.base import Model
from chagent.models.events import DoneEvent, StartEvent
from chagent.models.fake import FakeModelClient
from chagent.agent.turn_note import last_request_message
from chagent.storage.approvals import ApprovalRepository
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.tools.policy import ApprovalPolicy


class BatchWriteClient(FakeModelClient):
    """Asks for several writes in one assistant message, then answers."""

    def __init__(self, writes: list[tuple[str, str, str | None]]):
        super().__init__()
        #: (path, content, expected_sha256) — an overwrite needs the version it
        #: read, otherwise the versioned file tool refuses before any approval.
        self.writes = writes

    async def stream(self, request):
        if isinstance(last_request_message(request.messages), UserMessage):
            message = AssistantMessage([], request.model.api, request.model.provider, request.model.id, 0, "pending")
            yield StartEvent(message)
            final = deepcopy(message)
            final.content = [
                ToolCall(f"batch-{index}", "write_file",
                         {"path": path, "content": content,
                          **({"overwrite": True, "expected_sha256": digest} if digest else {})})
                for index, (path, content, digest) in enumerate(self.writes)]
            final.stop_reason = "toolUse"
            yield DoneEvent("toolUse", final)
            return
        async for event in super().stream(request):
            yield event


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class ApprovalBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_batch_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def runner(self, writes, *, timeout=10.0):
        """``writes`` is [(path, new content)]; each target is seeded as an
        existing file so the write is an overwrite — the case the gate asks about."""
        policy = ApprovalPolicy.from_settings(Settings(
            execution_profile="containerized", container_declared=True,
            allow_host_commands=True, sandbox_network=True, approval="untrusted"))
        arguments = []
        for path, content in writes:
            target = self.workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("old", encoding="utf-8")
            arguments.append((path, content, hashlib.sha256(b"old").hexdigest()))
        runner = create_runner(
            self.database,
            Model(id="batch-fake", provider="local-test", api="fake", context_window=16_384, max_tokens=512),
            BatchWriteClient(arguments),
            allow_host_commands=True, sandbox=None, timeout=60, max_turns=8,
            memory_policy=MemoryPolicy(enabled=False),
            approval_policy=policy, approval_timeout=timeout, approval_remote=True,
            code_mode=True, single_face=True, allow_workspace_writes=True, versioned_files=True,
        )
        return runner

    def new_file_runner(self, writes, *, timeout=10.0):
        policy = ApprovalPolicy.from_settings(Settings(
            execution_profile="containerized", container_declared=True,
            allow_host_commands=True, sandbox_network=True, approval="untrusted"))
        return create_runner(
            self.database,
            Model(id="batch-fake", provider="local-test", api="fake", context_window=16_384, max_tokens=512),
            BatchWriteClient([(path, content, None) for path, content in writes]),
            allow_host_commands=True, sandbox=None, timeout=60, max_turns=8,
            memory_policy=MemoryPolicy(enabled=False),
            approval_policy=policy, approval_timeout=timeout, approval_remote=True,
            code_mode=True, single_face=True, allow_workspace_writes=True, versioned_files=True,
        )

    def approvals(self, runner) -> ApprovalRepository:
        return ApprovalRepository(self.database, runner.runs)

    async def wait_pending(self, runner, run_id, count=1, timeout=10.0):
        repository = self.approvals(runner)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            pending = await repository.pending(run_id)
            if len(pending) >= count:
                return pending
            await asyncio.sleep(0.02)
        raise AssertionError(f"expected {count} pending approvals")

    async def run_id_of(self, session):
        for _ in range(200):
            row = await self.runner_for_query().one(
                "SELECT id FROM runs WHERE session_id = %s ORDER BY created_at DESC LIMIT 1", (session,))
            if row:
                return row["id"]
            await asyncio.sleep(0.02)
        raise AssertionError("run never started")

    def runner_for_query(self):
        class Query:
            def __init__(self, database):
                self.database = database

            async def one(self, statement, parameters):
                async with self.database.connection() as connection:
                    cursor = await connection.execute(statement, parameters)
                    return await cursor.fetchone()

        return Query(self.database)

    async def test_three_writes_in_one_turn_are_one_batch(self):
        runner = self.runner([("a.txt", "A"), ("b.txt", "B"), ("c.txt", "C")])
        session = await runner.conversations.create_session(self.workspace)
        task = asyncio.create_task(runner.run(session, "写三个文件"))
        pending = await self.wait_pending(runner, await self.run_id_of(session), count=3)

        batch_ids = {row.batch_id for row in pending}
        self.assertEqual(len(batch_ids), 1, "the three writes must share one batch id")
        self.assertIsNotNone(batch_ids.pop())
        self.assertEqual([row.batch_index for row in pending], [0, 1, 2])
        self.assertEqual([row.subject.split(" ")[0] for row in pending], ["overwrite"] * 3)
        self.assertEqual([row.subject.split(" ")[1] for row in pending], ["a.txt", "b.txt", "c.txt"])

        # One page decision answers all three rows.
        self.assertEqual(await self.approvals(runner).decide(pending[0].id, approve=True), "approved")
        result = await asyncio.wait_for(task, timeout=20)
        self.assertEqual(result.end_reason, "model_stop")
        for name, content in (("a.txt", "A"), ("b.txt", "B"), ("c.txt", "C")):
            self.assertEqual((self.workspace / name).read_text(encoding="utf-8"), content)

        # Each call consumed its own grant, and each result carries the approval.
        rows = await self.approvals(runner).batch(pending[0].batch_id)
        self.assertTrue(all(row.status == "approved" for row in rows))
        self.assertTrue(all(row.consumed_at is not None for row in rows))
        events = await runner.runs.events(result.run_id)
        # One event per batch: the page draws one card, per-call rows are audit.
        requested = [event for event in events if event["type"] == "approval_requested"]
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0]["payload"]["batch_size"], 3)
        self.assertEqual(requested[0]["payload"]["batch_id"], str(pending[0].batch_id))
        self.assertEqual(sum(event["type"] == "approval_consumed" for event in events), 3)

    async def test_rejecting_the_batch_writes_nothing(self):
        runner = self.runner([("a.txt", "A"), ("b.txt", "B"), ("c.txt", "C")])
        session = await runner.conversations.create_session(self.workspace)
        task = asyncio.create_task(runner.run(session, "写三个文件"))
        pending = await self.wait_pending(runner, await self.run_id_of(session), count=3)

        self.assertEqual(await self.approvals(runner).decide(pending[1].id, approve=False), "denied")
        result = await asyncio.wait_for(task, timeout=20)
        self.assertEqual(result.end_reason, "model_stop")
        for name in ("a.txt", "b.txt", "c.txt"):
            self.assertEqual((self.workspace / name).read_text(encoding="utf-8"), "old",
                             f"{name} was modified despite the refusal")

        rows = await self.approvals(runner).batch(pending[0].batch_id)
        self.assertTrue(all(row.status == "denied" for row in rows))
        self.assertTrue(all(row.consumed_at is None for row in rows))
        events = await runner.runs.events(result.run_id)
        self.assertNotIn("tool_started", [event["type"] for event in events])
        details = [event for event in events if event["type"] == "tool_call_refused"]
        self.assertEqual(len(details), 0, "a batch refusal is a decision, not a gate failure")

        # The model is told it was a decision about the edit, per call.
        messages = await runner.conversations.messages(session)
        refusals = [item.message for item in messages if item.message.role == "toolResult"]
        self.assertEqual(len(refusals), 3)
        for message in refusals:
            text = message.content[0].text
            self.assertIn("declined this change", text)
            self.assertIn("not a tool failure", text)

    async def test_a_second_decision_on_the_same_batch_conflicts(self):
        runner = self.runner([("a.txt", "A"), ("b.txt", "B")])
        session = await runner.conversations.create_session(self.workspace)
        task = asyncio.create_task(runner.run(session, "写两个文件"))
        pending = await self.wait_pending(runner, await self.run_id_of(session), count=2)
        repository = self.approvals(runner)

        self.assertEqual(await repository.decide(pending[0].id, approve=True), "approved")
        # The same batch, a different row: already answered.
        self.assertEqual(await repository.decide(pending[1].id, approve=False), "conflict")
        # The original row again: also a conflict, not a second decision.
        self.assertEqual(await repository.decide(pending[0].id, approve=False), "conflict")
        result = await asyncio.wait_for(task, timeout=20)
        self.assertEqual(result.end_reason, "model_stop")
        self.assertTrue((self.workspace / "a.txt").exists())
        self.assertTrue((self.workspace / "b.txt").exists())

    async def test_two_pages_deciding_at_once_resolve_to_one_answer(self):
        runner = self.runner([("a.txt", "A"), ("b.txt", "B")])
        session = await runner.conversations.create_session(self.workspace)
        task = asyncio.create_task(runner.run(session, "写两个文件"))
        pending = await self.wait_pending(runner, await self.run_id_of(session), count=2)
        repository = self.approvals(runner)

        answers = await asyncio.gather(
            repository.decide(pending[0].id, approve=True),
            repository.decide(pending[1].id, approve=False, note="second tab"),
        )
        self.assertEqual(sorted(answers), ["approved", "conflict"])
        await asyncio.wait_for(task, timeout=20)
        rows = await repository.batch(pending[0].batch_id)
        self.assertEqual({row.status for row in rows}, {"approved"})
        self.assertTrue(all(row.consumed_at is not None for row in rows))

    async def test_a_sibling_that_expired_is_not_revived_by_the_decision(self):
        runner = self.runner([("a.txt", "A"), ("b.txt", "B"), ("c.txt", "C")])
        session = await runner.conversations.create_session(self.workspace)
        task = asyncio.create_task(runner.run(session, "写三个文件"))
        pending = await self.wait_pending(runner, await self.run_id_of(session), count=3)
        repository = self.approvals(runner)

        # One row's deadline passes before the page answers: the decision covers
        # the two live rows and leaves the expired one expired.
        async with self.database.transaction() as connection:
            await connection.execute(
                "UPDATE approval_requests SET expires_at = clock_timestamp() - interval '1 second' "
                "WHERE id = %s", (pending[1].id,))
        await repository.expire_due()
        self.assertEqual(await repository.decide(pending[0].id, approve=True), "approved")
        result = await asyncio.wait_for(task, timeout=20)
        self.assertEqual(result.end_reason, "model_stop")

        rows = {row.batch_index: row for row in await repository.batch(pending[0].batch_id)}
        self.assertEqual(rows[0].status, "approved")
        self.assertEqual(rows[1].status, "expired")
        self.assertIsNone(rows[1].consumed_at)
        self.assertEqual((self.workspace / "a.txt").read_text(encoding="utf-8"), "A")
        self.assertEqual((self.workspace / "b.txt").read_text(encoding="utf-8"), "old",
                         "an expired row must not be written")
        self.assertEqual((self.workspace / "c.txt").read_text(encoding="utf-8"), "C")

    async def test_cancelling_the_run_cancels_the_whole_batch(self):
        runner = self.runner([("a.txt", "A"), ("b.txt", "B")])
        session = await runner.conversations.create_session(self.workspace)
        run_id = []

        task = asyncio.create_task(runner.run(session, "写两个文件", on_started=run_id.append))
        pending = await self.wait_pending(runner, await self.run_id_of(session), count=2)
        self.assertTrue(runner.cancel(run_id[0]))
        with self.assertRaises(asyncio.CancelledError):
            await task

        rows = await self.approvals(runner).batch(pending[0].batch_id)
        self.assertTrue(all(row.status == "cancelled" for row in rows))
        self.assertTrue(all(row.consumed_at is None for row in rows))
        for name in ("a.txt", "b.txt"):
            self.assertEqual((self.workspace / name).read_text(encoding="utf-8"), "old")

    async def test_a_grant_cannot_be_spent_by_another_call(self):
        """The batch shares a decision, never a ticket."""
        runner = self.runner([("a.txt", "A"), ("b.txt", "B")])
        session = await runner.conversations.create_session(self.workspace)
        task = asyncio.create_task(runner.run(session, "写两个文件"))
        run_id = await self.run_id_of(session)
        pending = await self.wait_pending(runner, run_id, count=2)
        repository = self.approvals(runner)
        await repository.decide(pending[0].id, approve=True)
        await asyncio.wait_for(task, timeout=20)

        rows = await repository.batch(pending[0].batch_id)
        # Spending one row's grant against the sibling's tool record must fail.
        spent = await repository.consume(
            rows[0].id, run_id=run_id, tool_record_id=rows[1].tool_record_id,
            tool_name="write_file", arguments={"path": "b.txt", "content": "B"}, cwd=str(self.workspace))
        self.assertFalse(spent, "a grant was accepted for a different tool record")
        # And each row was consumed by its own call exactly once.
        self.assertTrue(all(row.consumed_at is not None for row in rows))

    async def test_one_write_keeps_the_single_question_shape(self):
        """A lone write is a batch of one: same card, same fields, no regression."""
        runner = self.runner([("only.txt", "X")])
        session = await runner.conversations.create_session(self.workspace)
        task = asyncio.create_task(runner.run(session, "写一个文件"))
        pending = await self.wait_pending(runner, await self.run_id_of(session), count=1)
        self.assertEqual(len(pending), 1)
        self.assertIsNotNone(pending[0].batch_id)
        self.assertEqual(await self.approvals(runner).decide(pending[0].id, approve=True), "approved")
        result = await asyncio.wait_for(task, timeout=20)
        self.assertEqual(result.end_reason, "model_stop")
        self.assertEqual((self.workspace / "only.txt").read_text(encoding="utf-8"), "X")

    async def test_a_new_file_is_still_not_a_question(self):
        runner = self.new_file_runner([("quiet.txt", "Q")])
        session = await runner.conversations.create_session(self.workspace)
        result = await asyncio.wait_for(runner.run(session, "新建文件"), timeout=20)
        self.assertEqual(result.end_reason, "model_stop")
        self.assertEqual((self.workspace / "quiet.txt").read_text(encoding="utf-8"), "Q")
        self.assertEqual(await self.approvals(runner).pending(result.run_id), [])

    async def test_no_viewer_is_unwatched_and_not_a_user_refusal(self):
        """A missing page is not a decision, and the batch path must say so.

        The batch closes its whole question with one conditional update. Reading
        that closed state back as "denied" would tell the model — and through it
        the user — that the change was judged and refused, which is the one thing
        nobody watching does not mean. The single-question path already reports
        ``unwatched``; the batch one is what every write actually uses.
        """
        runner = self.runner([("a.txt", "A"), ("b.txt", "B")])
        broker = runner.scheduler.approvals
        broker.audience = lambda run_id: False
        broker.grace = 0.3
        broker.poll = 0.1
        session = await runner.conversations.create_session(self.workspace)
        result = await asyncio.wait_for(runner.run(session, "没人看着"), timeout=20)
        self.assertEqual(result.end_reason, "model_stop")

        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT status, result FROM tool_calls WHERE run_id = %s ORDER BY call_index", (result.run_id,))
            rows = await cursor.fetchall()
        self.assertEqual([row["status"] for row in rows], ["failed", "failed"])
        for row in rows:
            details = row["result"]["details"]
            self.assertEqual(details["approval"], "unwatched")
            self.assertEqual(details["failure_class"], "unwatched")
            self.assertFalse(details["execution_started"])
        # Nothing was written, and the model was told nobody judged the change.
        self.assertEqual((self.workspace / "a.txt").read_text(encoding="utf-8"), "old")
        self.assertEqual((self.workspace / "b.txt").read_text(encoding="utf-8"), "old")
        answer = result.message.content[0].text
        self.assertIn("No browser page was waiting", answer)
        self.assertNotIn("user declined", answer)
        # The audit still records why the rows are closed, as internal data.
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT status, note FROM approval_requests WHERE run_id = %s ORDER BY batch_index",
                (result.run_id,))
            requests = await cursor.fetchall()
        self.assertEqual([row["status"] for row in requests], ["denied", "denied"])
        self.assertEqual([row["note"] for row in requests], ["no viewer", "no viewer"])
        self.assertEqual(await self.approvals(runner).pending(result.run_id), [])


if __name__ == "__main__":
    unittest.main()
