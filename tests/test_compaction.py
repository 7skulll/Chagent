"""Compaction correctness, failure isolation and restart recovery on PostgreSQL."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.runner import RunFailedError
from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.context.builder import BasicContextBuilder
from chagent.context.tokens import HeuristicTokenCounter
from chagent.context.compactor import (
    RETAINED_HEADER, USER_QUOTES_HEADER, attach_evidence, quote_block, retained_outputs,
    serialize_turn, split_evidence, strip_retained, user_requests,
)
from chagent.domain.messages import AssistantMessage, ImageContent, TextContent, ThinkingContent, ToolCall, ToolResultMessage, UserMessage
from chagent.domain.runs import StoredMessage
from chagent.domain.sessions import TaskState
from chagent.domain.summaries import CompactionPolicy
from chagent.domain.usage import Usage
from chagent.models.events import DoneEvent, StartEvent
from chagent.models.fake import FakeModelClient
from chagent.sessions.errors import StateConflictError
from chagent.sessions.manager import SessionManager
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.summaries import SummaryRepository


class RecordingClient(FakeModelClient):
    def __init__(self, mode="success"):
        super().__init__()
        self.mode, self.requests = mode, []
        self.entered = asyncio.Event()

    async def stream(self, request):
        self.requests.append(deepcopy(request))
        if request.purpose != "summary":
            async for event in super().stream(request):
                yield event
            return
        self.entered.set()
        if self.mode == "block":
            await asyncio.Event().wait()
        if self.mode == "exception":
            raise RuntimeError("private-test-credential")
        message = AssistantMessage([], request.model.api, request.model.provider, request.model.id, 1, "pending")
        yield StartEvent(partial=message)
        if self.mode == "incomplete":
            return
        message = deepcopy(message)
        message.stop_reason = "length" if self.mode == "length" else "stop"
        content = "Goal: continue project. Constraints: preserve user requirements. Progress: old turns finished. Pending: current input."
        if self.mode == "empty":
            content = " "
        elif self.mode == "oversize":
            content = "huge" * 5000
        message.content = ([ThinkingContent("private-summary-reasoning"), TextContent(content)]
                           if self.mode == "thinking" else [TextContent(content)])
        if self.mode == "tool":
            message.content = [ToolCall("forbidden", "write_file", {"path": "no.txt", "content": "no"})]
            message.stop_reason = "toolUse"
        message.usage = Usage(10, 5, 0, 0, 15)
        yield DoneEvent(reason=message.stop_reason, message=message)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class CompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_compaction_test_" + uuid4().hex
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
        self.runner = create_demo_runner(self.database, compaction_policy=CompactionPolicy())
        self.client = RecordingClient()
        self.runner.client = self.client
        self.summaries = SummaryRepository(self.database)
        # Empty workspace: the environment snapshot is part of the prompt.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.session = await self.runner.conversations.create_session(Path(self.temporary.name))

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def summary_only(self):
        """Pin the summary implementation by turning the fresh-window fallback off.

        ``CompactionPolicy`` now falls back to a host-authored checkpoint when the
        summarizer fails. These cases are about what the *summary* path guarantees,
        so the fallback is disabled at the point of the assertion rather than in
        the shared runner; the fallback itself is covered in
        ``test_compaction_fresh_window.py``.
        """
        manager = self.runner.context_manager
        manager.policy = replace(manager.policy, fresh_window="off")

    async def seed(self, count=4, size=2400):
        self.runner.model.context_window = 100_000
        for index in range(count):
            await self.runner.run(self.session, f"old turn {index}: " + "x" * size)
        self.runner.model.context_window = 13_000
        self.client.requests.clear()

    async def calls(self, run_id):
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM compaction_calls WHERE run_id = %s ORDER BY iteration", (run_id,))
            return await cursor.fetchall()

    async def test_checkpoint_keeps_originals_and_reloads_after_restart(self):
        await self.seed()
        original = await self.runner.conversations.messages(self.session)
        await SessionManager(self.runner.conversations).update_task_state(
            self.session, TaskState(goal="finish", constraints=["do not delete data"]), expected_revision=0,
        )
        result = await self.runner.run(self.session, "continue")
        summary = await self.summaries.latest(self.session)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.revision, 1)
        self.assertLess(summary.covered_through_sequence, (await self.runner.conversations.messages(self.session))[-4].sequence)
        self.assertEqual((await self.runner.conversations.messages(self.session))[:len(original)], original)
        events = await self.runner.runs.events(result.run_id)
        kinds = [event["type"] for event in events]
        self.assertIn("compaction_completed", kinds)
        self.assertLess(kinds.index("compaction_completed"), kinds.index("model_started"))
        self.assertEqual(kinds.count("compaction_started"), 1)
        for request in self.client.requests:
            if request.purpose == "summary":
                self.assertTrue(all(not getattr(message, "tools_added", None) for message in request.messages))
            else:
                self.assertIn("do not delete data", request.messages[0].text())
                self.assertIsInstance(request.messages[1], UserMessage)
                self.assertIn(summary.content, request.messages[1].content)
        snapshots = await SessionManager(self.runner.conversations).contexts(self.session)
        self.assertEqual(snapshots[0]["snapshot"]["summary_id"], str(summary.id))
        self.assertTrue(any(item["reason"] == "summarized" for item in snapshots[0]["snapshot"]["selections"]))
        calls = await self.calls(result.run_id)
        self.assertTrue(all(call["status"] == "completed" and call["duration_ms"] >= 0 for call in calls))
        self.assertEqual(summary.usage.total_tokens, len(calls) * 15)
        restored = create_demo_runner(self.database)
        restored.model.context_window = 13_000
        restored.client = RecordingClient()
        await restored.run(self.session, "after restart")
        latest = (await SessionManager(restored.conversations).contexts(self.session))[0]["snapshot"]
        self.assertEqual(latest["summary_id"], str(summary.id))

    async def test_reasoning_model_summary_keeps_text_without_thinking(self):
        await self.seed()
        self.client.mode = "thinking"
        result = await self.runner.run(self.session, "continue with reasoning model")
        summary = await self.summaries.latest(self.session)
        self.assertIsNotNone(summary)
        self.assertIn("Goal: continue project", summary.content)
        self.assertNotIn("private-summary-reasoning", summary.content)
        self.assertIn("compaction_completed", [event["type"] for event in await self.runner.runs.events(result.run_id)])
        calls = await self.calls(result.run_id)
        self.assertTrue(calls and all(call["status"] == "completed" for call in calls))

    async def test_summary_keeps_the_user_requests_it_summarized(self):
        """The user's words are quoted by the host, not paraphrased by the model."""
        await self.seed()
        await self.runner.run(self.session, "keep this constraint")
        summary = await self.summaries.latest(self.session)
        self.assertIsNotNone(summary)
        self.assertIn(USER_QUOTES_HEADER, summary.content)
        history = await self.runner.conversations.messages(self.session)
        covered = [item for item in history if str(item.id) in set(summary.source_message_ids)
                   and isinstance(item.message, UserMessage)]
        self.assertTrue(covered)
        for item in covered[-3:]:
            self.assertIn(item.message.content[:40], summary.content)
        # The request this run was answering is *not* quoted here: the checkpoint
        # did not cover it, so it stays in the transcript verbatim and quoting it
        # twice would only spend budget on a duplicate.
        self.assertNotIn("keep this constraint", summary.content)

    async def test_user_quotes_are_carried_verbatim_by_the_host(self):
        await self.seed()
        manager = self.runner.context_manager
        manager.policy = replace(manager.policy, retained_user_requests=16)
        manager.compactor.policy = manager.policy
        await self.runner.run(self.session, "first compact")
        first = await self.summaries.latest(self.session)
        _, quotes = split_evidence(first.content)
        self.assertTrue(quotes)
        oldest = quotes[0]["text"][:40]

        await self.seed(count=3, size=2600)
        await self.runner.run(self.session, "second compact")
        second = await self.summaries.latest(self.session)
        self.assertEqual(second.revision, 2)
        self.assertIn(oldest, second.content)
        self.assertIn(USER_QUOTES_HEADER, second.content)

    async def test_the_quote_block_never_reaches_the_summarizer(self):
        """A model that saw the quotes could reword the text meant to stay verbatim."""
        await self.seed()
        await self.runner.run(self.session, "first compact")
        first = await self.summaries.latest(self.session)
        await self.seed(count=3, size=2600)
        await self.runner.run(self.session, "second compact")
        request = [request for request in self.client.requests if request.purpose == "summary"][-1]
        previous = json.loads(request.messages[1].content)["previous_summary"]
        self.assertNotIn(USER_QUOTES_HEADER, previous)
        self.assertNotIn(RETAINED_HEADER, previous)
        self.assertEqual(previous, strip_retained(first.content))

    async def test_compaction_retains_the_newest_tool_output_verbatim(self):
        """Summarizing the evidence makes the model re-read it; carry it instead."""
        await self.seed()
        await self.runner.run(self.session, "continue")
        summary = await self.summaries.latest(self.session)
        self.assertIsNotNone(summary)
        self.assertIn(RETAINED_HEADER, summary.content)
        history = await self.runner.conversations.messages(self.session)
        covered = set(summary.source_message_ids)
        results = [item.message for item in history
                   if str(item.id) in covered and isinstance(item.message, ToolResultMessage)]
        self.assertTrue(results)
        newest = "\n".join(block.text for block in results[-1].content if isinstance(block, TextContent))
        self.assertIn(newest[:200], summary.content)
        # The model sees the evidence in the summary message of the same run.
        self.assertTrue(any(RETAINED_HEADER in request.messages[1].content
                            for request in self.client.requests
                            if request.purpose == "task" and isinstance(request.messages[1], UserMessage)))

    async def test_incremental_checkpoint_uses_previous_summary_and_new_sources(self):
        await self.seed()
        await self.runner.run(self.session, "first compact")
        previous = await self.summaries.latest(self.session)
        await self.seed(count=3, size=2600)
        await self.runner.run(self.session, "second compact")
        summary = await self.summaries.latest(self.session)
        self.assertEqual(summary.revision, 2)
        self.assertEqual(summary.previous_summary_id, previous.id)
        self.assertGreater(summary.covered_through_sequence, previous.covered_through_sequence)
        self.assertFalse(set(summary.source_message_ids) & set(previous.source_message_ids))
        request = next(request for request in self.client.requests if request.purpose == "summary")
        # Retained evidence is re-derived from the turns on every compaction, so
        # only the summary text travels forward.
        self.assertEqual(json.loads(request.messages[1].content)["previous_summary"],
                         strip_retained(previous.content))
        self.assertEqual(len(await self.summaries.list(self.session)), 2)

    async def test_multiple_bounded_summary_calls_create_one_checkpoint(self):
        await self.seed(count=7, size=3000)
        result = await self.runner.run(self.session, "compact a backlog")
        summary = await self.summaries.latest(self.session)
        self.assertIsNotNone(summary)
        calls = await self.calls(result.run_id)
        self.assertGreater(len(calls), 1)
        self.assertEqual(len({call["compaction_id"] for call in calls}), 1)
        self.assertEqual(len(await self.summaries.list(self.session)), 1)
        requests = [request for request in self.client.requests if request.purpose == "summary"]
        for request in requests:
            self.assertLessEqual(self.runner.context.counter.count(request.messages) + request.max_tokens + 256, 13_000)
        self.assertTrue(json.loads(requests[1].messages[1].content)["previous_summary"])

    async def test_failed_summary_never_activates_and_credentials_are_not_logged(self):
        self.summary_only()
        for mode in ("empty", "length", "oversize", "tool", "incomplete", "exception"):
            with self.subTest(mode=mode):
                self.session = await self.runner.conversations.create_session(Path(self.temporary.name))
                self.client.mode = "success"
                await self.seed()
                self.client.mode = mode
                result = await self.runner.run(self.session, "fallback safely")
                self.assertIsNone(await self.summaries.latest(self.session))
                events = await self.runner.runs.events(result.run_id)
                self.assertEqual(sum(event["type"] == "compaction_started" for event in events), 1)
                self.assertIn("compaction_failed", [event["type"] for event in events])
                self.assertNotIn("private-test-credential", json.dumps(events, default=str))
                calls = await self.calls(result.run_id)
                self.assertTrue(all(call["status"] == "failed" for call in calls))
                self.assertNotIn("private-test-credential", json.dumps(calls, default=str))
                self.assertEqual(result.message.stop_reason, "stop")

    async def test_cancellation_does_not_activate_checkpoint(self):
        await self.seed()
        self.client.mode = "block"
        self.client.entered.clear()
        identity = None
        def started(value):
            nonlocal identity
            identity = value
        task = asyncio.create_task(self.runner.run(self.session, "cancel compact", on_started=started))
        await asyncio.wait_for(self.client.entered.wait(), 5)
        self.runner.cancel(identity)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(await self.summaries.latest(self.session))
        self.assertEqual((await self.runner.runs.get(identity))["status"], "cancelled")
        self.assertTrue(all(call["status"] == "cancelled" for call in await self.calls(identity)))
        self.assertIn("compaction_cancelled", [event["type"] for event in await self.runner.runs.events(identity)])
        self.client.mode = "success"
        await self.runner.run(self.session, "continue after cancel")
        self.assertIsNotNone(await self.summaries.latest(self.session))

    async def test_current_run_stays_complete_and_stale_checkpoint_is_rejected(self):
        await self.seed()
        await self.runner.run(self.session, "compact")
        summary = await self.summaries.latest(self.session)
        run_id = await self.runner.runs.begin(self.session, UserMessage("new run", 1))
        try:
            with self.assertRaises(StateConflictError):
                await self.summaries.save(replace(summary, id=uuid4(), compaction_id=uuid4()), run_id)
        finally:
            await self.runner.runs.finish(run_id, "cancelled")
        self.runner.model.context_window = 1000
        with self.assertRaises(RunFailedError) as error:
            await self.runner.run(self.session, "current" * 500)
        self.assertEqual(await self.calls(error.exception.run_id), [])
        self.assertEqual((await self.summaries.latest(self.session)).id, summary.id)

    async def test_failed_kept_run_does_not_break_summary_replay(self):
        await self.seed()
        # Failure occurs after summary commit; its kept input disappears from model replay.
        original_stream = self.client.stream
        async def fail_tasks(request):
            if request.purpose == "task":
                raise RuntimeError("task fails after compaction")
            async for event in original_stream(request):
                yield event
        self.client.stream = fail_tasks
        with self.assertRaises(RunFailedError):
            await self.runner.run(self.session, "will fail")
        summary = await self.summaries.latest(self.session)
        self.assertIsNotNone(summary)
        self.client.stream = original_stream
        result = await self.runner.run(self.session, "recover with checkpoint")
        self.assertEqual(result.message.stop_reason, "stop")

    async def test_timeout_and_call_limit_leave_previous_checkpoint_active(self):
        self.summary_only()
        await self.seed()
        await self.runner.run(self.session, "initial checkpoint")
        previous = await self.summaries.latest(self.session)
        await self.seed(count=7, size=3000)
        manager = self.runner.context_manager
        manager.policy = replace(manager.policy, max_calls=1)
        manager.compactor.policy = manager.policy
        result = await self.runner.run(self.session, "bounded work")
        self.assertEqual((await self.summaries.latest(self.session)).id, previous.id)
        self.assertEqual(len(await self.calls(result.run_id)), 1)
        self.assertIn("compaction_failed", [event["type"] for event in await self.runner.runs.events(result.run_id)])
        manager.policy = replace(manager.policy, max_calls=8, timeout=.02)
        manager.compactor.policy = manager.policy
        self.client.mode = "block"
        result = await self.runner.run(self.session, "summary timeout")
        self.assertEqual((await self.summaries.latest(self.session)).id, previous.id)
        self.assertTrue(all(call["status"] == "cancelled" for call in await self.calls(result.run_id)))
        self.assertIn("compaction_failed", [event["type"] for event in await self.runner.runs.events(result.run_id)])

    async def test_single_oversized_old_turn_is_not_silently_truncated(self):
        self.summary_only()
        await self.seed(count=1, size=45_000)
        result = await self.runner.run(self.session, "continue past huge old turn")
        self.assertIsNone(await self.summaries.latest(self.session))
        self.assertEqual(await self.calls(result.run_id), [])
        events = await self.runner.runs.events(result.run_id)
        self.assertTrue(any("One completed turn" in event["payload"].get("reason", "") for event in events))


