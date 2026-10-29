"""Recovery facts: confirmed side effects survive a failed run, raw messages do not."""

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
from chagent.domain.codec import message_from_dict
from chagent.domain.messages import AssistantMessage, ToolResultMessage
from chagent.domain.recovery import HEADER, MAX_FACTS, RecoveryFact, build_recovery_note
from chagent.models.events import ErrorEvent
from chagent.models.fake import FakeModelClient
from chagent.storage.database import Database
from chagent.storage.migrate import migrate


class FailsAfterTool(FakeModelClient):
    """Completes one tool batch, then fails the following model request."""

    async def stream(self, request):
        if isinstance(request.messages[-1], ToolResultMessage):
            yield ErrorEvent(reason="error", error=AssistantMessage(
                [], "fake", "local-test", "fake-echo", 1, "error",
                error_message="Provider rejected request",
            ))
            return
        async for event in super().stream(request):
            yield event


class RecoveryNoteTests(unittest.TestCase):
    def fact(self, target: str, *, confirmed: bool = True, run_id=None) -> RecoveryFact:
        return RecoveryFact(
            run_id=run_id or uuid4(), run_status="failed", tool_name="write_file",
            target=target, confirmed=confirmed, detail="sha256 abcdef12",
        )

    def test_empty_facts_produce_no_note(self):
        self.assertIsNone(build_recovery_note([]))

    def test_latest_attempt_per_target_replaces_older(self):
        first, second = self.fact("a.txt"), self.fact("a.txt")
        note = build_recovery_note([first, second])
        self.assertEqual(note.fact_count, 1)
        self.assertIn(str(second.run_id)[:8], note.text)
        self.assertNotIn(str(first.run_id)[:8], note.text)

    def test_unknown_outcome_is_not_worded_as_success(self):
        note = build_recovery_note([self.fact("a.txt", confirmed=False)])
        self.assertIn("unknown; no result was committed", note.text)
        self.assertNotIn("— committed", note.text)

    def test_facts_are_capped_but_keep_the_newest(self):
        facts = [self.fact(f"file{index}.txt") for index in range(MAX_FACTS + 4)]
        note = build_recovery_note(facts)
        self.assertEqual(note.fact_count, MAX_FACTS)
        self.assertIn("file15.txt", note.text)
        self.assertNotIn("file0.txt", note.text)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class RecoveryProjectionTests(unittest.IsolatedAsyncioTestCase):
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
        self.runner = create_demo_runner(self.database)
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.session = await self.runner.conversations.create_session(Path(self.workspace.name))

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def fail_with_write(self, path: str = "notes.txt", content: str = "hello\n", *, overwrite: bool = False):
        arguments = {"path": path, "content": content}
        if overwrite:
            arguments["overwrite"] = True
        self.runner.client = FailsAfterTool("write_file", arguments)
        with self.assertRaises(RunFailedError) as raised:
            await self.runner.run(self.session, "写文件")
        return raised.exception.run_id

    async def fail_with_tool(self, name: str, arguments: dict):
        self.runner.client = FailsAfterTool(name, arguments)
        with self.assertRaises(RunFailedError) as raised:
            await self.runner.run(self.session, "做点什么")
        return raised.exception.run_id

    async def first_request(self, run_id) -> dict:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT request, context_snapshot FROM model_calls "
                "WHERE run_id = %s AND iteration = 0", (run_id,),
            )
            return await cursor.fetchone()

    async def test_confirmed_write_reaches_the_next_run_without_replaying_history(self):
        failed = await self.fail_with_write()
        self.assertTrue((Path(self.workspace.name) / "notes.txt").exists())

        result = await create_demo_runner(self.database).run(self.session, "继续")
        row = await self.first_request(result.run_id)
        system = message_from_dict(row["request"]["messages"][0]).text()
        self.assertIn(HEADER, system)
        self.assertIn("write_file notes.txt", system)
        self.assertIn("committed (sha256", system)
        self.assertIn(str(failed)[:8], system)
        self.assertEqual(row["context_snapshot"]["recovery_fact_count"], 1)
        self.assertEqual(row["context_snapshot"]["recovery_source_runs"], [str(failed)])

        # The failed run's messages stay out of the model history: only the new
        # input is present, and the tool batch is never replayed.
        roles = [item["role"] for item in row["request"]["messages"]]
        self.assertEqual(roles, ["system", "user"])
        self.assertEqual(row["request"]["messages"][1]["content"], "继续")
        events = [event["type"] for event in await self.runner.runs.events(result.run_id)]
        self.assertIn("recovery_projected", events)

    async def test_a_patch_applied_by_a_failed_run_is_reported(self):
        """apply_patch changes files too, and its target only exists in the result."""
        patch = "\n".join(["--- a/notes.py", "+++ b/notes.py", "@@ -0,0 +1,1 @@", "+VALUE = 1"])
        await self.fail_with_tool("apply_patch", {"patch": patch})
        self.assertTrue((Path(self.workspace.name) / "notes.py").exists())

        result = await create_demo_runner(self.database).run(self.session, "继续")
        row = await self.first_request(result.run_id)
        system = message_from_dict(row["request"]["messages"][0]).text()
        self.assertIn(HEADER, system)
        self.assertIn("apply_patch notes.py — committed", system)
        self.assertEqual(row["context_snapshot"]["recovery_fact_count"], 1)

    async def test_unknown_outcome_is_reported_as_unknown(self):
        failed = await self.fail_with_write()
        async with self.database.transaction() as connection:
            await connection.execute(
                "UPDATE tool_calls SET status = 'unknown' WHERE run_id = %s", (failed,))

        result = await create_demo_runner(self.database).run(self.session, "继续")
        system = message_from_dict((await self.first_request(result.run_id))["request"]["messages"][0]).text()
        self.assertIn("write_file notes.txt — unknown; no result was committed", system)

    async def test_facts_stop_after_a_completed_run(self):
        await self.fail_with_write()
        await create_demo_runner(self.database).run(self.session, "第一轮正常结束")
        result = await create_demo_runner(self.database).run(self.session, "第二轮")
        row = await self.first_request(result.run_id)
        self.assertNotIn(HEADER, message_from_dict(row["request"]["messages"][0]).text())
        self.assertEqual(row["context_snapshot"]["recovery_fact_count"], 0)

    async def test_repeated_writes_report_only_the_latest_attempt(self):
        first = await self.fail_with_write(content="one\n")
        second = await self.fail_with_write(content="two\n", overwrite=True)
        result = await create_demo_runner(self.database).run(self.session, "继续")
        row = await self.first_request(result.run_id)
        system = message_from_dict(row["request"]["messages"][0]).text()
        self.assertEqual(system.count("write_file notes.txt"), 1)
        self.assertIn(str(second)[:8], system)
        self.assertNotIn(str(first)[:8], system)
        self.assertEqual(row["context_snapshot"]["recovery_fact_count"], 1)

    async def test_read_only_calls_are_not_recovery_facts(self):
        await self.fail_with_tool("list_dir", {"path": "."})
        result = await create_demo_runner(self.database).run(self.session, "继续")
        row = await self.first_request(result.run_id)
        self.assertNotIn(HEADER, message_from_dict(row["request"]["messages"][0]).text())
        self.assertEqual(row["context_snapshot"]["recovery_fact_count"], 0)
