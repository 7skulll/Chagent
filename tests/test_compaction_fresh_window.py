"""The second compaction implementation: drop the span and restate what it was.

A model summary is one way to make room. The other is to stop paying for the
compression entirely, say plainly that the earlier turns are gone, and keep the
few things that were expensive to obtain. Both share one lifecycle, so the only
difference a reader of the events sees is the ``implementation`` field.
"""

import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.context.compactor import CompactionError, FRESH_WINDOW_MODEL, FRESH_WINDOW_VERSION
from chagent.domain.summaries import CompactionPolicy
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from test_long_runs import EmitTool, LongClient


def policy(**overrides):
    values = dict(trigger_ratio=0.9, keep_recent_ratio=0.4, max_summary_tokens=512, max_calls=4,
                  speculative=False)
    values.update(overrides)
    return CompactionPolicy(**values)


class FailingSummaryClient(LongClient):
    """The summarizer is unavailable; the task model still works."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.summary_attempts = 0

    async def stream(self, request):
        if request.purpose != "summary":
            async for event in super().stream(request):
                yield event
            return
        self.summary_attempts += 1
        self.requests.append(request)
        raise CompactionError("summarizer unavailable")


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class FreshWindowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_fresh_window_test_" + uuid4().hex
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
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def runner(self, *, client=None, **policy_overrides):
        runner = create_demo_runner(self.database, compaction_policy=policy(**policy_overrides))
        runner.model.context_window = 6_000
        runner.tools.registry.register(EmitTool("result " + "x" * 2_000))
        runner.client = client if client is not None else LongClient(steps=5)
        return runner

    async def start(self, runner, text="GOAL"):
        """Not called ``run``: unittest would treat it as the test runner."""
        session = await runner.conversations.create_session(Path(self.temporary.name))
        result = await runner.run(session, text)
        return session, result

    async def rows(self, query, params=()):
        async with self.database.connection() as connection:
            return await (await connection.execute(query, params)).fetchall()

    async def events(self, run_id):
        return await self.rows("SELECT type, payload FROM run_events WHERE run_id = %s ORDER BY sequence", (run_id,))

    # -- the two implementations ------------------------------------------

    async def test_always_skips_the_summarizer_entirely(self):
        runner = self.runner(fresh_window="always")
        session, result = await self.start(runner)
        self.assertEqual(result.message.stop_reason, "stop")
        self.assertEqual([request for request in runner.client.requests if request.purpose == "summary"], [])
        self.assertEqual(await self.rows("SELECT id FROM compaction_calls"), [], "no call was made")
        summaries = await self.rows("SELECT model, provider, prompt_version, usage, content FROM session_summaries")
        self.assertEqual(len(summaries), 1)
        self.assertEqual((summaries[0]["model"], summaries[0]["provider"]), (FRESH_WINDOW_MODEL, FRESH_WINDOW_MODEL))
        self.assertEqual(summaries[0]["prompt_version"], FRESH_WINDOW_VERSION)
        self.assertIsNone(summaries[0]["usage"], "no model wrote this checkpoint")
        completed = [event["payload"] for event in await self.events(result.run_id)
                     if event["type"] == "compaction_completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["implementation"], "fresh_window")
        self.assertIsNone(completed[0]["fallback_reason"])
        self.assertLess(completed[0]["tokens_after"], completed[0]["tokens_before"])
        self.assertEqual((await runner.conversations.context_snapshots(session))[0]["snapshot"]["summary_id"],
                         str((await self.rows("SELECT id FROM session_summaries"))[0]["id"]))

    async def test_a_failed_summary_falls_back_instead_of_ending_the_run(self):
        runner = self.runner(client=FailingSummaryClient(steps=5), fresh_window="fallback")
        session, result = await self.start(runner)
        self.assertEqual(result.message.stop_reason, "stop")
        event_types = [event["type"] for event in await self.events(result.run_id)]
        self.assertNotIn("compaction_failed", event_types,
                         "the fallback succeeded, so no compaction failed")
        completed = [event["payload"] for event in await self.events(result.run_id)
                     if event["type"] == "compaction_completed"]
        self.assertEqual(completed[0]["implementation"], "fresh_window")
        self.assertEqual(completed[0]["fallback_reason"], "summarizer unavailable")
        summaries = await self.rows("SELECT prompt_version FROM session_summaries")
        self.assertEqual([row["prompt_version"] for row in summaries], [FRESH_WINDOW_VERSION])
        # The attempt that failed was still paid for, so it stays in the audit.
        self.assertEqual([row["status"] for row in await self.rows("SELECT status FROM compaction_calls")],
                         ["failed"])

    async def test_with_the_fallback_off_a_failed_summary_still_ends_compaction(self):
        runner = self.runner(client=FailingSummaryClient(steps=5), fresh_window="off")
        session, result = await self.start(runner)
        event_types = [event["type"] for event in await self.events(result.run_id)]
        self.assertIn("compaction_failed", event_types)
        self.assertNotIn("compaction_completed", event_types)
        self.assertEqual(await self.rows("SELECT id FROM session_summaries"), [])
        self.assertEqual(result.message.stop_reason, "stop")

    # -- what the checkpoint says -----------------------------------------

    async def test_the_restatement_never_implies_the_dropped_turns_were_summarized(self):
        runner = self.runner(fresh_window="always")
        session, result = await self.start(runner)
        content = (await self.rows("SELECT content FROM session_summaries"))[0]["content"]
        self.assertIn("NOT summarized", content)
        self.assertIn("no tool in this session can read them back", content)
        self.assertIn("Dropped: ", content)
        self.assertIn("completed turn(s)", content)
        for forbidden in ("Goal:", "Progress:", "Decisions:"):
            self.assertNotIn(forbidden, content, "a restatement must not look like a summary")

    async def test_the_user_requests_of_the_dropped_turns_are_quoted_and_truncated(self):
        """Losing the user's own words is the most expensive part of a fresh window.

        Within one run the droppable span is made of tool exchanges, which carry no
        user message; the request that has to survive comes from an earlier run.
        """
        runner = self.runner(fresh_window="always", client=LongClient(steps=1))
        runner.model.context_window = 100_000
        session = await runner.conversations.create_session(Path(self.temporary.name))
        await runner.run(session, "keep this constraint " + "y" * 900)
        runner.model.context_window = 6_000
        runner.client = LongClient(steps=5)
        await runner.run(session, "GOAL")
        content = (await self.rows("SELECT content FROM session_summaries ORDER BY revision DESC"))[0]["content"]
        self.assertIn("User requests from those turns", content)
        self.assertIn("keep this constraint", content)
        self.assertIn("[truncated]", content)
        self.assertNotIn("y" * 400, content, "the quote must stay bounded")

    async def test_tool_evidence_survives_the_drop(self):
        runner = self.runner(fresh_window="always")
        session, result = await self.start(runner)
        content = (await self.rows("SELECT content FROM session_summaries"))[0]["content"]
        self.assertIn("retained verbatim", content)
        self.assertIn("result xxx", content)

    async def test_the_dropped_turns_are_no_longer_sent_but_the_current_input_is(self):
        runner = self.runner(fresh_window="always")
        session, result = await self.start(runner)
        snapshot = (await runner.conversations.context_snapshots(session))[0]["snapshot"]
        self.assertTrue(any(item["reason"] == "summarized" for item in snapshot["selections"]))
        # The current question survives verbatim and is still marked as this run's
        # input; everything older is either summarized or dropped.
        current = [item for item in snapshot["selections"] if item["reason"] == "current_turn"]
        self.assertTrue(current)
        request = (await self.rows(
            "SELECT request FROM model_calls ORDER BY iteration DESC LIMIT 1"))[0]["request"]
        rendered = [message["content"] for message in request["messages"]]
        self.assertTrue(any(isinstance(text, str) and "NOT summarized" in text for text in rendered))
        self.assertTrue(any(message["role"] == "user" and message["content"] == "GOAL"
                            for message in request["messages"]))

    async def test_speculation_is_not_started_for_a_fresh_window(self):
        runner = self.runner(fresh_window="always", speculative=True, speculative_trigger=0.3)
        session, result = await self.start(runner)
        started = [event["payload"] for event in await self.events(result.run_id)
                   if event["type"] == "compaction_started"]
        self.assertTrue(started)
        self.assertTrue(all(payload.get("speculative", False) is False for payload in started),
                        "a build is not worth hiding behind a background task")


class FreshWindowPolicyTests(unittest.TestCase):
    def test_the_mode_is_validated_where_it_is_read(self):
        with self.assertRaises(ValueError):
            CompactionPolicy(fresh_window="sometimes")
        with self.assertRaises(ValueError):
            CompactionPolicy.from_env({"CHAGENT_COMPACTION_FRESH_WINDOW": "sometimes"})
        self.assertEqual(CompactionPolicy(fresh_window="always").fresh_window, "always")

    def test_the_mode_defaults_to_the_fallback(self):
        self.assertEqual(CompactionPolicy().fresh_window, "fallback")
        self.assertEqual(CompactionPolicy.from_env({}).fresh_window, "fallback")
        self.assertEqual(CompactionPolicy.from_env({"CHAGENT_COMPACTION_FRESH_WINDOW": "OFF"}).fresh_window, "off")
