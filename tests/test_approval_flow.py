"""End-to-end approval behaviour: refusal converges, grants are per call, waits are free."""

import asyncio
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.storage.approvals import ApprovalRepository
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.tools.policy import ApprovalPolicy
from chagent.tools.context import ToolContext
from chagent.domain.messages import ToolCall, UserMessage
from psycopg.types.json import Jsonb


async def no_approver(request):
    """A non-interactive command line: nobody can answer."""
    return None


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class ApprovalFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_approval_flow_" + uuid4().hex
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

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def runner(self, *, tool_name="exec_command", arguments=None, policy=None, remote=True,
               decider=None, allowlist=""):
        # An approval gate is only meaningful where commands exist, so the
        # settings that own the policy describe the container shape it belongs
        # to; the harness itself is built without a boundary below.
        configured = policy or ApprovalPolicy.from_settings(Settings(
            execution_profile="containerized", container_declared=True,
            allow_host_commands=True, sandbox_network=True,
            approval="untrusted", approval_allowlist=allowlist))
        runner = create_demo_runner(
            self.database, tool_name=tool_name, tool_arguments=arguments,
            allow_host_commands=True, sandbox=None, approval_policy=configured,
            approval_timeout=10.0, approval_decider=decider, approval_remote=remote)
        runner.model.context_window = 16_384
        return runner

    def approvals(self, runner):
        return ApprovalRepository(self.database, runner.runs)

    async def wait_run_id(self, identities, timeout=10.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not identities:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("run never started")
            await asyncio.sleep(0.01)
        return identities[0]

    async def wait_pending(self, runner, run_id, timeout=10.0):
        repository = self.approvals(runner)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            pending = await repository.pending(run_id)
            if pending:
                return pending[0]
            await asyncio.sleep(0.02)
        raise AssertionError("approval was never requested")

    async def tool_row(self, run_id):
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT status, result FROM tool_calls WHERE run_id = %s", (run_id,))
            return await cursor.fetchone()

    async def test_denied_command_is_not_executed_and_the_run_converges(self):
        marker = self.workspace / "denied.txt"
        runner = self.runner(arguments={"cmd": f"printf x > {marker.name}"},
                             remote=False, decider=no_approver)
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "执行命令")
        # Convergence, not turn_limit or a crash.
        self.assertEqual(result.end_reason, "model_stop")
        answer = result.message.content[0].text
        self.assertIn("was not executed", answer)
        self.assertIn("policy decision", answer)
        self.assertFalse(marker.exists())
        row = await self.tool_row(result.run_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["result"]["details"]["approval"], "denied")
        self.assertFalse(row["result"]["details"]["execution_started"])
        kinds = [event["type"] for event in await runner.runs.events(result.run_id)]
        self.assertIn("approval_requested", kinds)
        self.assertIn("approval_decided", kinds)
        self.assertNotIn("tool_started", kinds)
        self.assertEqual((await self.approvals(runner).pending(result.run_id)), [])

    async def test_approving_one_command_does_not_approve_another(self):
        first = self.workspace / "first.txt"
        asked = []

        async def approve(request):
            asked.append(request.subject)
            return True

        runner = self.runner(arguments={"cmd": f"printf a > {first.name}"}, remote=False, decider=approve)
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "第一条命令")
        self.assertEqual(result.end_reason, "model_stop")
        self.assertTrue(first.exists())
        self.assertEqual(asked, [f"printf a > {first.name}"])

        # A different command is a different question, even in the same session.
        asked.clear()
        second = self.runner(arguments={"cmd": "printf b > second.txt"}, remote=False, decider=approve)
        await second.run(session, "第二条命令")
        self.assertEqual(asked, ["printf b > second.txt"])
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT subject FROM approval_requests ORDER BY created_at")
            subjects = [row["subject"] for row in await cursor.fetchall()]
        self.assertEqual(subjects, [f"printf a > {first.name}", "printf b > second.txt"])

    async def test_waiting_for_approval_does_not_block_another_run(self):
        marker = self.workspace / "later.txt"
        waiting = self.runner(arguments={"cmd": f"printf A > {marker.name}"})
        waiting_session = await waiting.conversations.create_session(self.workspace)
        (self.workspace / "notes.txt").write_text("hello", encoding="utf-8")
        reader = self.runner(tool_name="read_file", arguments={"path": "notes.txt"})
        reader_session = await reader.conversations.create_session(self.workspace)

        identities = []
        task = asyncio.create_task(waiting.run(waiting_session, "等等看", on_started=identities.append))
        request = await self.wait_pending(waiting, await self.wait_run_id(identities))
        self.assertFalse(marker.exists())

        # The other session completes while the first one is still waiting.
        other = await asyncio.wait_for(reader.run(reader_session, "读一下"), timeout=20)
        self.assertEqual(other.end_reason, "model_stop")
        self.assertFalse(task.done())
        self.assertFalse(marker.exists())

        await self.approvals(waiting).decide(request.id, approve=True)
        result = await asyncio.wait_for(task, timeout=20)
        self.assertEqual(result.end_reason, "model_stop")
        self.assertTrue(marker.exists())

    async def test_the_wait_does_not_consume_the_run_budget(self):
        marker = self.workspace / "budget.txt"
        runner = self.runner(arguments={"cmd": f"printf B > {marker.name}"})
        runner.timeout = 1.0
        session = await runner.conversations.create_session(self.workspace)
        identities = []
        task = asyncio.create_task(runner.run(session, "等批准", on_started=identities.append))
        request = await self.wait_pending(runner, await self.wait_run_id(identities))
        await asyncio.sleep(1.5)   # longer than the whole run budget
        self.assertFalse(task.done())
        await self.approvals(runner).decide(request.id, approve=True)
        result = await asyncio.wait_for(task, timeout=20)
        self.assertEqual(result.end_reason, "model_stop")
        self.assertTrue(marker.exists())

    async def test_wall_limit_ends_a_run_waiting_for_approval(self):
        marker = self.workspace / "wall-limit.txt"
        runner = self.runner(arguments={"cmd": f"printf x > {marker.name}"})
        runner.timeout = 1.0
        runner.wall_timeout = 0.3
        session = await runner.conversations.create_session(self.workspace)
        from chagent.agent.runner import RunFailedError
        with self.assertRaises(RunFailedError) as failure:
            await asyncio.wait_for(runner.run(session, "等待审批"), timeout=5)
        self.assertEqual(failure.exception.end_reason, "run_timeout")
        self.assertFalse(marker.exists())

    async def test_an_allowlisted_command_runs_without_a_question(self):
        policy = ApprovalPolicy.from_settings(Settings(
            execution_profile="containerized", container_declared=True,
            allow_host_commands=True, sandbox_network=True,
            approval="untrusted", approval_allowlist="printf"))
        runner = self.runner(arguments={"cmd": "printf allow-marker"},
                             policy=policy, remote=False, decider=no_approver)
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "允许的命令")
        self.assertEqual(result.end_reason, "model_stop")
        self.assertIn("allow-marker", result.message.content[0].text)
        kinds = [event["type"] for event in await runner.runs.events(result.run_id)]
        self.assertNotIn("approval_requested", kinds)
        self.assertEqual(await self.approvals(runner).pending(result.run_id), [])

    async def test_a_question_with_no_page_waiting_is_denied_quickly(self):
        """Nobody is looking: the command must not wait out the full TTL."""
        marker = self.workspace / "unwatched.txt"
        runner = self.runner(arguments={"cmd": f"printf x > {marker.name}"})
        broker = runner.scheduler.approvals
        broker.audience = lambda run_id: False
        broker.grace = 0.3
        session = await runner.conversations.create_session(self.workspace)
        started = asyncio.get_running_loop().time()
        result = await asyncio.wait_for(runner.run(session, "没人看着"), timeout=15)
        self.assertLess(asyncio.get_running_loop().time() - started, 12)
        self.assertEqual(result.end_reason, "model_stop")
        answer = result.message.content[0].text
        self.assertIn("No browser page was waiting", answer)
        self.assertIn("Do not retry", answer)
        self.assertFalse(marker.exists())
        row = await self.tool_row(result.run_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["result"]["details"]["approval"], "unwatched")
        self.assertFalse(row["result"]["details"]["execution_started"])
        self.assertEqual(await self.approvals(runner).pending(result.run_id), [])

    async def test_a_watching_page_keeps_the_absolute_deadline(self):
        """Someone is looking, so the question waits for them, not for a timer."""
        runner = self.runner(arguments={"cmd": "printf watched"})
        broker = runner.scheduler.approvals
        broker.audience = lambda run_id: True
        broker.grace = 0.3
        broker.timeout = 1.0
        session = await runner.conversations.create_session(self.workspace)
        result = await asyncio.wait_for(runner.run(session, "有人看着"), timeout=15)
        row = await self.tool_row(result.run_id)
        # Expired by its own deadline, not denied for lack of an audience.
        self.assertEqual(row["result"]["details"]["approval"], "expired")

    async def test_zero_grace_restores_the_absolute_deadline(self):
        """The rollback switch ignores audience loss and waits for the TTL."""
        runner = self.runner(arguments={"cmd": "printf disabled"})
        broker = runner.scheduler.approvals
        broker.audience = lambda run_id: False
        broker.grace = 0
        broker.timeout = 0.5
        session = await runner.conversations.create_session(self.workspace)
        result = await asyncio.wait_for(runner.run(session, "关闭在场宽限"), timeout=15)
        row = await self.tool_row(result.run_id)
        self.assertEqual(row["result"]["details"]["approval"], "expired")

    async def test_a_reconnecting_page_clears_the_unwatched_clock(self):
        """A refresh drops the stream for a moment; that must not kill the run."""
        marker = self.workspace / "reconnected.txt"
        runner = self.runner(arguments={"cmd": f"printf y > {marker.name}"})
        broker = runner.scheduler.approvals
        calls = {"n": 0}

        def audience(run_id):
            calls["n"] += 1
            # Two absent polls, two present polls, then absent again. Each
            # absence is shorter than the grace; only a reset on reconnect lets
            # the second one survive too.
            return ((calls["n"] - 1) // 2) % 2 == 1

        broker.audience = audience
        broker.grace = 0.4
        # The audience is consulted once per tick, so this test sets the cadence
        # it counts on instead of inheriting the product's recovery interval.
        broker.poll = 0.1
        session = await runner.conversations.create_session(self.workspace)
        identities = []
        task = asyncio.create_task(runner.run(session, "刷新一下", on_started=identities.append))
        request = await self.wait_pending(runner, await self.wait_run_id(identities))
        await asyncio.sleep(1.2)          # three grace windows after the reconnect
        self.assertFalse(task.done())
        self.assertEqual([item.id for item in await self.approvals(runner).pending(request.run_id)],
                         [request.id])
        await self.approvals(runner).decide(request.id, approve=True)
        result = await asyncio.wait_for(task, timeout=20)
        self.assertTrue(marker.exists())
        self.assertEqual(result.end_reason, "model_stop")

    async def test_cli_questions_ignore_the_audience(self):
        """``remote=False`` has no page to lose, so the grace never applies."""
        marker = self.workspace / "cli.txt"
        runner = self.runner(arguments={"cmd": f"printf z > {marker.name}"}, remote=False,
                             decider=no_approver)
        broker = runner.scheduler.approvals
        broker.audience = lambda run_id: False
        broker.grace = 0.1
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "命令行")
        row = await self.tool_row(result.run_id)
        # Denied because the terminal cannot answer, not because nobody watched.
        self.assertEqual(row["result"]["details"]["approval"], "denied")

    async def test_stale_grant_is_not_executed(self):
        """An approval is bound to the arguments it was issued for."""
        marker = self.workspace / "stale.txt"
        runner = self.runner()
        session = await runner.conversations.create_session(self.workspace)
        tool_id = uuid4()
        model_call = uuid4()
        run_id = await runner.runs.begin(session, __import__("chagent.domain.messages", fromlist=["UserMessage"]).UserMessage("x", 0))
        async with self.database.transaction() as connection:
            from psycopg.types.json import Jsonb
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot) "
                "VALUES (%s, %s, 0, %s, %s)", (model_call, run_id, Jsonb({}), Jsonb({})))
        from chagent.domain.messages import ToolCall
        record = (await runner.runs.queue_tool_calls(
            run_id, model_call, [ToolCall("c1", "exec_command", {"cmd": "printf x"})]))[0]
        repository = self.approvals(runner)
        request = await repository.create(
            run_id=run_id, tool_record_id=record, fingerprint="approved-for-something-else",
            tool_name="exec_command", cwd=str(self.workspace), subject="printf x", ttl=60)
        await repository.decide(request.id, approve=True)
        broker = runner.scheduler.approvals
        self.assertFalse(await broker.consume(
            request.id, run_id=run_id, tool_record_id=record, tool_name="exec_command",
            arguments={"cmd": f"printf x > {marker.name}"}, cwd=str(self.workspace)))
        self.assertFalse(marker.exists())
        await runner.runs.finish(run_id, "completed")

    async def process_attempt(self, *, timeout=10.0):
        """A live stdin consumer plus a persisted tool call for its write."""
        runner = self.runner()
        runner.scheduler.approvals.timeout = timeout
        session = await runner.conversations.create_session(self.workspace)
        run_id = await runner.runs.begin(session, UserMessage("feed stdin", 0))
        marker = self.workspace / f"stdin-{uuid4().hex}.txt"
        script = "import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
        process = await runner.processes.start(
            run_id=run_id, command="stdin consumer", cwd=self.workspace,
            argv=[sys.executable, "-c", script, str(marker)])
        model_call = uuid4()
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot) "
                "VALUES (%s, %s, 0, %s, %s)", (model_call, run_id, Jsonb({}), Jsonb({})))
        args = {"action": "write", "process_id": process.id, "chars": "你\n", "close_stdin": True}
        record = (await runner.runs.queue_tool_calls(
            run_id, model_call, [ToolCall("feed", "process", args)]))[0]
        self.addAsyncCleanup(runner.processes.terminate_run, run_id)
        return runner, run_id, process, marker, args, record

    async def test_process_stdin_approval_binds_run_call_process_bytes_and_eof(self):
        runner, run_id, process, marker, args, record = await self.process_attempt()
        broker = runner.scheduler.approvals
        waiting = asyncio.create_task(broker.request(
            run_id=run_id, tool_record_id=record, tool_name="process", arguments=args,
            cwd=str(self.workspace)))
        request = await self.wait_pending(runner, run_id)
        self.assertIn(process.id, request.subject)
        self.assertIn("4 UTF-8 bytes", request.subject)
        self.assertFalse(marker.exists())
        await self.approvals(runner).decide(request.id, approve=True)
        self.assertTrue((await waiting).approved)
        for changed in ({**args, "process_id": "other"}, {**args, "chars": "no"},
                        {**args, "close_stdin": False}):
            self.assertFalse(await broker.consume(request.id, run_id=run_id, tool_record_id=record,
                                                  tool_name="process", arguments=changed,
                                                  cwd=str(self.workspace)))
        self.assertFalse(await broker.consume(request.id, run_id=uuid4(), tool_record_id=record,
                                              tool_name="process", arguments=args, cwd=str(self.workspace)))
        self.assertFalse(await broker.consume(request.id, run_id=run_id, tool_record_id=uuid4(),
                                              tool_name="process", arguments=args, cwd=str(self.workspace)))
        self.assertTrue(await broker.consume(request.id, run_id=run_id, tool_record_id=record,
                                             tool_name="process", arguments=args, cwd=str(self.workspace)))
        self.assertFalse(await broker.consume(request.id, run_id=run_id, tool_record_id=record,
                                              tool_name="process", arguments=args, cwd=str(self.workspace)))
        tool = runner.tools.registry.get("process")
        result = await tool.execute("feed", args, context=ToolContext(self.workspace, session_id=uuid4(), run_id=run_id))
        self.assertFalse(result.is_error)
        await asyncio.wait_for(process.finished.wait(), timeout=5)
        self.assertEqual(marker.read_bytes(), "你\n".encode())
        await runner.runs.finish(run_id, "completed")

    async def test_denied_or_expired_process_input_never_reaches_stdin(self):
        for decision in (False, None):
            runner, run_id, process, marker, args, record = await self.process_attempt(
                timeout=0.3 if decision is None else 10.0)
            broker = runner.scheduler.approvals
            waiting = asyncio.create_task(broker.request(
                run_id=run_id, tool_record_id=record, tool_name="process", arguments=args,
                cwd=str(self.workspace)))
            request = await self.wait_pending(runner, run_id)
            if decision is False:
                await self.approvals(runner).decide(request.id, approve=False)
            outcome = await asyncio.wait_for(waiting, timeout=5)
            self.assertFalse(outcome.approved)
            self.assertEqual(outcome.reason, "denied" if decision is False else "expired")
            self.assertFalse(await broker.consume(request.id, run_id=run_id, tool_record_id=record,
                                                  tool_name="process", arguments=args,
                                                  cwd=str(self.workspace)))
            self.assertFalse(marker.exists())
            await runner.processes.stop(process)
            await runner.runs.finish(run_id, "completed")

    async def test_process_exit_while_waiting_cannot_receive_approved_input(self):
        runner, run_id, process, marker, args, record = await self.process_attempt()
        broker = runner.scheduler.approvals
        waiting = asyncio.create_task(broker.request(
            run_id=run_id, tool_record_id=record, tool_name="process", arguments=args,
            cwd=str(self.workspace)))
        request = await self.wait_pending(runner, run_id)
        await runner.processes.stop(process)
        await self.approvals(runner).decide(request.id, approve=True)
        self.assertTrue((await waiting).approved)
        self.assertTrue(await broker.consume(request.id, run_id=run_id, tool_record_id=record,
                                             tool_name="process", arguments=args, cwd=str(self.workspace)))
        result = await runner.tools.registry.get("process").execute(
            "feed", args, context=ToolContext(self.workspace, session_id=uuid4(), run_id=run_id))
        self.assertTrue(result.is_error)
        self.assertEqual(result.details["written"], 0)
        self.assertFalse(marker.exists())
        await runner.runs.finish(run_id, "completed")

    async def test_cancelling_wait_for_process_input_never_writes(self):
        runner, run_id, process, marker, args, record = await self.process_attempt()
        broker = runner.scheduler.approvals
        signal = asyncio.Event()
        waiting = asyncio.create_task(broker.request(
            run_id=run_id, tool_record_id=record, tool_name="process", arguments=args,
            cwd=str(self.workspace), signal=signal))
        request = await self.wait_pending(runner, run_id)
        signal.set()
        outcome = await asyncio.wait_for(waiting, timeout=5)
        self.assertEqual(outcome.reason, "cancelled")
        self.assertEqual((await self.approvals(runner).get(request.id)).status, "cancelled")
        self.assertEqual(await self.approvals(runner).decide(request.id, approve=True), "conflict")
        self.assertFalse(marker.exists())
        await runner.processes.stop(process)
        await runner.runs.finish(run_id, "completed")

    async def test_an_approved_command_says_so_in_the_result_the_model_reads(self):
        """C5: the page answered, so the result has to say it did.

        The model never sees the card and the question is settled before the
        tool runs, so the decision can only reach it inside the tool result.
        In C5 every run had a real approval, a consumed grant and the expected
        file on disk, and the final answer still said none was asked for.
        """
        marker = self.workspace / "approved.txt"
        runner = self.runner(arguments={"cmd": f"printf c5 > {marker.name}"})
        session = await runner.conversations.create_session(self.workspace)
        identities = []
        task = asyncio.create_task(runner.run(session, "批准后再执行", on_started=identities.append))
        request = await self.wait_pending(runner, await self.wait_run_id(identities))
        await self.approvals(runner).decide(request.id, approve=True, note="only this one")
        result = await asyncio.wait_for(task, timeout=20)
        self.assertEqual(result.end_reason, "model_stop")
        self.assertTrue(marker.exists())

        row = await self.tool_row(result.run_id)
        self.assertEqual(row["status"], "completed")
        details = row["result"]["details"]
        self.assertEqual(details["approval"], "approved")
        self.assertTrue(details["approval_id"])
        self.assertEqual(details["approval_note"], "only this one")
        head = row["result"]["content"][0]["text"]
        self.assertTrue(head.startswith("Approval: the user approved this exec_command call"), head)
        self.assertIn("only this one", head)
        self.assertIn("do not report that no approval was requested", head)

    async def test_a_call_that_needed_no_approval_carries_no_approval_line(self):
        # The line is a fact about one call, not a banner on every result.
        (self.workspace / "notes.txt").write_text("hello", encoding="utf-8")
        runner = self.runner(tool_name="read_file", arguments={"path": "notes.txt"})
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "读一下")
        self.assertEqual(result.end_reason, "model_stop")
        row = await self.tool_row(result.run_id)
        self.assertNotIn("Approval:", row["result"]["content"][0]["text"])
        self.assertNotIn("approval", row["result"]["details"] or {})

    async def test_the_local_prompt_marker_never_reads_as_the_users_words(self):
        # "local prompt" records who answered for audit; it is not a remark, so
        # the model must not be told the user wrote it.
        marker = self.workspace / "cli-approved.txt"

        async def approve(request):
            return True

        runner = self.runner(arguments={"cmd": f"printf cli > {marker.name}"},
                             remote=False, decider=approve)
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "批准一下")
        self.assertEqual(result.end_reason, "model_stop")
        self.assertTrue(marker.exists())
        row = await self.tool_row(result.run_id)
        head = row["result"]["content"][0]["text"]
        self.assertIn("Approval: the user approved this exec_command call", head)
        self.assertNotIn("local prompt", head)
        self.assertNotIn("approval_note", row["result"]["details"])


if __name__ == "__main__":
    unittest.main()
