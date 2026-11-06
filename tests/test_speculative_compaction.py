"""Speculative compaction: pay for the summary early, publish it only when needed.

The rule under test is that speculation changes *when* the summarizer runs, never
*whether* a checkpoint is installed, and that nothing reaches the database until
the run that owns it adopts the candidate.
"""

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.domain.messages import UserMessage
from chagent.domain.summaries import CompactionPolicy
from chagent.models.base import Model
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from test_long_runs import EmitTool, LongClient


def policy(**overrides):
    """A low trigger so a handful of tool steps can cross every threshold."""
    values = dict(trigger_ratio=0.9, keep_recent_ratio=0.4, speculative_trigger=0.3,
                  max_summary_tokens=512, max_calls=4)
    values.update(overrides)
    return CompactionPolicy(**values)


class BlockingSummaryClient(LongClient):
    """Holds the summarizer open so the run ends with work still in flight."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = asyncio.Event()

    async def stream(self, request):
        if request.purpose != "summary":
            async for event in super().stream(request):
                yield event
            return
        self.requests.append(request)
        self.entered.set()
        await asyncio.Event().wait()


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class SpeculativeCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_speculative_test_" + uuid4().hex
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

    def runner(self, *, steps=5, window=6_000, chars=2_000, client=None, **policy_overrides):
        runner = create_demo_runner(self.database, compaction_policy=policy(**policy_overrides))
        runner.model.context_window = window
        runner.tools.registry.register(EmitTool("result " + "x" * chars))
        runner.client = client if client is not None else LongClient(steps=steps)
        return runner

    async def session(self, runner):
        return await runner.conversations.create_session(Path(self.temporary.name))

    async def events(self, run_id):
        return await runner_events(self.database, run_id)

    async def rows(self, query, params=()):
        async with self.database.connection() as connection:
            return await (await connection.execute(query, params)).fetchall()

    # -- the ordinary case ------------------------------------------------

    async def test_a_background_summary_is_adopted_instead_of_recomputed(self):
        """The only reason to speculate is that the calls already happened."""
        runner = self.runner(steps=5)
        session = await self.session(runner)
        result = await runner.run(session, "GOAL")
        event_types = [event["type"] for event in await self.events(result.run_id)]
        self.assertIn("compaction_completed", event_types)
        self.assertNotIn("compaction_failed", event_types)
        started = [event["payload"] for event in await self.events(result.run_id)
                   if event["type"] == "compaction_started"]
        self.assertTrue(started and started[0]["speculative"] is True,
                        "the first compaction must have been paid for in the background")
        self.assertLess(event_types.index("compaction_started"), event_types.index("compaction_completed"))
        summaries = await self.rows("SELECT id, revision, compaction_id FROM session_summaries ORDER BY revision")
        self.assertTrue(summaries)
        calls = await self.rows("SELECT compaction_id, status FROM compaction_calls")
        # Every paid summarization request is accounted for exactly once: a
        # second set of calls would mean the adopted candidate was thrown away
        # and the same work bought again.
        summary_requests = [request for request in runner.client.requests if request.purpose == "summary"]
        self.assertEqual(len(summary_requests), len(calls))
        self.assertTrue(all(row["status"] == "completed" for row in calls))
        self.assertIn(calls[0]["compaction_id"], {row["compaction_id"] for row in summaries})
        self.assertEqual(summaries[0]["revision"], 1)
        self.assertEqual(result.message.stop_reason, "stop")

    async def test_an_adopted_summary_is_what_the_model_saw(self):
        runner = self.runner(steps=5)
        session = await self.session(runner)
        result = await runner.run(session, "GOAL")
        snapshot = (await runner.conversations.context_snapshots(session))[0]["snapshot"]
        summary = (await self.rows("SELECT id FROM session_summaries"))[0]
        self.assertEqual(snapshot["summary_id"], str(summary["id"]))
        self.assertTrue(any(item["reason"] == "summarized" for item in snapshot["selections"]))
        self.assertEqual([event["type"] for event in await self.events(result.run_id)][-1], "run_completed")

    # -- cases where the candidate is not used ----------------------------

    async def test_a_candidate_the_run_never_needs_is_not_published(self):
        """Below the compaction trigger the request is already affordable."""
        runner = self.runner(steps=5, window=9_500)
        session = await self.session(runner)
        result = await runner.run(session, "GOAL")
        event_types = [event["type"] for event in await self.events(result.run_id)]
        self.assertIn("compaction_started", event_types, "the work must still have been started early")
        self.assertNotIn("compaction_completed", event_types)
        self.assertEqual(await self.rows("SELECT id FROM session_summaries"), [])
        self.assertEqual(await self.rows("SELECT id FROM compaction_calls"), [],
                         "an unadopted candidate leaves no audit row")
        self.assertEqual(runner.context_manager._speculations, {})
        self.assertEqual(result.message.stop_reason, "stop")

    def speculation(self, **overrides):
        from chagent.context.manager import _Speculation
        values = dict(identity=uuid4(), model_id="m", provider="p", tool_revision=3,
                      previous_id=None, previous_revision=None, progress=10, tokens_before=1)
        values.update(overrides)
        return _Speculation(**values)

    def test_a_candidate_is_invalidated_by_anything_that_shapes_the_prompt(self):
        model = Model("m", "p", "openai-chat", 4096, 256)
        speculation = self.speculation()
        self.assertTrue(speculation.valid(model=model, previous=None, tool_revision=3))
        self.assertFalse(speculation.valid(
            model=Model("other", "p", "openai-chat", 4096, 256), previous=None, tool_revision=3))
        self.assertFalse(speculation.valid(
            model=Model("m", "other", "openai-chat", 4096, 256), previous=None, tool_revision=3))
        self.assertFalse(speculation.valid(model=model, previous=None, tool_revision=4),
                         "the tool declarations live in the prefix, so a new set invalidates it")
        from chagent.domain.summaries import SessionSummary
        newer = SessionSummary(id=uuid4(), session_id=uuid4(), revision=2, previous_summary_id=None,
                               covered_through_sequence=1, first_kept_message_id=uuid4(), content="x",
                               model="m", provider="p", prompt_version="v", compaction_id=uuid4())
        self.assertFalse(speculation.valid(model=model, previous=newer, tool_revision=3),
                         "the candidate was built on top of a different checkpoint")
        self.assertTrue(self.speculation(previous_id=newer.id, previous_revision=2)
                        .valid(model=model, previous=newer, tool_revision=3))

    async def test_discarding_reports_the_wasted_calls_and_publishes_nothing(self):
        runner = self.runner(steps=1)
        session = await self.session(runner)
        run_id = await runner.runs.begin(session, UserMessage("discard me", 1))
        speculation = self.speculation()
        speculation.task = asyncio.create_task(asyncio.sleep(0))
        await speculation.task
        runner.context_manager._speculations[run_id] = speculation
        await runner.context_manager._discard(speculation, run_id, "invalidated")
        events = await self.events(run_id)
        discarded = [event["payload"] for event in events if event["type"] == "compaction_discarded"]
        self.assertEqual(len(discarded), 1)
        self.assertEqual(discarded[0]["reason"], "invalidated")
        self.assertEqual(discarded[0]["compaction_id"], str(speculation.identity))
        self.assertIn("speculative_calls", discarded[0])
        self.assertEqual(await self.rows("SELECT id FROM session_summaries"), [])
        self.assertEqual(await self.rows("SELECT id FROM compaction_calls"), [])
        self.assertEqual(runner.context_manager._speculations, {})
        await runner.runs.finish(run_id, "cancelled")

    # -- teardown and configuration ---------------------------------------

    async def test_run_teardown_cancels_work_still_in_flight(self):
        """A run that never needs the summary must not wait for it."""
        client = BlockingSummaryClient(steps=5)
        runner = self.runner(client=client, window=9_500)
        session = await self.session(runner)
        result = await runner.run(session, "GOAL")
        self.assertEqual(result.message.stop_reason, "stop")
        self.assertEqual(runner.context_manager._speculations, {},
                         "the run must not leave a task writing after its terminal state")
        self.assertEqual(await self.rows("SELECT id FROM session_summaries"), [])

    async def test_disabling_speculation_leaves_compaction_synchronous(self):
        runner = self.runner(steps=5, speculative=False)
        session = await self.session(runner)
        result = await runner.run(session, "GOAL")
        started = [event["payload"] for event in await self.events(result.run_id)
                   if event["type"] == "compaction_started"]
        self.assertTrue(started)
        self.assertTrue(all(payload.get("speculative", False) is False for payload in started))


class SpeculativePolicyTests(unittest.TestCase):
    def test_the_speculative_trigger_must_sit_below_the_compaction_trigger(self):
        with self.assertRaises(ValueError):
            CompactionPolicy(trigger_ratio=0.8, speculative_trigger=0.8)
        with self.assertRaises(ValueError):
            CompactionPolicy(trigger_ratio=0.8, speculative_trigger=0.0)
        self.assertEqual(CompactionPolicy(speculative_trigger=0.6).speculative_trigger, 0.6)

    def test_the_knobs_come_from_the_environment(self):
        parsed = CompactionPolicy.from_env({"CHAGENT_COMPACTION_SPECULATIVE": "false",
                                            "CHAGENT_COMPACTION_SPECULATIVE_TRIGGER": "0.55",
                                            "CHAGENT_COMPACTION_SPECULATIVE_TIMEOUT": "2"})
        self.assertFalse(parsed.speculative)
        self.assertEqual(parsed.speculative_trigger, 0.55)
        self.assertEqual(parsed.speculative_shutdown_timeout, 2)
        with self.assertRaises(ValueError):
            CompactionPolicy.from_env({"CHAGENT_COMPACTION_SPECULATIVE": "maybe"})

    def test_speculation_is_on_by_default(self):
        self.assertTrue(CompactionPolicy().speculative)


async def runner_events(database, run_id):
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT type, payload FROM run_events WHERE run_id = %s ORDER BY sequence", (run_id,))
        return await cursor.fetchall()
