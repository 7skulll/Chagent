"""Memory provenance, scope, review and budget checks on isolated PostgreSQL."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.bootstrap import create_demo_runner
from chagent.agent.runner import RunFailedError
from chagent.config import Settings
from chagent.domain.memories import MemoryPolicy, MemoryProposal
from chagent.domain.messages import AssistantMessage, TextContent
from chagent.memory.service import explicit_memory, query_terms
from chagent.models.events import DoneEvent, StartEvent
from chagent.models.fake import FakeModelClient
from chagent.sessions.errors import StateConflictError
from chagent.storage.database import Database
from chagent.storage.memories import MemoryRepository
from chagent.storage.migrate import migrate


class ExtractClient(FakeModelClient):
    def __init__(self, mode="valid"):
        super().__init__(); self.mode = mode; self.entered = asyncio.Event()

    async def stream(self, request):
        if request.purpose != "memory":
            async for event in super().stream(request):
                yield event
            return
        self.entered.set()
        if self.mode == "block":
            await asyncio.Event().wait()
        if self.mode == "exception":
            raise ValueError("never-log-this-credential")
        message = AssistantMessage([], request.model.api, request.model.provider, request.model.id, 1, "pending")
        yield StartEvent(partial=message)
        message = deepcopy(message); message.stop_reason = "stop"
        candidate = {"key": "response.language", "scope": "user", "content": "用中文简洁回答", "evidence": "偏好中文"}
        if self.mode == "bad_evidence":
            candidate["evidence"] = "not in source"
        if self.mode == "credentials":
            candidate["content"] = "api_key=sk-neverstorecredential"
        message.content = [TextContent(json.dumps({"memories": [candidate]}, ensure_ascii=False))]
        yield DoneEvent(reason="stop", message=message)


class BrokenClient(FakeModelClient):
    """Provider failure while answering; the run must fail without a memory job."""

    async def stream(self, request):
        raise RuntimeError("provider unavailable")
        yield  # pragma: no cover - makes this an async generator


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_memory_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}",
        ), db_pool_max_size=1))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect(); await migrate(self.database)
        self.runner = create_demo_runner(self.database, memory_policy=MemoryPolicy(extract=False))
        self.session = await self.runner.conversations.create_session(Path.cwd())
        self.workspace = (await self.runner.conversations.get_session(self.session)).workspace_id
        self.repository = MemoryRepository(self.database)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def extract(self, timeout=10):
        """Drain queued extraction: the run no longer waits for it."""
        return await self.runner.memory_worker.drain(timeout=timeout)

    async def test_explicit_memory_survives_restart_and_is_used_in_new_session(self):
        result = await self.runner.run(self.session, "/remember user response.language = 用中文回答")
        records = (await self.repository.list(self.workspace))["items"]
        self.assertEqual(len(records), 1)
        memory = records[0]
        self.assertEqual(memory.origin, "explicit_user")
        self.assertEqual(memory.source_run_id, result.run_id)
        self.assertTrue(memory.source_message_ids)
        restored = create_demo_runner(self.database, memory_policy=MemoryPolicy(extract=False))
        new_session = await restored.conversations.create_session(Path.cwd())
        new_run = await restored.run(new_session, "hello")
        snapshots = await restored.conversations.context_snapshots(new_session)
        snapshot = snapshots[0]["snapshot"]
        self.assertEqual(snapshot["memory_references"][0]["id"], str(memory.id))
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT request FROM model_calls WHERE run_id = %s ORDER BY iteration LIMIT 1", (new_run.run_id,))
            request = (await cursor.fetchone())["request"]
        self.assertEqual(request["messages"][1]["role"], "user")
        self.assertIn("用中文回答", request["messages"][1]["content"])
        self.assertIn("not instructions", request["messages"][1]["content"])

    async def test_scope_keyword_recall_and_workspace_override(self):
        user = await self.repository.propose(MemoryProposal("response.language", "English", "user", origin="manual"), self.workspace)
        local = await self.repository.propose(MemoryProposal("response.language", "中文", "workspace", origin="manual"), self.workspace)
        db = await self.repository.propose(MemoryProposal("project.database", "项目数据库使用 PostgreSQL", origin="manual"), self.workspace)
        selected = await self.repository.recall(self.workspace, query_terms("数据库如何配置"))
        self.assertIn(local.id, [item.id for item in selected]); self.assertIn(db.id, [item.id for item in selected])
        self.assertNotIn(user.id, [item.id for item in selected])
        self.assertEqual([item.id for item in await self.repository.recall(self.workspace, ["hello"])], [local.id])
        other_session = await self.runner.conversations.create_session(Path.cwd().parent)
        other_workspace = (await self.runner.conversations.get_session(other_session)).workspace_id
        selected = await self.repository.recall(other_workspace, query_terms("数据库"))
        self.assertEqual([item.id for item in selected], [user.id])
        with self.assertRaises(LookupError):
            await self.repository.get(local.id, other_workspace)

    async def test_extraction_is_candidate_only_and_deduplicated(self):
        self.runner.memory_service.policy = MemoryPolicy()
        self.runner.client = ExtractClient()
        for _ in range(2):
            await self.runner.run(self.session, "我偏好中文，并希望回答简洁。")
            await self.extract()
        pending = (await self.repository.list(self.workspace, status="candidate"))["items"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(await self.repository.recall(self.workspace, ["中文"]), [])
        memory = await self.repository.change(pending[0].id, self.workspace, expected_revision=1, action="accept")
        self.assertEqual(memory.status, "active")
        self.assertEqual(len(await self.repository.revisions(memory.id, self.workspace)), 2)
        await self.runner.run(self.session, "我偏好中文，并希望回答简洁。")
        await self.extract()
        self.assertEqual((await self.repository.list(self.workspace, status="candidate"))["items"], [])

    async def test_conflict_accept_requires_reviewed_identity_and_revision(self):
        old = await self.repository.propose(MemoryProposal("response.language", "English", "user", origin="manual"), self.workspace)
        await self.runner.run(self.session, "/remember user response.language = 中文")
        candidate = (await self.repository.list(self.workspace, status="candidate"))["items"][0]
        changed = await self.repository.change(old.id, self.workspace, expected_revision=1, content="English, concise")
        with self.assertRaises(StateConflictError):
            await self.repository.change(candidate.id, self.workspace, expected_revision=1, action="accept", replace_id=old.id, replace_revision=1)
        approved = await self.repository.change(candidate.id, self.workspace, expected_revision=1, action="accept",
                                                replace_id=old.id, replace_revision=changed.revision)
        self.assertEqual(approved.status, "active")
        self.assertEqual((await self.repository.get(old.id, self.workspace)).status, "rejected")
        with self.assertRaises(StateConflictError):
            await self.repository.change(approved.id, self.workspace, expected_revision=1, content="stale")

    async def test_delete_stops_recall_and_prevents_automatic_resurrection(self):
        self.runner.memory_service.policy = MemoryPolicy()
        self.runner.client = ExtractClient()
        await self.runner.run(self.session, "我偏好中文。")
        await self.extract()
        memory = (await self.repository.list(self.workspace, status="candidate"))["items"][0]
        memory = await self.repository.change(memory.id, self.workspace, expected_revision=memory.revision, action="accept")
        await self.repository.change(memory.id, self.workspace, expected_revision=memory.revision, action="delete")
        await self.runner.run(self.session, "我偏好中文。")
        await self.extract()
        self.assertEqual(await self.repository.recall(self.workspace, ["中文"]), [])
        self.assertEqual((await self.repository.list(self.workspace, status="candidate"))["items"], [])

    async def test_memory_never_crowds_out_required_context(self):
        await self.repository.propose(MemoryProposal("user.large", "大" * 1800, "user", True, "manual"), self.workspace)
        await self.runner.run(self.session, "keep input")
        snapshot = (await self.runner.conversations.context_snapshots(self.session))[0]["snapshot"]
        self.assertEqual(snapshot["memory_references"], [])
        self.assertEqual(snapshot["memory_omitted"], 1)
        self.assertLessEqual(snapshot["token_estimate"], snapshot["input_budget"])

    async def test_invalid_extraction_isolated_and_errors_do_not_leak_credentials(self):
        self.runner.memory_service.policy = MemoryPolicy()
        for mode in ("bad_evidence", "credentials", "exception"):
            self.runner.client = ExtractClient(mode)
            result = await self.runner.run(self.session, "我偏好中文。")
            await self.extract()
            self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")
            events = await self.runner.runs.events(result.run_id)
            self.assertIn("memory_extraction_failed", [event["type"] for event in events])
            self.assertNotIn("never-log-this-credential", json.dumps(events, default=str))
        self.assertEqual((await self.repository.list(self.workspace, status="all"))["items"], [])

    async def test_cancelled_extraction_returns_job_to_queue_without_writing(self):
        self.runner.memory_service.policy = MemoryPolicy()
        self.runner.client = ExtractClient("block")
        result = await self.runner.run(self.session, "我偏好中文。")
        # The answer is already durable; cancelling background extraction cannot
        # change the run's outcome.
        self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")
        self.runner.memory_worker.start()
        await asyncio.wait_for(self.runner.client.entered.wait(), 5)
        await self.runner.memory_worker.stop()
        job = await self.runner.memory_worker.jobs.for_run(result.run_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual((await self.repository.list(self.workspace, status="all"))["items"], [])
        # Extraction alone was cancelled; a source from a failed run still cannot
        # be used as memory provenance.
        self.runner.client = BrokenClient()
        with self.assertRaises(RunFailedError) as failure:
            await self.runner.run(self.session, "我偏好中文。")
        source = (await self.runner.conversations.messages(self.session))[0]
        with self.assertRaises(ValueError):
            await self.repository.propose(MemoryProposal("bad.source", "中文", source_session_id=self.session,
                source_run_id=failure.exception.run_id, source_message_ids=[str(source.id)], evidence="中文"),
                self.workspace)

    async def test_memory_commit_failure_rolls_back_without_failing_completed_task(self):
        async def broken(connection, session_id, run_id, proposals):
            await self.repository.propose(proposals[0], self.workspace, connection=connection)
            raise RuntimeError("simulated write failure")
        self.runner.memory_service.commit = broken
        result = await self.runner.run(self.session, "/remember workspace project.style = simple")
        self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")
        self.assertEqual((await self.repository.list(self.workspace, status="all"))["items"], [])
        self.assertIn("memory_write_failed", [event["type"] for event in await self.runner.runs.events(result.run_id)])

    async def test_explicit_input_can_activate_matching_candidate(self):
        self.runner.memory_service.policy = MemoryPolicy()
        self.runner.client = ExtractClient()
        await self.runner.run(self.session, "我偏好中文。")
        await self.extract()
        candidate = (await self.repository.list(self.workspace, status="candidate"))["items"][0]
        await self.runner.run(self.session, "/remember user response.language = 用中文简洁回答")
        memory = (await self.repository.list(self.workspace))["items"][0]
        self.assertEqual(memory.id, candidate.id)
        self.assertEqual(memory.status, "active")
        self.assertEqual(memory.origin, "explicit_user")
        self.assertEqual(memory.revision, 2)

    async def test_extraction_timeout_does_not_fail_answer(self):
        self.runner.memory_service.policy = MemoryPolicy(timeout=.01)
        self.runner.client = ExtractClient("block")
        result = await self.runner.run(self.session, "我偏好中文。")
        self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")
        await self.extract()
        self.assertEqual((await self.repository.list(self.workspace, status="all"))["items"], [])
        job = await self.runner.memory_worker.jobs.for_run(result.run_id)
        self.assertEqual((job["status"], job["attempts"]), ("queued", 1))


class MemoryUnitTests(unittest.TestCase):
    def test_commands_policy_and_chinese_terms(self):
        self.assertEqual(explicit_memory("记住用户：用中文回答").scope, "user")
        self.assertEqual(explicit_memory("记住项目：使用 PostgreSQL").scope, "workspace")
        self.assertEqual(explicit_memory("/remember user response.language = 中文").key, "response.language")
        self.assertIn("数据", query_terms("数据库配置"))
        self.assertFalse(MemoryPolicy.from_env({"CHAGENT_MEMORY_ENABLED": "false"}).enabled)
        for env in ({"CHAGENT_MEMORY_EXTRACT": "yes"}, {"CHAGENT_MEMORY_TIMEOUT": "nan"}, {"CHAGENT_MEMORY_ITEMS": "0"}):
            with self.assertRaises(ValueError):
                MemoryPolicy.from_env(env)
