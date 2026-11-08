"""B1：改动过文件却没验证过的运行，得到且只得到一次补验提示。"""

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import tempfile
from time import time_ns
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.verification import NUDGE, mutations, verified
from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.domain.messages import AssistantMessage, TextContent, ToolCall, ToolResultMessage
from chagent.domain.runs import StoredMessage
from chagent.models.base import Model
from chagent.models.events import DoneEvent, StartEvent
from chagent.storage.database import Database
from chagent.storage.migrate import migrate


def _stored(tool_name: str, *, is_error: bool = False, details=None, sequence: int = 1) -> StoredMessage:
    return StoredMessage(
        uuid4(), sequence,
        ToolResultMessage(tool_call_id="call_" + tool_name, tool_name=tool_name,
                          content=[TextContent("ok")], is_error=is_error,
                          timestamp=0, details=details),
        uuid4(), None,
    )


class VerificationEvidenceTests(unittest.TestCase):
    """The two questions are answered from the committed transcript only."""

    def test_write_then_nothing_is_unverified(self):
        history = [_stored("write_file"), _stored("read_file", sequence=2)]
        self.assertEqual(mutations(history), 1)
        self.assertFalse(verified(history))

    def test_failed_write_is_not_a_mutation(self):
        self.assertEqual(mutations([_stored("write_file", is_error=True)]), 0)

    def test_run_tests_success_is_evidence(self):
        history = [_stored("write_file"), _stored("run_tests", sequence=2)]
        self.assertTrue(verified(history))

    def test_failed_tests_are_not_evidence(self):
        history = [_stored("write_file"), _stored("run_tests", is_error=True, sequence=2)]
        self.assertTrue(mutations(history) == 1)
        self.assertFalse(verified(history))

    def test_site_counts_only_a_probe_answering_200(self):
        started = [_stored("write_file"), _stored("site", details={"url": "u"}, sequence=2)]
        probed = [_stored("write_file"), _stored("site", details={"status_code": 200}, sequence=2)]
        failed = [_stored("write_file"), _stored("site", details={"status_code": 500}, sequence=2)]
        self.assertFalse(verified(started))
        self.assertTrue(verified(probed))
        self.assertFalse(verified(failed))

    def test_reading_a_lot_is_not_a_mutation(self):
        history = [_stored(name, sequence=index + 1)
                   for index, name in enumerate(("read_file", "grep", "glob", "list_dir", "web_search"))]
        self.assertEqual(mutations(history), 0)

    def test_nudge_says_it_is_not_a_request(self):
        self.assertIn("not a new request", NUDGE)
        self.assertIn("did not verify", NUDGE)


class VerificationSettingTests(unittest.TestCase):
    def test_default_is_remind(self):
        self.assertEqual(Settings.from_env({}).require_verification, "remind")

    def test_off_is_accepted(self):
        self.assertEqual(Settings.from_env({"CHAGENT_REQUIRE_VERIFICATION": "off"}).require_verification, "off")

    def test_unknown_value_is_refused(self):
        with self.assertRaisesRegex(ValueError, "CHAGENT_REQUIRE_VERIFICATION"):
            Settings.from_env({"CHAGENT_REQUIRE_VERIFICATION": "always"})


class _Scripted:
    """Writes one file, then claims to be done, whatever it is asked."""

    def __init__(self):
        self.steps = 0

    async def stream(self, request):
        self.steps += 1
        partial = AssistantMessage(
            content=[], api=request.model.api, provider=request.model.provider,
            model=request.model.id, timestamp=time_ns() // 1_000_000, stop_reason="pending",
        )
        yield StartEvent(partial=partial)
        final = deepcopy(partial)
        if self.steps == 1:
            final.content = [ToolCall(id="call_write", name="write_file",
                                      arguments={"path": "site.txt", "content": "built"})]
            final.stop_reason = "toolUse"
            yield DoneEvent(reason="toolUse", message=final)
            return
        final.content = [TextContent(f"第 {self.steps} 次声称完成")]
        final.stop_reason = "stop"
        yield DoneEvent(reason="stop", message=final)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class VerificationGateRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_verify_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self._drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    async def _drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def _model(self):
        return Model(id="scripted", provider="local-test", api="fake", context_window=16_384, max_tokens=512)

    async def _run(self, **kwargs):
        client = _Scripted()
        runner = create_runner(self.database, self._model(), client, **kwargs)
        session = await runner.conversations.create_session(Path(self.directory.name))
        return runner, await runner.run(session, "做一个站点")

    async def test_remind_adds_exactly_one_verification_turn(self):
        runner, result = await self._run(require_verification="remind")
        # 写文件 → 声称完成 → 补验提示 → 再次声称完成
        self.assertEqual(result.end_reason, "model_stop")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT request FROM model_calls WHERE run_id = %s ORDER BY iteration", (result.run_id,))
            requests = [row["request"] for row in await cursor.fetchall()]
        self.assertEqual(len(requests), 3)
        sent = [json_text(message) for message in requests[-1]["messages"]]
        self.assertTrue(any("not a new request" in text and "did not verify" in text for text in sent),
                        "补验提示没有进入模型请求")
        events = await runner.runs.events(result.run_id)
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds.count("verification_required"), 1)
        self.assertEqual(kinds.count("verification_missing"), 1)

    async def test_off_keeps_the_old_completion_condition(self):
        runner, result = await self._run(require_verification="off")
        self.assertEqual(result.end_reason, "model_stop")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS n FROM model_calls WHERE run_id = %s", (result.run_id,))
            self.assertEqual((await cursor.fetchone())["n"], 2)
        kinds = [event["type"] for event in await runner.runs.events(result.run_id)]
        self.assertNotIn("verification_required", kinds)
        self.assertNotIn("verification_missing", kinds)


def json_text(message) -> str:
    """Flatten one request message to searchable text."""

    def walk(value):
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return " ".join(walk(item) for item in value.values())
        if isinstance(value, list):
            return " ".join(walk(item) for item in value)
        return ""

    return walk(message)


if __name__ == "__main__":
    unittest.main()