class CompactionUnitTests(unittest.TestCase):
    def test_serialization_excludes_thinking_images_and_host_metadata(self):
        turn = [StoredMessage(uuid4(), 1, UserMessage([TextContent("keep requirement"), ImageContent("private-image", "image/png")], 1)),
                StoredMessage(uuid4(), 2, AssistantMessage([ThinkingContent("private-thinking")], "fake", "test", "test", 2, "stop")),
                StoredMessage(uuid4(), 3, ToolResultMessage("tool", "read_file", [TextContent("x" * 3000)], False, 3, details={"secret": "host-only"}))]
        result = serialize_turn(turn)
        for value in ("private-thinking", "private-image", "host-only"):
            self.assertNotIn(value, result)
        self.assertIn("keep requirement", result)
        self.assertIn("1000 characters omitted", result)

    def test_policy_validation(self):
        self.assertFalse(CompactionPolicy.from_env({"CHAGENT_COMPACTION_ENABLED": "false"}).enabled)
        for values in ({"CHAGENT_COMPACTION_TRIGGER": "nan"}, {"CHAGENT_COMPACTION_TIMEOUT": "inf"},
                       {"CHAGENT_COMPACTION_ENABLED": "yes"}, {"CHAGENT_COMPACTION_MAX_CALLS": "0"}):
            with self.assertRaises(ValueError):
                CompactionPolicy.from_env(values)

