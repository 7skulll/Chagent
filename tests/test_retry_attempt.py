"""Attempts and retry classification (batch 1B).

WHAT THIS PINS
    - Every way a call can end without a result has a name, and the name says
      whether running it again is a reasonable next step.
    - A retry is a new attempt: new tool record, new approval question, new
      attempt number. The earlier ticket cannot be spent on it.
    - An unknown outcome is spoken into the result the model reads, because a
      blind retry there can duplicate a side effect that already happened.

WHAT IT IS NOT
    A client-behaviour test: whether a model *chooses* to retry is not asserted.
    What is asserted is that the harness never does it on its own and that the
    vocabulary it hands the model is unambiguous.
"""

import asyncio
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.runner import RunFailedError
from chagent.agent.turn_note import last_request_message
from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.domain.memories import MemoryPolicy
from chagent.domain.messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from chagent.models.base import Model
from chagent.models.events import DoneEvent, StartEvent
from chagent.models.fake import FakeModelClient
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import ConversationRepository, RunRepository
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.domain.messages import TextContent
from chagent.tools.echo import EchoTool
from chagent.tools.executor import ToolExecutor
from chagent.tools.policy import ApprovalPolicy
from chagent.tools.registry import ToolRegistry
from chagent.tools.retry import FAILURES, of as failure_of


class RetryClient(FakeModelClient):
    """Asks once, then asks the same thing again after the result."""

    def __init__(self, tool_name: str, arguments: dict, *, attempts: int = 2):
        super().__init__()
        self.tool_name, self.arguments, self.attempts = tool_name, arguments, attempts
        self.asks = 0

    async def stream(self, request):
        last = last_request_message(request.messages)
        if self.asks < self.attempts and isinstance(last, (UserMessage, ToolResultMessage)):
            self.asks += 1
            message = AssistantMessage([], request.model.api, request.model.provider, request.model.id, 0, "pending")
            yield StartEvent(message)
            final = deepcopy(message)
            final.content = [ToolCall(f"try-{self.asks}", self.tool_name, deepcopy(self.arguments))]
            final.stop_reason = "toolUse"
            yield DoneEvent("toolUse", final)
            return
        if isinstance(last, ToolResultMessage):
            message = AssistantMessage([], request.model.api, request.model.provider, request.model.id, 0, "pending")
            yield StartEvent(message)
            final = deepcopy(message)
            final.content = [TextContent("stopped after the tools")]
            final.stop_reason = "stop"
            yield DoneEvent("stop", final)
            return
        async for event in super().stream(request):
            yield event


class BoomTool(AgentTool):
    """Reports an ordinary failure: the tool ran and said what went wrong."""

    def __init__(self):
        super().__init__(name="boom", label="Fail", description="Always fails after running.",
                         parameters={"type": "object", "properties": {}})

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context=None):
        return AgentToolResult(content=[TextContent("ValueError: bad input")], is_error=True)


class SlowTool(AgentTool):
    """Never returns: the executor turns this into an unknown outcome."""

    def __init__(self):
        super().__init__(name="slow", label="Hang", description="Never returns.",
                         parameters={"type": "object", "properties": {}})

    def execution_timeout(self, params):
        return 0.2

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context=None):
        await asyncio.Event().wait()


