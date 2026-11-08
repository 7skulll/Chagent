"""Opt-in PostgreSQL checks, isolated in a fresh schema for every test."""

import asyncio
from hashlib import sha256
import os
import sys
from importlib.resources import files
import unittest
import tempfile
from pathlib import Path
from uuid import uuid4

from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from chagent.config import Settings
from chagent.storage.database import Database
from chagent.storage.migrate import migrate


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(
            os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True,
        )
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        settings = Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"],
            options=f"-c search_path={self.schema}",
        ))
        self.database = Database(settings)
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def test_migrate_is_concurrent_and_idempotent(self):
        # Counted from the bundled files: adding a migration must not fail this
        # test, only applying one twice or losing one must.
        expected = len([path for path in files("chagent.storage").joinpath("migrations").iterdir()
                        if path.name.endswith(".sql")])
        results = await asyncio.gather(migrate(self.database), migrate(self.database))
        self.assertEqual(sorted(len(result) for result in results), [0, expected])
        self.assertEqual(await migrate(self.database), [])
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT count(*) AS total FROM schema_migrations")
            self.assertEqual((await cursor.fetchone())["total"], expected)

    async def test_two_host_bindings_never_share_sessions_or_memories(self):
        """Two host directories are two workspaces: nothing leaks between them.

        The binding model replaced the old ``workspaces.host_path`` column: the
        identity of a directory is (host_path, device, inode) in
        ``workspace_bindings``, enforced unique, and sessions and memories hang
        off the workspace row that binding points at. This is the repository-level
        half of ``test_host_controller``'s end-to-end check.
        """
        from chagent.domain.memories import MemoryProposal
        from chagent.storage.memories import MemoryRepository
        from chagent.storage.repositories import ConversationRepository
        await migrate(self.database)
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first, second = Path(one), Path(two)
            repository = ConversationRepository(self.database)
            first_session = await repository.create_session(first)
            second_session = await repository.create_session(second)
            first_id = (await repository.get_session(first_session)).workspace_id
            second_id = (await repository.get_session(second_session)).workspace_id
            self.assertNotEqual(first_id, second_id)

            memories = MemoryRepository(self.database)
            first_memory = await memories.propose(
                MemoryProposal("project.marker", "first project", "workspace", origin="manual"), first_id)
            second_memory = await memories.propose(
                MemoryProposal("project.marker", "second project", "workspace", origin="manual"), second_id)
            self.assertEqual([item.id for item in (await memories.list(first_id))["items"]], [first_memory.id])
            self.assertEqual([item.id for item in (await memories.list(second_id))["items"]], [second_memory.id])
            self.assertEqual([item.id for item in await memories.recall(first_id, ["marker"])], [first_memory.id])
            with self.assertRaises(LookupError):
                await memories.get(first_memory.id, second_id)

            # Each session belongs to exactly one workspace, and the listing for
            # a directory returns only that directory's sessions.
            self.assertEqual([item.id for item in (await repository.list_sessions(workspace=first)).items],
                             [first_session])
            self.assertEqual([item.id for item in (await repository.list_sessions(workspace=second)).items],
                             [second_session])

            # A directory that was already mounted somewhere else cannot be
            # claimed twice: the second active binding for one host path is a
            # constraint violation, not a silent merge.
            async with self.database.connection() as connection:
                for workspace_id, path in ((first_id, first), (second_id, second)):
                    stat = path.stat()
                    await connection.execute(
                        "INSERT INTO workspace_bindings (workspace_id, host_path, device, inode, state) "
                        "VALUES (%s, %s, %s, %s, 'active')",
                        (workspace_id, str(path), stat.st_dev, stat.st_ino))
            stat = first.stat()
            other = uuid4()
            async with self.database.connection() as connection:
                await connection.execute("INSERT INTO workspaces (id, root_path) VALUES (%s, %s)",
                                         (other, str(first)))
                with self.assertRaises(errors.UniqueViolation):
                    await connection.execute(
                        "INSERT INTO workspace_bindings (workspace_id, host_path, device, inode, state) "
                        "VALUES (%s, %s, %s, %s, 'active')",
                        (other, str(first), stat.st_dev, stat.st_ino))

    async def test_migration_quarantines_legacy_container_binding(self):
        """A ``/workspaces`` row cannot prove its host directory: mark it rebind-only."""
        migrations = files("chagent.storage").joinpath("migrations")
        async with self.database.transaction() as connection:
            await connection.execute("CREATE TABLE schema_migrations (name text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
            for path in sorted(migrations.iterdir(), key=lambda item: item.name):
                if not path.name.endswith(".sql") or path.name >= "0020":
                    continue
                source = path.read_text(encoding="utf-8")
                await connection.execute(source)
                await connection.execute("INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)",
                                         (path.name, sha256(source.encode()).hexdigest()))
            legacy_id, legacy_session = uuid4(), uuid4()
            await connection.execute("INSERT INTO workspaces (id, root_path) VALUES (%s, '/workspaces')", (legacy_id,))
            await connection.execute("INSERT INTO sessions (id, workspace_id) VALUES (%s, %s)",
                                     (legacy_session, legacy_id))
        self.assertEqual(await migrate(self.database), [
            "0020_host_controller.sql", "0021_legacy_workspace_quarantine.sql", "0022_native_interactive.sql",
            "0023_approval_batches.sql", "0024_attempts.sql", "0025_workspace_containers.sql",
            "0026_site_workdir.sql", "0027_memory_usage_index.sql", "0028_task_history.sql"])
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT b.state, b.host_path FROM workspace_bindings b WHERE b.workspace_id = %s", (legacy_id,))
            row = await cursor.fetchone()
            # Kept for audit, but never a usable host workspace.
            self.assertEqual((row["state"], row["host_path"]), ("rebind_required", "/workspaces"))
            cursor = await connection.execute("SELECT root_path FROM workspaces WHERE id = %s", (legacy_id,))
            self.assertEqual((await cursor.fetchone())["root_path"], "/workspaces")
            # The session survives for inspection; the runtime is what refuses it.
            cursor = await connection.execute("SELECT workspace_id FROM sessions WHERE id = %s", (legacy_session,))
            self.assertEqual((await cursor.fetchone())["workspace_id"], legacy_id)

    async def test_run_owner_token_and_expired_lease_reject_writes_and_renewal(self):
        from pathlib import Path
        from chagent.domain.messages import UserMessage
        from chagent.storage.repositories import ConversationRepository, RunRepository, RunOwnershipLost
        await migrate(self.database)
        runs = RunRepository(self.database)
        session = await ConversationRepository(self.database).create_session(Path("/tmp/owner-test"))
        run = await runs.begin(session, UserMessage("test", 0))
        owner = self.database.run_owners[run]
        await runs.heartbeat(run)
        self.database.run_owners[run] = uuid4()
        with self.assertRaises(RunOwnershipLost):
            await runs.event(run, "text_delta", {"delta": "wrong owner"})
        with self.assertRaises(RunOwnershipLost):
            await runs.heartbeat(run)
        self.database.run_owners[run] = owner
        async with self.database.connection() as connection:
            await connection.execute("UPDATE runs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = %s", (run,))
        with self.assertRaises(RunOwnershipLost):
            await runs.event(run, "text_delta", {"delta": "expired"})
        with self.assertRaises(RunOwnershipLost):
            await runs.heartbeat(run)
        with self.assertRaises(RunOwnershipLost):
            await runs.finish(run, "cancelled")
        events = await runs.events(run)
        self.assertEqual([event["type"] for event in events], ["run_started"])

    async def test_expired_run_recovery_closes_calls_and_unblocks_session(self):
        from pathlib import Path
        from chagent.domain.messages import UserMessage
        from chagent.storage.repositories import ConversationRepository, RunRepository, RunOwnershipLost
        await migrate(self.database)
        runs = RunRepository(self.database)
        session = await ConversationRepository(self.database).create_session(Path("/tmp/recovery-test"))
        run = await runs.begin(session, UserMessage("test", 0))
        model, queued, active, done = uuid4(), uuid4(), uuid4(), uuid4()
        async with self.database.transaction() as connection:
            await connection.execute("INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot) VALUES (%s, %s, 0, '{}'::jsonb, '{}'::jsonb)", (model, run))
            for identity, name, status, index in ((queued, 'queued', 'queued', 0), (active, 'active', 'running', 1), (done, 'done', 'completed', 2)):
                await connection.execute("INSERT INTO tool_calls (id, run_id, model_call_id, call_id, name, arguments, status, call_index) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s)", (identity, run, model, name, name, status, index))
            await connection.execute("UPDATE runs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = %s", (run,))
        self.assertEqual(await runs.reap_expired(), [run])
        self.assertEqual(await runs.reap_expired(), [])
        row = await runs.get(run)
        self.assertEqual((row['status'], row['end_reason']), ('interrupted', 'owner_lost'))
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT call_id, status FROM tool_calls WHERE run_id = %s", (run,))
            self.assertEqual({r['call_id']: r['status'] for r in await cursor.fetchall()}, {'queued': 'cancelled', 'active': 'unknown', 'done': 'completed'})
            cursor = await connection.execute("SELECT status FROM model_calls WHERE id = %s", (model,))
            self.assertEqual((await cursor.fetchone())['status'], 'interrupted')
        self.assertEqual([e['type'] for e in await runs.events(run)], ['run_started', 'tool_interrupted', 'tool_cancelled', 'run_interrupted'])
        with self.assertRaises(RunOwnershipLost):
            await runs.heartbeat(run)
        with self.assertRaises(RunOwnershipLost):
            await runs.event(run, 'text_delta', {'delta': 'stale'})
        next_run = await runs.begin(session, UserMessage("next", 0))
        await runs.finish(next_run, 'cancelled')

    async def test_killed_owner_is_recovered_by_another_process(self):
        from chagent.storage.repositories import RunRepository
        await migrate(self.database)
        code = """
import asyncio, os
from pathlib import Path
from chagent.config import Settings
from chagent.domain.messages import UserMessage
from chagent.storage.database import Database
from chagent.storage.repositories import ConversationRepository, RunRepository
async def main():
    async with Database(Settings(database_url=os.environ['CHAGENT_CHILD_DB'])) as db:
        session = await ConversationRepository(db).create_session(Path('/tmp/process-recovery'))
        run = await RunRepository(db).begin(session, UserMessage('test', 0))
        print(str(run), flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
"""
        env = dict(os.environ, CHAGENT_CHILD_DB=make_conninfo(
            os.environ['CHAGENT_TEST_DATABASE_URL'], options=f'-c search_path={self.schema}'))
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', code, env=env,
                                                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            from uuid import UUID
            run = UUID((await asyncio.wait_for(child.stdout.readline(), 5)).decode().strip())
            async with self.database.connection() as connection:
                await connection.execute("UPDATE runs SET lease_expires_at = clock_timestamp() + interval '0.2 seconds' WHERE id = %s", (run,))
            child.kill()
            await child.wait()
            await asyncio.sleep(.3)
            runs = RunRepository(self.database)
            self.assertEqual(await runs.reap_expired(), [run])
            self.assertEqual((await runs.get(run))['status'], 'interrupted')
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    async def test_recovery_skips_healthy_and_locked_renewal(self):
        from pathlib import Path
        from chagent.domain.messages import UserMessage
        from chagent.storage.repositories import ConversationRepository, RunRepository
        await migrate(self.database)
        runs = RunRepository(self.database)
        session = await ConversationRepository(self.database).create_session(Path("/tmp/healthy-test"))
        run = await runs.begin(session, UserMessage("test", 0))
        self.assertEqual(await runs.reap_expired(), [])
        async with self.database.transaction() as connection:
            await connection.execute("UPDATE runs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = %s", (run,))
            self.assertEqual(await runs.reap_expired(), [])
            await connection.execute("UPDATE runs SET lease_expires_at = clock_timestamp() + interval '30 seconds' WHERE id = %s", (run,))
        self.assertEqual(await runs.reap_expired(), [])
        await runs.finish(run, 'cancelled')

    async def test_reject_modified_applied_migration(self):
        await migrate(self.database)
        async with self.database.connection() as connection:
            await connection.execute("UPDATE schema_migrations SET checksum = 'modified'")
        with self.assertRaisesRegex(RuntimeError, "modified"):
            await migrate(self.database)

    async def test_end_reason_migration_preserves_unknown_legacy_meaning(self):
        async with self.database.connection() as connection:
            await connection.execute("CREATE TABLE runs (status text NOT NULL)")
            await connection.execute("INSERT INTO runs(status) VALUES ('queued'), ('running'), ('completed'), ('failed'), ('cancelled'), ('interrupted')")
            await connection.execute(files("chagent.storage").joinpath("migrations/0007_run_end_reasons.sql").read_text())
            cursor = await connection.execute("SELECT status, end_reason FROM runs")
            rows = await cursor.fetchall()
            for row in rows:
                self.assertEqual(row["end_reason"], None if row["status"] in {"queued", "running"} else "legacy_terminal")
            with self.assertRaises(errors.CheckViolation):
                await connection.execute("UPDATE runs SET end_reason = 'model_stop' WHERE status = 'failed'")
            with self.assertRaises(errors.CheckViolation):
                await connection.execute("UPDATE runs SET status = 'completed' WHERE status = 'running'")

    async def test_tool_scheduling_migration_preserves_legacy_order_and_constraints(self):
        async with self.database.connection() as connection:
            await connection.execute("""
                CREATE TABLE tool_calls (
                    id integer PRIMARY KEY, model_call_id integer NOT NULL, created_at integer NOT NULL,
                    status text CONSTRAINT tool_calls_status_check CHECK(status IN ('running', 'completed')));
                INSERT INTO tool_calls VALUES (9, 1, 20, 'completed'), (2, 1, 10, 'running'), (3, 2, 5, 'completed');
            """)
            await connection.execute(files("chagent.storage").joinpath("migrations/0008_tool_scheduling.sql").read_text())
            cursor = await connection.execute("SELECT id, call_index FROM tool_calls ORDER BY id")
            self.assertEqual([(row["id"], row["call_index"]) for row in await cursor.fetchall()], [(2, 0), (3, 0), (9, 1)])
            await connection.execute("UPDATE tool_calls SET status = 'queued' WHERE id = 2")
            await connection.execute("UPDATE tool_calls SET status = 'cancelled' WHERE id = 2")
            with self.assertRaises(errors.UniqueViolation):
                await connection.execute("UPDATE tool_calls SET call_index = 0 WHERE id = 9")
            with self.assertRaises(errors.CheckViolation):
                await connection.execute("UPDATE tool_calls SET call_index = -1 WHERE id = 9")
            with self.assertRaises(errors.NotNullViolation):
                await connection.execute("UPDATE tool_calls SET call_index = NULL WHERE id = 9")

    async def test_transaction_commit_rollback_and_cancel(self):
        await migrate(self.database)
        committed, rolled_back, cancelled = uuid4(), uuid4(), uuid4()
        async with self.database.transaction() as connection:
            await connection.execute("INSERT INTO workspaces (id, root_path) VALUES (%s, %s)", (committed, "/committed"))
        for identity, exception in ((rolled_back, ValueError), (cancelled, asyncio.CancelledError)):
            with self.assertRaises(exception):
                async with self.database.transaction() as connection:
                    await connection.execute("INSERT INTO workspaces (id, root_path) VALUES (%s, %s)", (identity, str(identity)))
                    raise exception()
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT id FROM workspaces")
            self.assertEqual([row["id"] for row in await cursor.fetchall()], [committed])

    async def test_messages_round_trip_and_session_constraints(self):
        await migrate(self.database)
        workspace, first_session, second_session, run = (uuid4() for _ in range(4))
        payload = {"role": "assistant", "content": [{"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "说明.md"}}]}
        async with self.database.transaction() as connection:
            await connection.execute("INSERT INTO workspaces (id, root_path) VALUES (%s, %s)", (workspace, "/project"))
            for session in (first_session, second_session):
                await connection.execute("INSERT INTO sessions (id, workspace_id) VALUES (%s, %s)", (session, workspace))
            await connection.execute("INSERT INTO runs (id, session_id) VALUES (%s, %s)", (run, first_session))
            await connection.execute("INSERT INTO messages (id, session_id, run_id, role, payload) VALUES (%s, %s, %s, %s, %s)", (uuid4(), first_session, run, "assistant", Jsonb(payload)))
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT payload FROM messages")
            self.assertEqual((await cursor.fetchone())["payload"], payload)
            with self.assertRaises(errors.ForeignKeyViolation):
                await connection.execute("INSERT INTO messages (id, session_id, run_id, role, payload) VALUES (%s, %s, %s, %s, %s)", (uuid4(), second_session, run, "user", Jsonb({})))
            with self.assertRaises(errors.UniqueViolation):
                await connection.execute("INSERT INTO runs (id, session_id) VALUES (%s, %s)", (uuid4(), first_session))
            await connection.execute("UPDATE runs SET status = 'completed', end_reason = 'model_stop' WHERE id = %s", (run,))
            await connection.execute("INSERT INTO runs (id, session_id) VALUES (%s, %s)", (uuid4(), first_session))
