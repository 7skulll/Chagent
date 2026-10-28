"""Event-path acceptance: timing, bounds, commit failures and SSE races."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

import httpx
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.runner import RunFailedError
from chagent.agent.stream_writer import TextStreamWriter
from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.domain.memories import MemoryPolicy
from chagent.domain.messages import AssistantMessage, TextContent, UserMessage
from chagent.events.notifications import RunNotifications
from chagent.models.events import ContentDeltaEvent, DoneEvent, StartEvent
from chagent.storage.database import Database
from chagent.storage.memory_jobs import MemoryJobRepository
from chagent.storage.migrate import migrate
from chagent.web.app import WebRuntime, create_app, events


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_fragment_immediate_and_pending_fragment_flushes_without_new_input(self):
        saved, changed = [], asyncio.Queue()

        async def save(text, count):
            saved.append(text)
            changed.put_nowait(text)

        writer = TextStreamWriter(save, interval=.02)
        self.addAsyncCleanup(writer.stop)
        await writer.append("first")
        self.assertEqual(await asyncio.wait_for(changed.get(), .5), "first")
        await writer.append("第二片")
        self.assertEqual(await asyncio.wait_for(changed.get(), .5), "第二片")
        await writer.close()
        self.assertEqual("".join(saved), "first第二片")

    async def test_first_fragment_does_not_wait_for_flush_deadline(self):
        saved = asyncio.Event()

        async def save(*_):
            saved.set()

        writer = TextStreamWriter(save, interval=60)
        self.addAsyncCleanup(writer.stop)
        await writer.append("first")
        await asyncio.wait_for(saved.wait(), .5)
        await writer.append("tail")
        await asyncio.wait_for(writer.flush(), .5)
        await writer.close()

    async def test_oversized_unicode_and_empty_fragments_remain_exact_and_bounded(self):
        saved = []

        async def save(text, count):
            self.assertLessEqual(len(text.encode("utf-8")), 32)
            saved.append(text)

        writer = TextStreamWriter(save, batch_bytes=32, capacity_bytes=64)
        self.addAsyncCleanup(writer.stop)
        text = "汉字😀abcd" * 1000
        await writer.append("")
        await writer.append(text)
        await writer.close()
        self.assertEqual("".join(saved), text)
        with self.assertRaises(RuntimeError):
            await writer.append("late")

    async def test_inflight_write_counts_toward_capacity_and_backpressures_producer(self):
        entered, release = asyncio.Event(), asyncio.Event()
        saved = []

        async def save(text, count):
            entered.set()
            await release.wait()
            saved.append(text)

        writer = TextStreamWriter(save, batch_bytes=32, capacity_bytes=64)
        self.addAsyncCleanup(writer.stop)
        producer = asyncio.create_task(writer.append("x" * 1000))
        try:
            await asyncio.wait_for(entered.wait(), .5)
            self.assertFalse(producer.done())
            release.set()
            await asyncio.wait_for(producer, 2)
            await writer.close()
            self.assertEqual("".join(saved), "x" * 1000)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def test_failed_write_releases_flush_and_capacity_waiters(self):
        entered = asyncio.Event()

        async def save(*_):
            entered.set()
            raise RuntimeError("controlled write failure")

        writer = TextStreamWriter(save, batch_bytes=32, capacity_bytes=64)
        self.addAsyncCleanup(writer.stop)
        producer = asyncio.create_task(writer.append("x" * 1000))
        flush = asyncio.create_task(writer.flush())
        await entered.wait()
        results = await asyncio.wait_for(asyncio.gather(producer, flush, return_exceptions=True), 1)
        self.assertTrue(any(isinstance(value, RuntimeError) for value in results))
        with self.assertRaisesRegex(RuntimeError, "controlled write failure"):
            await writer.close()


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_reaches_every_subscriber_without_competing(self):
        hub, identity = RunNotifications(), uuid4()
        with hub.subscribe(identity) as first, hub.subscribe(identity) as second:
            one, two = asyncio.create_task(first.wait(first.version)), asyncio.create_task(second.wait(second.version))
            await asyncio.sleep(0)
            hub.notify(identity)
            await asyncio.wait_for(asyncio.gather(one, two), .5)
        self.assertEqual(hub._subscriptions, {})

    async def test_notification_between_query_and_wait_is_not_lost(self):
        hub, identity = RunNotifications(), uuid4()
        with hub.subscribe(identity) as subscription:
            version = subscription.version
            hub.notify(identity)
            await asyncio.wait_for(subscription.wait(version, timeout=10), .1)
        hub.notify(uuid4())
        self.assertEqual(hub._subscriptions, {})

    async def test_no_notification_falls_back_and_cancelled_wait_cleans_up(self):
        hub, identity = RunNotifications(), uuid4()
        with hub.subscribe(identity) as subscription:
            await asyncio.wait_for(subscription.wait(subscription.version, timeout=.01), .5)
            waiter = asyncio.create_task(subscription.wait(subscription.version))
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        self.assertEqual(hub._subscriptions, {})


class FixtureClient:
    def __init__(self, *, block=False):
        self.block = block
        self.produced, self.closed = asyncio.Event(), asyncio.Event()

    async def stream(self, request):
        message = AssistantMessage([TextContent("")], "fake", "local-test", "test", 0, "pending")
        try:
            yield StartEvent(message)
            for _ in range(128):
                message.content[0].text += "😀汉字"
                yield ContentDeltaEvent("text_delta", 0, "😀汉字", message)
            self.produced.set()
            if self.block:
                await asyncio.Event().wait()
            message.stop_reason = "stop"
            yield DoneEvent("stop", message)
        finally:
            self.closed.set()


class ConnectedRequest:
    async def is_disconnected(self):
        return False


def updates(blocks):
    return [json.loads(next(line[6:] for line in block.splitlines() if line.startswith("data: ")))
            for block in blocks if "event: update" in block]


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class EventPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.schema = "chagent_event_test_" + uuid4().hex
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
        self.runner.client = FixtureClient()
        self.session = await self.runner.conversations.create_session(self.workspace)
        self.runtime = WebRuntime(self.runner, self.workspace)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def response(self, identity, after=0):
        return await events(identity, ConnectedRequest(), self.runtime, after=after, last_event_id=None)

    async def collect(self, response):
        return [block async for block in response.body_iterator]

    def assert_no_writers(self):
        self.assertFalse(any(task.get_name().startswith(("model-text-writer", "model-stream-"))
                             for task in asyncio.all_tasks() if not task.done()))

    async def test_heartbeat_failure_stops_run_without_writing_terminal_state(self):
        client = FixtureClient(block=True)
        self.runner.client = client
        self.runner.heartbeat_interval = .05
        async def fail_heartbeat(_run_id):
            raise ConnectionError("database unavailable")
        self.runner.runs.heartbeat = fail_heartbeat
        with self.assertRaises(RunFailedError) as caught:
            await asyncio.wait_for(self.runner.run(self.session, "test"), 3)
        self.assertEqual(caught.exception.end_reason, "storage_error")
        self.assertTrue(client.closed.is_set())
        row = await self.runner.runs.get(caught.exception.run_id)
        self.assertEqual(row["status"], "running")
        self.assertNotIn(caught.exception.run_id, self.database.run_owners)
        self.assert_no_writers()

    async def test_burst_commit_order_exact_replay_and_background_events_cutoff(self):
        original = self.runner.runs.save_response

        async def save(*args):
            prior = await self.runner.runs.events(args[0])
            self.assertEqual("".join(event["payload"]["delta"] for event in prior if event["type"] == "text_delta"), "😀汉字" * 128)
            return await original(*args)

        self.runner.runs.save_response = save
        result = await self.runner.run(self.session, "burst")
        persisted = await self.runner.runs.events(result.run_id)
        deltas = [event for event in persisted if event["type"] == "text_delta"]
        self.assertLessEqual(len(deltas), 4)
        self.assertEqual([event["sequence"] for event in persisted], list(range(1, len(persisted) + 1)))
        terminal = persisted[-1]["sequence"]
        await self.runner.runs.audit_event(result.run_id, "memory_saved", {"items": []})
        blocks = await self.collect(await self.response(result.run_id))
        self.assertNotIn("memory_saved", [event["type"] for event in updates(blocks)])
        self.assertEqual(blocks[-1], "event: end\ndata: {}\n\n")
        self.assertEqual(await self.collect(await self.response(result.run_id, after=terminal + 1)), ["event: end\ndata: {}\n\n"])
        self.assertEqual(blocks, await self.collect(await self.response(result.run_id)))
        self.assert_no_writers()

    async def test_commit_failure_interrupts_blocked_provider_and_never_notifies_rollback(self):
        self.runner.client = FixtureClient(block=True)
        async with self.database.connection() as connection:
            await connection.execute("""
                CREATE FUNCTION reject_text() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.type = 'text_delta' THEN RAISE EXCEPTION 'controlled text rejection'; END IF; RETURN NEW; END $$;
                CREATE CONSTRAINT TRIGGER reject_text AFTER INSERT ON run_events
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_text();
            """)
        with self.assertRaises(RunFailedError) as caught:
            await asyncio.wait_for(self.runner.run(self.session, "write failure"), 3)
        self.assertEqual(caught.exception.end_reason, "storage_error")
        self.assertTrue(self.runner.client.closed.is_set())
        persisted = await self.runner.runs.events(caught.exception.run_id)
        self.assertNotIn("text_delta", [event["type"] for event in persisted])
        self.assertEqual(persisted[-1]["type"], "run_failed")
        self.assert_no_writers()
        # Direct event commit failure must leave notification version unchanged.
        with self.database.notifications.subscribe(caught.exception.run_id) as subscription:
            version = subscription.version
            with self.assertRaises(Exception):
                await self.runner.runs.event(caught.exception.run_id, "text_delta", {"delta": "rejected"})
            self.assertEqual(subscription.version, version)

    async def test_slow_storage_is_bounded_but_does_not_serially_block_provider(self):
        release, entered = asyncio.Event(), asyncio.Event()
        original = self.runner.runs.event

        async def slow(identity, kind, payload):
            if kind == "text_delta":
                entered.set()
                await release.wait()
            return await original(identity, kind, payload)

        self.runner.runs.event = slow
        task = asyncio.create_task(self.runner.run(self.session, "slow storage"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(self.runner.client.produced.wait(), 2)
            self.assertFalse(task.done())
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assert_no_writers()

    async def test_cancel_waits_for_tail_commit_before_terminal_and_leaves_no_tasks(self):
        release, entered = asyncio.Event(), asyncio.Event()
        original = self.runner.runs.event
        self.runner.client = FixtureClient(block=True)
        identities = []

        async def slow(identity, kind, payload):
            if kind == "text_delta":
                entered.set()
                await release.wait()
            return await original(identity, kind, payload)

        self.runner.runs.event = slow
        task = asyncio.create_task(self.runner.run(self.session, "cancel", on_started=identities.append))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.runner.cancel(identities[0])
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            persisted = await self.runner.runs.events(identities[0])
            self.assertEqual(persisted[-1]["type"], "run_cancelled")
            self.assertTrue(any(event["type"] == "text_delta" for event in persisted))
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assert_no_writers()

    async def test_sse_query_wait_race_is_recovered_without_fallback_delay(self):
        identity = await self.runner.runs.begin(self.session, UserMessage("fixture", 0))
        original = self.runner.runs.events
        raced = False

        async def race(run_id, after=0, **kwargs):
            nonlocal raced
            result = await original(run_id, after, **kwargs)
            if not raced:
                raced = True
                await self.runner.runs.event(run_id, "text_delta", {"delta": "raced"})
            return result

        self.runner.runs.events = race
        response = await self.response(identity)
        iterator = response.body_iterator
        try:
            self.assertIn("run_started", await anext(iterator))
            self.assertIn("raced", await asyncio.wait_for(anext(iterator), .5))
        finally:
            await iterator.aclose()
            await self.runner.runs.finish(identity, "cancelled")
        self.assertEqual(self.database.notifications._subscriptions, {})

    async def test_two_sse_subscribers_receive_identical_events(self):
        identity = await self.runner.runs.begin(self.session, UserMessage("fixture", 0))
        first = asyncio.create_task(self.collect(await self.response(identity)))
        second = asyncio.create_task(self.collect(await self.response(identity)))
        await asyncio.sleep(.02)
        await self.runner.runs.event(identity, "text_delta", {"delta": "broadcast"})
        await self.runner.runs.finish(identity, "completed")
        one, two = await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(one, two)
        self.assertIn("broadcast", "".join(one))
        self.assertEqual(self.database.notifications._subscriptions, {})

    async def test_slow_sse_reader_does_not_hold_up_completion(self):
        identity = await self.runner.runs.begin(self.session, UserMessage("fixture", 0))
        response = await self.response(identity)
        iterator = response.body_iterator
        first = await anext(iterator)
        # Reader is paused after yielding, producer can still commit everything.
        await asyncio.wait_for(self.runner.runs.event(identity, "text_delta", {"delta": "tail"}), 1)
        await asyncio.wait_for(self.runner.runs.finish(identity, "completed"), 1)
        rest = [block async for block in iterator]
        self.assertIn("tail", "".join([first, *rest]))
        self.assertEqual(rest[-1], "event: end\ndata: {}\n\n")

    async def test_idle_subscriber_polls_at_most_one_fallback_round_per_second(self):
        identity = await self.runner.runs.begin(self.session, UserMessage("fixture", 0))
        iterator = (await self.response(identity)).body_iterator
        await anext(iterator)
        original, calls = self.runner.runs.events, []

        async def counted(*args, **kwargs):
            calls.append(asyncio.get_running_loop().time())
            return await original(*args, **kwargs)

        self.runner.runs.events = counted
        pending = asyncio.create_task(anext(iterator))
        try:
            await asyncio.sleep(1.15)
            self.assertLessEqual(len(calls), 2)
            await self.runner.runs.finish(identity, "cancelled")
            await asyncio.wait_for(pending, .5)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await iterator.aclose()

    async def test_missing_notification_is_recovered_by_fallback_query(self):
        identity = await self.runner.runs.begin(self.session, UserMessage("fixture", 0))
        iterator = (await self.response(identity)).body_iterator
        await anext(iterator)
        pending = asyncio.create_task(anext(iterator))
        try:
            await asyncio.sleep(.02)
            with patch.object(self.database.notifications, "notify"):
                await self.runner.runs.event(identity, "text_delta", {"delta": "without-notification"})
            self.assertIn("without-notification", await asyncio.wait_for(pending, 1.5))
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await iterator.aclose()
            await self.runner.runs.finish(identity, "cancelled")

    async def test_subscriber_disconnect_releases_registration(self):
        identity = await self.runner.runs.begin(self.session, UserMessage("fixture", 0))
        iterator = (await self.response(identity)).body_iterator
        await anext(iterator)
        pending = asyncio.create_task(anext(iterator))
        await asyncio.sleep(.01)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await iterator.aclose()
        self.assertEqual(self.database.notifications._subscriptions, {})
        await self.runner.runs.finish(identity, "cancelled")

    async def test_cancel_with_stuck_tail_has_bounded_cleanup_and_no_success(self):
        self.runner.client = FixtureClient(block=True)
        entered = asyncio.Event()
        original = self.runner.runs.event
        identities = []

        async def stuck(identity, kind, payload):
            if kind == "text_delta":
                entered.set()
                await asyncio.Event().wait()
            return await original(identity, kind, payload)

        self.runner.runs.event = stuck
        task = asyncio.create_task(self.runner.run(self.session, "cancel stuck", on_started=identities.append))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.runner.cancel(identities[0])
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            persisted = await self.runner.runs.events(identities[0])
            self.assertEqual(persisted[-1]["type"], "run_cancelled")
            self.assertNotIn("run_completed", [event["type"] for event in persisted])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assert_no_writers()

    async def test_job_status_is_separate_and_response_does_not_expose_internal_fields(self):
        result = await self.runner.run(self.session, "job")
        source = (await self.runner.conversations.messages(self.session))[0]
        jobs = MemoryJobRepository(self.database)
        async with self.database.transaction() as connection:
            await jobs.enqueue(connection, session_id=self.session, run_id=result.run_id,
                               source_message_id=source.id, prompt_version="test")
        self.runner.memory_worker = None  # Keep this queued job pending while SSE ends.
        app = create_app(workspace=self.workspace, injected_runtime=self.runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                response = await client.get(f"/api/runs/{result.run_id}/memory-job")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["job"]["status"], "queued")
                self.assertNotIn("last_error", response.json()["job"])
                stream = await client.get(f"/api/runs/{result.run_id}/events")
                self.assertIn("event: end", stream.text)
                self.assertEqual((await jobs.for_run(result.run_id))["status"], "queued")
