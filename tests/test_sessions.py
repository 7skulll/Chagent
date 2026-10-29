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
from chagent.domain.codec import message_from_dict
from chagent.domain.messages import UserMessage
from chagent.domain.sessions import TaskState
from chagent.sessions.errors import SessionArchivedError, SessionBusyError, SessionNotFoundError, StateConflictError
from chagent.sessions.manager import SessionManager
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import ConversationRepository


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_test_" + uuid4().hex
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
        self.workspace = Path(self.temporary.name)
        self.manager = SessionManager(ConversationRepository(self.database))
        self.session = await self.manager.create(self.workspace, "first")
        self.runner = create_demo_runner(self.database)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def test_session_pages_workspace_filter_rename_and_restore(self):
        second = await self.manager.create(self.workspace, "second")
        other = self.workspace / "other"
        other.mkdir()
        await self.manager.create(other, "other workspace")
        first_page = await self.manager.list(workspace=self.workspace, limit=1)
        self.assertEqual([item.id for item in first_page.items], [second.id])
        second_page = await self.manager.list(workspace=self.workspace, limit=1, before=first_page.next_cursor)
        self.assertEqual([item.id for item in second_page.items], [self.session.id])
        self.assertIsNone(second_page.next_cursor)
        self.assertEqual((await self.manager.rename(second.id, " new title ")).title, "new title")
        await self.manager.archive(second.id)
        self.assertEqual(len((await self.manager.list(workspace=self.workspace)).items), 1)
        self.assertEqual(len((await self.manager.list(workspace=self.workspace, include_archived=True)).items), 2)
        with self.assertRaises(SessionArchivedError):
            await self.runner.run(second.id, "must not start")
        self.assertEqual((await self.manager.messages(second.id)).items, [])
        await self.manager.restore(second.id)
        await self.runner.run(second.id, "restored")

    async def test_history_pages_have_no_gaps_or_duplicate_messages(self):
        await self.runner.run(self.session.id, "one")
        await self.runner.run(self.session.id, "two")
        pages, before = [], None
        while True:
            page = await self.manager.messages(self.session.id, limit=3, before=before)
            self.assertEqual([item.sequence for item in page.items], sorted(item.sequence for item in page.items))
            pages = page.items + pages
            if page.next_before is None:
                break
            before = page.next_before
        whole = await self.runner.conversations.messages(self.session.id)
        self.assertEqual([item.id for item in pages], [item.id for item in whole])

    async def test_state_revisions_sources_and_snapshot_injection(self):
        await self.runner.run(self.session.id, "明确约束")
        source = (await self.manager.messages(self.session.id)).items[0]
        state = await self.manager.update_task_state(self.session.id, TaskState(
            goal="finish", constraints=["不要修改数据库"], pending=["verify"], source_message_ids=[str(source.id)],
        ), expected_revision=0)
        self.assertEqual(state.revision, 1)
        with self.assertRaises(StateConflictError):
            await self.manager.update_task_state(self.session.id, TaskState(goal="stale"), expected_revision=0)
        await self.runner.run(self.session.id, "继续")
        snapshots = await self.manager.contexts(self.session.id)
        self.assertEqual(snapshots[0]["snapshot"]["task_state_revision"], 1)
        self.assertIn("input_budget", snapshots[0]["snapshot"])
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT request FROM model_calls WHERE id = %s", (snapshots[0]["model_call_id"],))
            self.assertIn("不要修改数据库", message_from_dict((await cursor.fetchone())["request"]["messages"][0]).text())
        other = await self.manager.create(self.workspace, "other")
        with self.assertRaises(ValueError):
            await self.manager.update_task_state(other.id, TaskState(source_message_ids=[str(source.id)]), expected_revision=0)

    async def test_active_run_prevents_archive_or_task_mutation(self):
        run_id = await self.runner.runs.begin(self.session.id, UserMessage("active", 1))
        with self.assertRaises(SessionBusyError):
            await self.manager.archive(self.session.id)
        with self.assertRaises(SessionBusyError):
            await self.manager.update_task_state(self.session.id, TaskState(goal="new"), expected_revision=0)
        await self.runner.runs.finish(run_id, "interrupted")
        await self.manager.archive(self.session.id)

    async def test_concurrent_state_updates_allow_one_winner(self):
        results = await asyncio.gather(
            self.manager.update_task_state(self.session.id, TaskState(goal="one"), expected_revision=0),
            self.manager.update_task_state(self.session.id, TaskState(goal="two"), expected_revision=0),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(item, TaskState) for item in results), 1)
        self.assertEqual(sum(isinstance(item, StateConflictError) for item in results), 1)

    async def test_validation_and_missing_sessions(self):
        with self.assertRaises(SessionNotFoundError):
            await self.manager.get(uuid4())
        with self.assertRaises(ValueError):
            await self.manager.rename(self.session.id, "x" * 201)
        with self.assertRaises(ValueError):
            await self.manager.list(limit=0)
        with self.assertRaises(ValueError):
            await self.manager.update_task_state(self.session.id, TaskState(constraints="wrong type"), expected_revision=0)