class ClassificationTests(unittest.TestCase):
    def test_every_failure_kind_is_named_and_explained(self):
        for code in ("denied", "unwatched", "expired", "cancelled", "unavailable", "stale",
                     "execution_failed", "outcome_unknown"):
            with self.subTest(code=code):
                failure = failure_of(code)
                self.assertIsNotNone(failure, f"{code} has no classification")
                self.assertTrue(failure.advice.strip(), f"{code} has no advice")
                self.assertEqual(failure.code, code)

    def test_only_the_documented_kinds_are_retryable(self):
        retryable = {code for code, failure in FAILURES.items() if failure.retryable}
        self.assertEqual(retryable, {"expired", "execution_failed"},
                         "retryable set changed; update the table in tools/retry.py and its docs")

    def test_an_unknown_kind_is_not_silently_retryable(self):
        self.assertIsNone(failure_of("something_new"))


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class AttemptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_retry_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        self.runs = RunRepository(self.database)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def runner(self, client, *, approvals=False):
        policy = ApprovalPolicy.from_settings(Settings(
            execution_profile="containerized", container_declared=True,
            allow_host_commands=True, sandbox_network=True,
            approval="untrusted" if approvals else "never"))
        runner = create_runner(
            self.database,
            Model(id="retry-fake", provider="local-test", api="fake", context_window=16_384, max_tokens=512),
            client, allow_host_commands=True, sandbox=None, timeout=30, max_turns=8,
            memory_policy=MemoryPolicy(enabled=False), approval_policy=policy,
            approval_timeout=5.0, approval_remote=False, approval_decider=allow_everything,
            code_mode=True, single_face=True, allow_workspace_writes=True, versioned_files=True,
        )
        return runner

    async def attempts_of(self, run_id):
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT name, attempt, status FROM tool_calls WHERE run_id = %s ORDER BY created_at",
                (run_id,))
            return [dict(row) for row in await cursor.fetchall()]

    async def test_the_same_invocation_twice_is_attempt_two(self):
        runner = self.runner(RetryClient("echo", {"text": "same"}))
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "回显两次")
        rows = await self.attempts_of(result.run_id)
        self.assertEqual([row["attempt"] for row in rows], [1, 2])
        self.assertTrue(all(row["name"] == "echo" for row in rows))

    async def test_a_different_invocation_is_a_first_attempt(self):
        runner = self.runner(RetryClient("echo", {"text": "same"}))
        session = await runner.conversations.create_session(self.workspace)
        first = await runner.run(session, "第一次")
        rows = await self.attempts_of(first.run_id)
        self.assertEqual([row["attempt"] for row in rows], [1, 2])

        other = self.runner(RetryClient("echo", {"text": "different"}))
        second = await other.run(session, "换参数")
        rows = await self.attempts_of(second.run_id)
        self.assertEqual([row["attempt"] for row in rows], [1, 2],
                         "a new run numbers its own attempts from 1")

    async def test_a_retry_after_a_refusal_asks_a_new_question(self):
        target = self.workspace / "gated.txt"
        target.write_text("old", encoding="utf-8")
        digest = hashlib.sha256(b"old").hexdigest()
        client = RetryClient("write_file", {"path": "gated.txt", "content": "new",
                                            "overwrite": True, "expected_sha256": digest})
        answers = [False, True]
        runner = self.runner(client, approvals=True)
        runner.scheduler.approvals.decider = None      # the test answers, not a stub
        runner.scheduler.approvals.remote = True
        session = await runner.conversations.create_session(self.workspace)

        task = asyncio.create_task(runner.run(session, "改文件"))
        for expected in answers:
            pending = await self.wait_pending(session)
            await self.approvals().decide(pending[0].id, approve=expected)
        await asyncio.wait_for(task, timeout=20)

        rows = await self.approval_rows()
        self.assertEqual(len(rows), 2, "the retry must ask its own question")
        self.assertEqual([row["status"] for row in rows], ["denied", "approved"])
        self.assertIsNone(rows[0]["consumed_at"], "the refused ticket stays unspent")
        self.assertIsNotNone(rows[1]["consumed_at"])
        self.assertNotEqual(rows[0]["tool_record_id"], rows[1]["tool_record_id"])
        self.assertEqual(target.read_text(encoding="utf-8"), "new")

    async def test_an_old_ticket_cannot_be_spent_on_the_new_attempt(self):
        target = self.workspace / "gated.txt"
        target.write_text("old", encoding="utf-8")
        digest = hashlib.sha256(b"old").hexdigest()
        arguments = {"path": "gated.txt", "content": "new", "overwrite": True, "expected_sha256": digest}
        client = RetryClient("write_file", arguments)
        runner = self.runner(client, approvals=True)
        runner.scheduler.approvals.decider = None
        runner.scheduler.approvals.remote = True
        session = await runner.conversations.create_session(self.workspace)

        task = asyncio.create_task(runner.run(session, "改文件"))
        first = await self.wait_pending(session)
        await self.approvals().decide(first[0].id, approve=True)
        second = await self.wait_pending(session, exclude={first[0].id})
        # Spending the first ticket against the second attempt's tool record fails.
        from chagent.tools.policy import fingerprint
        spent = await self.approvals().consume(
            first[0].id, run_id=first[0].run_id, tool_record_id=second[0].tool_record_id,
            fingerprint=fingerprint("write_file", arguments, str(self.workspace)))
        self.assertFalse(spent)
        await self.approvals().decide(second[0].id, approve=True)
        await asyncio.wait_for(task, timeout=20)
        rows = await self.approval_rows()
        self.assertEqual([row["status"] for row in rows], ["approved", "approved"])
        self.assertTrue(all(row["consumed_at"] is not None for row in rows),
                        "each attempt consumed its own ticket")

    async def test_an_execution_failure_is_named_and_marked_retryable(self):
        runner = self.runner(FakeModelClient("boom", {}))
        registry = runner.tools.registry
        registry.register(BoomTool())
        runner.tools = ToolExecutor(registry)
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "跑一下")
        details = await self.result_details(result.run_id)
        self.assertEqual(details["failure_class"], "execution_failed")
        self.assertTrue(details["retryable"])
        self.assertEqual(details["attempt"], 1)

    async def test_an_unknown_outcome_is_spoken_and_not_retryable(self):
        runner = self.runner(FakeModelClient("slow", {}))
        registry = runner.tools.registry
        registry.register(SlowTool())
        # The executor takes max(default, requested); the default has to be small
        # for a hang test, or the run budget expires before the tool does.
        runner.tools = ToolExecutor(registry, timeout=1)
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "卡住")
        details = await self.result_details(result.run_id)
        self.assertEqual(details["failure_class"], "outcome_unknown")
        self.assertFalse(details["retryable"])
        text = await self.result_text(result.run_id)
        self.assertTrue(text.startswith("Result unknown:"), text[:80])
        self.assertIn("do not retry this call", text.lower())

    async def test_the_harness_never_retries_an_unknown_outcome_by_itself(self):
        runner = self.runner(FakeModelClient("slow", {}))
        registry = runner.tools.registry
        registry.register(SlowTool())
        # The executor takes max(default, requested); the default has to be small
        # for a hang test, or the run budget expires before the tool does.
        runner.tools = ToolExecutor(registry, timeout=1)
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "卡住")
        events = await self.runs.events(result.run_id)
        self.assertEqual(sum(event["type"] == "tool_started" for event in events), 1,
                         "the harness re-executed a call whose outcome was unknown")

    async def test_a_denied_approval_refusal_names_its_class(self):
        target = self.workspace / "gated.txt"
        target.write_text("old", encoding="utf-8")
        digest = hashlib.sha256(b"old").hexdigest()
        client = RetryClient("write_file", {"path": "gated.txt", "content": "new",
                                            "overwrite": True, "expected_sha256": digest}, attempts=1)
        runner = self.runner(client, approvals=True)

        async def deny(request):
            return False

        runner.scheduler.approvals.decider = deny
        runner.scheduler.approvals.remote = False
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "改文件")
        details = await self.result_details(result.run_id)
        self.assertEqual(details["failure_class"], "denied")
        self.assertFalse(details["retryable"])
        self.assertEqual(details["attempt"], 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "old")

    # ---- helpers -------------------------------------------------------
    def approvals(self):
        from chagent.storage.approvals import ApprovalRepository
        return ApprovalRepository(self.database, self.runs)

    async def approval_rows(self):
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT status, consumed_at, tool_record_id FROM approval_requests ORDER BY created_at")
            return [dict(row) for row in await cursor.fetchall()]

    async def wait_pending(self, session, exclude=frozenset(), timeout=10.0):
        repository = self.approvals()
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            run_id = await self.latest_run(session)
            pending = [row for row in await repository.pending(run_id) if row.id not in exclude]
            if pending:
                return pending
            await asyncio.sleep(0.02)
        raise AssertionError("approval was never requested")

    async def latest_run(self, session):
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT id FROM runs WHERE session_id = %s ORDER BY created_at DESC LIMIT 1", (session,))
            row = await cursor.fetchone()
        return row["id"] if row else None

    async def result_details(self, run_id):
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT result FROM tool_calls WHERE run_id = %s "
                "AND status IN ('completed', 'failed', 'unknown') "
                "ORDER BY created_at LIMIT 1", (run_id,))
            row = await cursor.fetchone()
        return (row["result"] or {}).get("details") or {}

    async def result_text(self, run_id):
        messages = await ConversationRepository(self.database).messages(
            await self.session_of(run_id))
        for item in messages:
            if isinstance(item.message, ToolResultMessage):
                return item.message.content[0].text
        return ""

    async def session_of(self, run_id):
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT session_id FROM runs WHERE id = %s", (run_id,))
            return (await cursor.fetchone())["session_id"]


async def allow_everything(request):
    return True


if __name__ == "__main__":
    unittest.main()
