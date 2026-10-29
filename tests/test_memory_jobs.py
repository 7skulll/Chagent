"""Completion must not wait for extraction; the queue owns retries and leases."""

import asyncio
import json
import os
from time import monotonic
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.domain.memories import MemoryPolicy
from chagent.domain.messages import AssistantMessage, TextContent
from chagent.models.events import DoneEvent, StartEvent
from chagent.models.fake import FakeModelClient
from chagent.storage.database import Database
from chagent.storage.memories import MemoryRepository
from chagent.storage.memory_jobs import MemoryJobClaimLost, MemoryJobRepository
from chagent.storage.migrate import migrate


class SlowExtractClient(FakeModelClient):
    """Answers immediately, but a memory extraction takes a long time."""

    def __init__(self, delay=2.0):
        super().__init__()
        self.delay = delay

    async def stream(self, request):
        if request.purpose != "memory":
            async for event in super().stream(request):
                yield event
            return
        await asyncio.sleep(self.delay)
        message = AssistantMessage([], request.model.api, request.model.provider, request.model.id, 1, "pending")
        yield StartEvent(partial=message)
        final = AssistantMessage([TextContent(json.dumps({"memories": []}))], request.model.api,
                                 request.model.provider, request.model.id, 1, "stop")
        yield DoneEvent(reason="stop", message=final)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class MemoryJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_jobs_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.runner = create_demo_runner(self.database, memory_policy=MemoryPolicy())
        # Empty workspace: the environment snapshot is part of the prompt.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.session = await self.runner.conversations.create_session(Path(self.temporary.name))
        self.jobs = MemoryJobRepository(self.database)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def _expire_and_reclaim(self):
        """Age out the current lease and let a successor claim the same job."""
        async with self.database.connection() as connection:
            await connection.execute("UPDATE memory_jobs SET lease_expires_at = now() - interval '1 second'")
        await self.jobs.reap_expired()
        return await self.jobs.claim(lease_seconds=30)

    async def test_completed_run_enqueues_job_without_waiting_for_extraction(self):
        self.runner.client = SlowExtractClient(delay=2.0)
        started = monotonic()
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        elapsed = monotonic() - started
        self.assertLess(elapsed, 1.2, "the run must not wait for memory extraction")
        self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")
        job = await self.jobs.for_run(result.run_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["attempts"], 0)
        events = [event["type"] for event in await self.runner.runs.events(result.run_id)]
        self.assertIn("memory_job_queued", events)
        self.assertNotIn("memory_extraction_started", events)

    async def test_extraction_runs_after_the_answer_and_records_candidates(self):
        self.runner.client = SlowExtractClient(delay=0)
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        self.assertEqual((await self.jobs.for_run(result.run_id))["status"], "queued")
        await self.runner.memory_worker.drain(timeout=10)
        job = await self.jobs.for_run(result.run_id)
        self.assertEqual((job["status"], job["attempts"]), ("completed", 0))
        events = [event["type"] for event in await self.runner.runs.events(result.run_id)]
        self.assertIn("memory_extraction_started", events)
        self.assertLess(events.index("run_completed"), events.index("memory_extraction_started"))

    async def test_expired_lease_is_reaped_and_costs_one_attempt(self):
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        first = await self.jobs.claim(lease_seconds=60)
        self.assertIsNotNone(first)
        self.assertEqual(first.run_id, result.run_id)
        # A worker that died leaves the row 'running'; the lease is the only
        # signal that the work may be retaken, so a live lease blocks re-claiming.
        self.assertIsNone(await self.jobs.claim(lease_seconds=30))
        self.assertEqual(await self.jobs.reap_expired(), [])
        async with self.database.connection() as connection:
            await connection.execute("UPDATE memory_jobs SET lease_expires_at = now() - interval '1 second'")
        reaped = await self.jobs.reap_expired()
        self.assertEqual([(row["id"], row["status"], row["attempts"]) for row in reaped],
                         [(first.id, "queued", 1)])
        second = await self.jobs.claim(lease_seconds=30)
        self.assertEqual((second.id, second.attempts), (first.id, 1))

    async def test_repeated_lease_expiry_stops_at_max_attempts(self):
        """A job that keeps killing its process must not retry forever."""
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        async with self.database.connection() as connection:
            await connection.execute(
                "INSERT INTO memory_extractions (id, run_id, status, prompt_version, request) "
                "VALUES (%s, %s, 'running', 'test', %s)", (uuid4(), result.run_id, Jsonb({})))
        for attempt in (1, 2):
            await self.jobs.claim(lease_seconds=30)
            async with self.database.connection() as connection:
                await connection.execute("UPDATE memory_jobs SET lease_expires_at = now() - interval '1 second'")
            reaped = await self.jobs.reap_expired()
            self.assertEqual([(row["status"], row["attempts"]) for row in reaped], [("queued", attempt)])
        await self.jobs.claim(lease_seconds=30)
        async with self.database.connection() as connection:
            await connection.execute("UPDATE memory_jobs SET lease_expires_at = now() - interval '1 second'")
        reaped = await self.jobs.reap_expired()
        self.assertEqual([(row["status"], row["attempts"]) for row in reaped], [("failed", 3)])
        self.assertEqual((await self.jobs.for_run(result.run_id))["status"], "failed")
        self.assertIsNone(await self.jobs.claim(lease_seconds=30))
        # The dead attempt's audit row must not stay 'running' forever.
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT status, error FROM memory_extractions WHERE run_id = %s", (result.run_id,))
            self.assertEqual(dict(await cursor.fetchone()), {"status": "failed", "error": "LeaseExpired"})

    async def test_worker_reports_a_crash_exhausted_job(self):
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        await self.jobs.claim(lease_seconds=30)
        async with self.database.connection() as connection:
            await connection.execute(
                "UPDATE memory_jobs SET attempts = max_attempts - 1, lease_expires_at = now() - interval '1 second'")
        await self.runner.memory_worker.run_once()
        self.assertEqual((await self.jobs.for_run(result.run_id))["status"], "failed")
        events = await self.runner.runs.events(result.run_id)
        reported = [event for event in events if event["type"] == "memory_job_failed"]
        self.assertEqual([event["payload"]["reason"] for event in reported], ["LeaseExpired"])
        # The answer is untouched by a failed extraction.
        self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")

    async def test_stale_attempt_cannot_write_after_reclaim(self):
        """A lease says when work may be retaken; only the token says who owns it."""
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        first = await self.jobs.claim(lease_seconds=30)
        second = await self._expire_and_reclaim()
        self.assertEqual(second.id, first.id)
        self.assertNotEqual(first.claim_token, second.claim_token)

        with self.assertRaises(MemoryJobClaimLost):
            await self.jobs.complete(first.id, first.claim_token)
        with self.assertRaises(MemoryJobClaimLost):
            await self.jobs.requeue(first.id, first.claim_token)
        with self.assertRaises(MemoryJobClaimLost):
            await self.jobs.fail(first.id, first.claim_token, error="RuntimeError")

        job = await self.jobs.for_run(result.run_id)
        self.assertEqual((job["status"], job["attempts"], job["claim_token"]),
                         ("running", 1, second.claim_token))
        await self.jobs.complete(second.id, second.claim_token)
        self.assertEqual((await self.jobs.for_run(result.run_id))["status"], "completed")

    async def test_stale_completion_rolls_back_its_candidates(self):
        """Candidates and the terminal state share a transaction, so a lost
        claim must discard the candidates rather than let them commit alone."""
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        first = await self.jobs.claim(lease_seconds=30)
        await self._expire_and_reclaim()
        with self.assertRaises(MemoryJobClaimLost):
            async with self.database.transaction() as connection:
                await connection.execute(
                    "INSERT INTO memory_extractions (id, run_id, status, prompt_version, request) "
                    "VALUES (%s, %s, 'running', 'stale-attempt', %s)", (uuid4(), result.run_id, Jsonb({})))
                await self.jobs.complete(first.id, first.claim_token)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS total FROM memory_extractions WHERE run_id = %s AND prompt_version = 'stale-attempt'",
                (result.run_id,))
            self.assertEqual((await cursor.fetchone())["total"], 0)

    async def test_worker_discards_an_attempt_whose_lease_was_reaped(self):
        """The successor owns the job; the slow attempt must stay silent."""
        self.runner.client = SlowExtractClient(delay=0.5)
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        attempt = asyncio.create_task(self.runner.memory_worker.run_once())
        await asyncio.sleep(0.15)  # claimed and extracting, but not yet committing
        successor = await self._expire_and_reclaim()
        self.assertIsNotNone(successor)
        await attempt
        job = await self.jobs.for_run(result.run_id)
        self.assertEqual((job["status"], job["claim_token"]), ("running", successor.claim_token))
        events = [event["type"] for event in await self.runner.runs.events(result.run_id)]
        self.assertNotIn("memory_job_failed", events, "a live successor must not be reported as failed")
        await self.jobs.complete(successor.id, successor.claim_token)
        self.assertEqual((await self.jobs.for_run(result.run_id))["status"], "completed")

    async def test_retry_stops_at_max_attempts(self):
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        statuses = []
        for _ in range(3):
            claimed = await self.jobs.claim(lease_seconds=30)
            statuses.append(await self.jobs.fail(claimed.id, claimed.claim_token,
                                                 error="RuntimeError", retry_delay=0))
        self.assertEqual(statuses, ["queued", "queued", "failed"])
        self.assertEqual((await self.jobs.for_run(result.run_id))["attempts"], 3)
        self.assertIsNone(await self.jobs.claim(lease_seconds=30))

    async def test_enqueue_is_idempotent_per_run(self):
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        async with self.database.transaction() as connection:
            duplicate = await self.jobs.enqueue(
                connection, session_id=self.session, run_id=result.run_id,
                source_message_id=uuid4(), prompt_version="test")
        self.assertIsNone(duplicate)
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT count(*) AS total FROM memory_jobs")
            self.assertEqual((await cursor.fetchone())["total"], 1)

    async def test_disabled_or_short_input_does_not_queue_work(self):
        self.runner.memory_service.policy = MemoryPolicy(enabled=False)
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        self.assertIsNone(await self.jobs.for_run(result.run_id))
        self.runner.memory_service.policy = MemoryPolicy()
        short = await self.runner.run(self.session, "继续")
        self.assertIsNone(await self.jobs.for_run(short.run_id))

    async def test_worker_failure_is_recorded_without_touching_the_run(self):
        class FailingExtractClient(SlowExtractClient):
            async def stream(self, request):
                if request.purpose == "memory":
                    raise RuntimeError("provider down")
                async for event in super().stream(request):
                    yield event
        self.runner.client = FailingExtractClient()
        result = await self.runner.run(self.session, "我偏好中文，希望回答尽量简洁，并保留参考链接。")
        await self.runner.memory_worker.drain(timeout=10)
        self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")
        job = await self.jobs.for_run(result.run_id)
        self.assertEqual((job["status"], job["attempts"], job["last_error"]), ("queued", 1, "RuntimeError"))
        # A retry waiting out its backoff is not drained, so the CLI must report
        # it as still open instead of assuming the queue is empty.
        self.assertEqual(await self.jobs.unfinished(), 1)
        events = [event["type"] for event in await self.runner.runs.events(result.run_id)]
        self.assertIn("memory_extraction_failed", events)
        self.assertIn("memory_job_failed", events)
        self.assertEqual((await MemoryRepository(self.database).list(
            (await self.runner.conversations.get_session(self.session)).workspace_id, status="all"))["items"], [])
