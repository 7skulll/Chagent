"""Cross-process approval wakeups over LISTEN/NOTIFY (batch 1C).

WHAT THIS PINS
    A decision made by one process must reach a waiter in another promptly, not
    on the next poll tick. The in-process bus cannot do that, which is why the
    poll could not be removed before this batch.

    Two things must stay true when notifications are imperfect:
      - a notification that never arrives costs latency, not correctness: the
        waiter re-reads its rows after its bounded timeout;
      - a listener that reconnects re-issues LISTEN and keeps delivering.

WHAT IT IS NOT
    A latency benchmark. The numbers asserted here are only "before a second",
    wide enough for a loaded machine and tight enough to fail if wakeups fell
    back to the old quarter-second poll.
"""

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import UUID, uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.config import Settings
from chagent.domain.messages import ToolCall, UserMessage
from chagent.events.notify import CHANNEL, PostgresWakeups
from chagent.storage.database import Database
from chagent.storage.migrate import migrate

LISTENER = Path("/tmp/wakeup_listener.py")
READY_TIMEOUT = 15.0
WAKE_TIMEOUT = 5.0


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class CrossProcessWakeupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_wakeup_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.dsn = os.environ["CHAGENT_TEST_DATABASE_URL"]
        self.database = Database(Settings(database_url=make_conninfo(
            self.dsn, options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def raw(self, statement, parameters=()):
        """A different connection: no in-process notify, only the database."""
        connection = await AsyncConnection.connect(self.dsn, autocommit=True)
        try:
            await connection.execute(f'SET search_path TO "{self.schema}"')
            return await connection.execute(statement, parameters)
        finally:
            await connection.close()

    def spawn_listener(self, run_id):
        process = subprocess.Popen(
            [sys.executable, str(LISTENER), self.dsn, self.schema, str(run_id), "10"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def finish():
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()

        self.addCleanup(finish)
        line = process.stdout.readline().strip()
        self.assertEqual(line, "READY", f"listener did not start: {line}")
        return process

    async def read_line(self, process, timeout=WAKE_TIMEOUT):
        return json.loads(await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout))

    async def test_another_process_receives_a_decision_promptly(self):
        """The judgement criterion: a decision made here wakes a waiter there."""
        run_id = await self.pending_approval()

        process = self.spawn_listener(run_id)
        started = time.monotonic()
        # Deciding is what rings the doorbell, from this process's transaction.
        await self.approvals().decide(await self.pending_id(run_id), approve=True)
        result = await self.read_line(process)
        elapsed = (time.monotonic() - started) * 1000
        self.assertEqual(result["payload"], str(run_id))
        self.assertLess(elapsed, 2000, f"wakeup took {elapsed:.0f} ms")
        self.assertLess(result["latency_ms"], 2000)

    async def test_a_decided_approval_wakes_a_waiter_without_polling(self):
        """A decision written by another connection wakes the in-process waiter.

        The decision goes through raw SQL, so the in-process bus is never told:
        only the LISTEN/NOTIFY path can make this fast.
        """
        run_id = await self.pending_approval()

        # Open a subscription so the listener starts, then wait on it.
        with self.database.notifications.subscribe(run_id) as subscription:
            version = subscription.version
            task = asyncio.create_task(subscription.wait(version, timeout=10.0))
            await asyncio.sleep(0.4)  # let the listener connect
            started = time.monotonic()
            await self.raw("SELECT pg_notify(%s, %s)", (CHANNEL, str(run_id)))
            await asyncio.wait_for(task, timeout=WAKE_TIMEOUT)
            elapsed = (time.monotonic() - started) * 1000
        self.assertLess(elapsed, 1000, f"waiter only woke after {elapsed:.0f} ms")
        self.assertFalse(task.cancelled())

    async def test_a_lost_notification_is_recovered_by_the_timeout(self):
        """Nobody rings the doorbell; the waiter must still look again."""
        run_id = uuid4()
        with self.database.notifications.subscribe(run_id) as subscription:
            started = time.monotonic()
            await subscription.wait(subscription.version, timeout=0.3)
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.25, "the timeout returned early")
        self.assertLess(elapsed, 2.0, "the recovery bound is not bounded")

    async def test_a_listener_that_loses_its_connection_reconnects_and_delivers(self):
        seen: list = []
        wakeups = PostgresWakeups(self.dsn, lambda run_id: seen.append(run_id))
        wakeups.start()
        try:
            await self.wait_connected(wakeups)
            run_id = uuid4()
            await self.raw("SELECT pg_notify(%s, %s)", (CHANNEL, str(run_id)))
            await self.wait_for(lambda: seen == [run_id])

            # Drop the listener's own connection: the loop must reconnect and
            # LISTEN again, otherwise the next notification is lost for good.
            await wakeups._connection.close()
            await self.wait_for(lambda: not wakeups.connected)
            await self.wait_connected(wakeups, after=1)
            second = uuid4()
            await self.raw("SELECT pg_notify(%s, %s)", (CHANNEL, str(second)))
            await self.wait_for(lambda: seen == [run_id, second])
            self.assertGreaterEqual(wakeups.reconnects, 1)
        finally:
            await wakeups.stop()

    # ---- helpers -------------------------------------------------------
    def approvals(self):
        from chagent.storage.approvals import ApprovalRepository
        from chagent.storage.repositories import RunRepository
        return ApprovalRepository(self.database, RunRepository(self.database))

    async def pending_approval(self) -> UUID:
        """One real run with one pending write approval; returns the run id.

        Written with plain SQL rather than the repositories: this test is about
        the doorbell, and building a model call through the context builder would
        add a lot of setup that has nothing to do with it.
        """
        workspace_id = uuid4()
        session_id = uuid4()
        run_id = uuid4()
        model_call_id = uuid4()
        tool_id = uuid4()
        token = uuid4().hex
        self.database.run_owners[run_id] = token
        self.addCleanup(self.database.run_owners.pop, run_id, None)
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO workspaces (id, root_path) VALUES (%s, %s)", (workspace_id, str(self.workspace)))
            await connection.execute(
                "INSERT INTO sessions (id, workspace_id, title) VALUES (%s, %s, 'wakeup')",
                (session_id, workspace_id))
            await connection.execute(
                "INSERT INTO runs (id, session_id, status, owner_token, lease_expires_at) "
                "VALUES (%s, %s, 'running', %s, clock_timestamp() + interval '30 minutes')",
                (run_id, session_id, token))
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot) "
                "VALUES (%s, %s, 1, '{}'::jsonb, '{}'::jsonb)", (model_call_id, run_id))
            await connection.execute(
                "INSERT INTO tool_calls (id, run_id, model_call_id, call_id, name, arguments, status, call_index) "
                "VALUES (%s, %s, %s, 'c1', 'write_file', '{}'::jsonb, 'queued', 0)",
                (tool_id, run_id, model_call_id))
        await self.approvals().create(
            run_id=run_id, tool_record_id=tool_id, fingerprint="f", tool_name="write_file",
            cwd=str(self.workspace), subject="overwrite a.txt", ttl=30.0)
        return run_id

    async def tool_record(self, run_id):
        async with self.database.connection() as connection:
            return (await (await connection.execute(
                "SELECT id FROM tool_calls WHERE run_id = %s ORDER BY created_at DESC LIMIT 1",
                (run_id,))).fetchone())["id"]

    async def pending_id(self, run_id):
        pending = await self.approvals().pending(run_id)
        self.assertEqual(len(pending), 1)
        return pending[0].id
    async def wait_connected(self, wakeups, after=0, timeout=10.0):
        await self.wait_for(lambda: wakeups.connected and wakeups.reconnects >= after, timeout=timeout)

    @staticmethod
    async def wait_for(predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("condition never became true")


if __name__ == "__main__":
    unittest.main()
