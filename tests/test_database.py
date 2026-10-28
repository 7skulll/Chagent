"""Opt-in PostgreSQL checks, isolated in a fresh schema for every test."""

import asyncio
import os
from importlib.resources import files
import unittest
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
        results = await asyncio.gather(migrate(self.database), migrate(self.database))
        self.assertEqual(sorted(len(result) for result in results), [0, 9])
        self.assertEqual(await migrate(self.database), [])
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT count(*) AS total FROM schema_migrations")
            self.assertEqual((await cursor.fetchone())["total"], 9)

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