class CompactorStreamTests(unittest.IsolatedAsyncioTestCase):
    """The summarizer's provider stream is closed on the error paths too.

    ``_response`` rejects a stream that keeps talking after its terminal event.
    The connection behind that generator belongs to the provider client and is
    not released by breaking out of ``async for``, so the rejection has to close
    it explicitly — the run's own model call does the same.
    """

    class _BrokenClient:
        def __init__(self):
            self.closed = False

        def stream(self, request):
            async def events():
                message = AssistantMessage([], request.model.api, request.model.provider,
                                           request.model.id, 0, "pending")
                yield StartEvent(message)
                final = replace(message, content=[TextContent("ok")], stop_reason="stop")
                yield DoneEvent("stop", final)
                yield DoneEvent("stop", final)   # rejected: after completion

            async def generator():
                try:
                    async for event in events():
                        yield event
                finally:
                    self.closed = True
            return generator()

    async def test_a_rejected_stream_is_closed(self):
        from chagent.context.compactor import CompactionError, ContextCompactor
        from chagent.models.base import Model, ModelRequest
        client = self._BrokenClient()
        request = ModelRequest(Model("m", "local-test", "fake", 8192, 512), messages=[],
                               max_tokens=64, purpose="summary")
        with self.assertRaises(CompactionError):
            await ContextCompactor._response(client, request)
        self.assertTrue(client.closed, "the provider generator must be closed, not left to the GC")


class RetainedOutputTests(unittest.TestCase):
    """The retained block is bounded, ordered and never confused with facts."""

    def turn(self, *messages):
        return [StoredMessage(uuid4(), index, message) for index, message in enumerate(messages, start=1)]

    def result(self, text, name="read_file"):
        return ToolResultMessage("call", name, [TextContent(text)], False, 1)

    def test_keeps_the_newest_results_in_prompt_order(self):
        turns = [self.turn(self.result("old result")), self.turn(self.result("new result"))]
        text = retained_outputs(turns, 1)
        self.assertIn("new result", text)
        self.assertNotIn("old result", text)
        both = retained_outputs(turns, 2)
        self.assertLess(both.index("old result"), both.index("new result"))

    def test_long_results_are_truncated_and_the_total_is_capped(self):
        long_turn = [self.turn(self.result("x" * 5000))]
        text = retained_outputs(long_turn, 1)
        self.assertLess(len(text), 1000)
        self.assertIn("excerpt", text)
        many = [self.turn(self.result("y" * 700)) for _ in range(10)]
        self.assertLessEqual(len(retained_outputs(many, 5)), 3000)

    def test_zero_disables_retention(self):
        self.assertEqual(retained_outputs([self.turn(self.result("body"))], 0), "")

    def test_strip_retained_keeps_only_the_summary_text(self):
        content = "Goal: done." + "\n\n" + RETAINED_HEADER + "\n\n[read_file]\nbody"
        self.assertEqual(strip_retained(content), "Goal: done.")


class UserQuoteTests(unittest.TestCase):
    """Quotes are host-authored, newest-first and never invented by the model."""

    def request(self, sequence, text):
        return {"sequence": sequence, "text": text}

    def turn(self, sequence, text):
        return [StoredMessage(uuid4(), sequence, UserMessage(text, 1))]

    def test_requests_include_the_current_input_once(self):
        turns = [self.turn(1, "old ask"), self.turn(2, "next ask")]
        requests = user_requests(turns, current="current ask", current_sequence=3)
        self.assertEqual([item["text"] for item in requests], ["old ask", "next ask", "current ask"])
        twice = user_requests(turns, current="current ask", current_sequence=2)
        self.assertEqual(len(twice), 2)

    def test_packing_keeps_the_newest_and_drops_the_oldest_whole(self):
        counter = HeuristicTokenCounter()
        content = "Goal: done."
        base = counter.count([BasicContextBuilder.summary_message(content)])
        requests = [self.request(1, "old " + "a" * 400),
                    self.request(2, "mid " + "b" * 400),
                    self.request(3, "new " + "c" * 400)]
        tight = quote_block(content, requests, counter, base + 150, keep=3)
        self.assertIn("new ", tight)
        self.assertNotIn("old ", tight)
        roomy = quote_block(content, requests, counter, 100_000, keep=3)
        for marker in ("old ", "mid ", "new "):
            self.assertIn(marker, roomy)

    def test_long_requests_are_truncated_not_reworded(self):
        counter = HeuristicTokenCounter()
        content = quote_block("Goal: done.", [self.request(1, "k" * 2000)], counter, 100_000, keep=1)
        self.assertIn("k" * 100, content)
        self.assertIn("[truncated]", content)
        self.assertLess(len(content), 1400)

    def test_evidence_round_trips_and_strips_both_blocks(self):
        counter = HeuristicTokenCounter()
        requests = [self.request(7, "do not delete data")]
        content = attach_evidence("Goal: done.", requests, "[read_file]\nbody", counter, 100_000, keep=4)
        body, quotes = split_evidence(content)
        self.assertEqual(body, "Goal: done.")
        self.assertEqual(quotes, requests)
        self.assertIn(USER_QUOTES_HEADER, content)
        self.assertIn(RETAINED_HEADER, content)

    def test_an_unreadable_block_is_dropped_not_guessed(self):
        body, quotes = split_evidence("Goal: done." + "\n\n" + USER_QUOTES_HEADER + "\n\n{not json")
        self.assertEqual(body, "Goal: done.")
        self.assertEqual(quotes, [])
        self.assertEqual(strip_retained("Goal: done."), "Goal: done.")
