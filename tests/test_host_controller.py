"""Host workspace identity and native approval rules."""

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from uuid import UUID, uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from starlette.exceptions import HTTPException

from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import ConversationRepository, RunRepository
from chagent.storage.approvals import ApprovalRepository
from chagent.domain.messages import ToolCall, UserMessage
from chagent.hostexec.native import (NativeHostExecTool, NativeHostProcessTool,
                                     mark_interrupted_native_executions)
from chagent.tools.context import ToolContext
from chagent.tools.gate import HumanGate
from chagent.tools.policy import fingerprint
from chagent.tools.policy import ApprovalPolicy
from chagent.tools.approvals import ApprovalBroker
from chagent.tools.processes import ProcessManager
from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.web.runtime import WebRuntime
from chagent.web.auth import HostAuth, COOKIE


class HostControllerPolicyTests(unittest.TestCase):
    def test_host_auth_is_outside_workspace_and_requires_secret(self):
        with tempfile.TemporaryDirectory() as raw:
            token_path = Path(raw) / "private" / "token"
            auth = HostAuth(token_path)
            self.assertEqual(token_path.stat().st_mode & 0o077, 0)
            self.assertIsNone(auth.login("wrong"))
            session = auth.login(auth.token)
            self.assertTrue(auth.valid(f"{COOKIE}={session}"))
            self.assertFalse(auth.valid(f"{COOKIE}=wrong"))
            with self.assertRaises(ValueError):
                auth.guard_workspace(Path(raw))

    def test_only_native_tool_needs_approval(self):
        policy = ApprovalPolicy("on-request")
        for name in ("exec_command", "run_tests", "site", "write_file", "process"):
            self.assertFalse(policy.requires_approval(name, {}, sandbox="managed-container"))
        self.assertTrue(policy.requires_approval("host_exec", {"command": "pwd"}, sandbox="managed-container"))
        self.assertTrue(policy.requires_approval(
            "host_process", {"action": "write", "chars": "x"}, sandbox="managed-container"))
        self.assertFalse(policy.requires_approval(
            "host_process", {"action": "read"}, sandbox="managed-container"))

    def test_different_host_roots_get_different_container_names(self):
        with tempfile.TemporaryDirectory() as raw:
            first, second = Path(raw) / "first", Path(raw) / "second"
            first.mkdir(); second.mkdir()
            self.assertNotEqual(Sandbox.container_name(first), Sandbox.container_name(second))


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class HostWorkspaceIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.first = Path(self.directory.name) / "first"
        self.second = Path(self.directory.name) / "second"
        self.first.mkdir(); self.second.mkdir()
        self.schema = "chagent_host_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        settings = Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}"),
            execution_profile="host-controller", allow_host_commands=True,
            sandbox_network=True, approval="on-request")
        self.database = Database(settings)
        await self.database.connect()
        self.addAsyncCleanup(self.database.close)
        await migrate(self.database)
        self.runtime = WebRuntime(create_demo_runner(self.database), self.first, settings, programmable=False)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def test_bindings_are_distinct_and_reject_replaced_directory(self):
        one = await self.runtime.workspace_id(self.first)
        self.runtime.open_workspace(str(self.second))
        two = await self.runtime.workspace_id(self.second)
        self.assertNotEqual(one, two)
        self.assertEqual(await self.runtime.workspace_id(self.first), one)
        self.first.rename(self.first.with_name("first-old"))
        self.first.mkdir()
        with self.assertRaises(HTTPException) as failure:
            await self.runtime.workspace_id(self.first)
        self.assertEqual(failure.exception.status_code, 409)

    async def test_binding_is_host_path_and_filesystem_identity(self):
        identity = await self.runtime.workspace_id(self.first)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT host_path,device,inode,state FROM workspace_bindings WHERE workspace_id=%s", (identity,))
            row = await cursor.fetchone()
        stat = self.first.stat()
        self.assertEqual((row["host_path"], row["device"], row["inode"], row["state"]),
                         (str(self.first.resolve()), stat.st_dev, stat.st_ino, "active"))

    async def test_native_execution_requires_single_consumed_approval(self):
        root = self.first.resolve()
        session = await ConversationRepository(self.database).create_session(root)
        runs = RunRepository(self.database)
        run = await runs.begin(session, UserMessage("test", 0))
        verified_model, host_model, verified_tool, native_tool = (uuid4() for _ in range(4))
        arguments = {"command": "python3 -c 'print(42)'", "purpose": "isolated test"}
        async with self.database.connection() as connection:
            for identity, iteration in ((verified_model, 0), (host_model, 1)):
                await connection.execute(
                    "INSERT INTO model_calls (id,run_id,iteration,status,request,context_snapshot) "
                    "VALUES (%s,%s,%s,'completed','{}'::jsonb,'{}'::jsonb)",
                    (identity, run, iteration))
            await connection.execute(
                "INSERT INTO tool_calls (id,run_id,model_call_id,call_id,name,arguments,status,result,call_index,ended_at) "
                "VALUES (%s,%s,%s,'verified','exec_command','{}'::jsonb,'completed',%s,0,now())",
                (verified_tool, run, verified_model, Jsonb({"details": {"exit_code": 0}})))
            await connection.execute(
                "INSERT INTO tool_calls (id,run_id,model_call_id,call_id,name,arguments,status,call_index) "
                "VALUES (%s,%s,%s,'native','host_exec',%s,'running',0)",
                (native_tool, run, host_model, Jsonb(arguments)))
        approvals = ApprovalRepository(self.database, runs)
        request = await approvals.create(run_id=run, tool_record_id=native_tool,
            fingerprint=fingerprint("host_exec", arguments, str(root)),
            tool_name="host_exec", cwd=str(root), subject=arguments["command"], ttl=30)
        self.assertEqual(await approvals.decide(request.id, approve=True), "approved")
        self.assertTrue(await approvals.consume(request.id, run_id=run, tool_record_id=native_tool,
                                               tool_name="host_exec", arguments=arguments, cwd=str(root)))
        tool = NativeHostExecTool(self.database)
        context = ToolContext(workspace=root, session_id=session, run_id=run)
        first = await tool.execute("native", arguments, context=context)
        self.assertFalse(first.is_error, first.content)
        self.assertIn("42", first.content[0].text)
        second = await tool.execute("native", arguments, context=context)
        self.assertTrue(second.is_error)
        self.assertTrue(second.outcome_unknown)
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT status FROM native_executions WHERE approval_id=%s", (request.id,))
            self.assertEqual((await cursor.fetchone())["status"], "completed")
            await connection.execute("UPDATE native_executions SET status='starting', ended_at=NULL WHERE approval_id=%s",
                                     (request.id,))
        self.assertEqual(await mark_interrupted_native_executions(self.database), 1)
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT status,ended_at FROM native_executions WHERE approval_id=%s",
                                              (request.id,))
            row = await cursor.fetchone()
        self.assertEqual(row["status"], "unknown")
        self.assertIsNotNone(row["ended_at"])

    async def _native_fixture(self, arguments, call_id, name="host_exec"):
        """A run with container evidence and one queued native tool call."""
        root = self.first.resolve()
        session = await ConversationRepository(self.database).create_session(root)
        runs = RunRepository(self.database)
        run = await runs.begin(session, UserMessage("test", 0))
        verified_model, host_model, verified_tool, tool_row = (uuid4() for _ in range(4))
        async with self.database.connection() as connection:
            for identity, iteration in ((verified_model, 0), (host_model, 1)):
                await connection.execute(
                    "INSERT INTO model_calls (id,run_id,iteration,status,request,context_snapshot) "
                    "VALUES (%s,%s,%s,'completed','{}'::jsonb,'{}'::jsonb)",
                    (identity, run, iteration))
            await connection.execute(
                "INSERT INTO tool_calls (id,run_id,model_call_id,call_id,name,arguments,status,result,call_index,ended_at) "
                "VALUES (%s,%s,%s,'verified','exec_command','{}'::jsonb,'completed',%s,0,now())",
                (verified_tool, run, verified_model, Jsonb({"details": {"exit_code": 0}})))
            await connection.execute(
                "INSERT INTO tool_calls (id,run_id,model_call_id,call_id,name,arguments,status,call_index) "
                "VALUES (%s,%s,%s,%s,%s,%s,'running',0)",
                (tool_row, run, host_model, call_id, name, Jsonb(arguments)))
        approvals = ApprovalRepository(self.database, runs)
        request = await approvals.create(run_id=run, tool_record_id=tool_row,
            fingerprint=fingerprint(name, arguments, str(root)),
            tool_name=name, cwd=str(root), subject=str(arguments), ttl=60)
        self.assertEqual(await approvals.decide(request.id, approve=True), "approved")
        self.assertTrue(await approvals.consume(request.id, run_id=run, tool_record_id=tool_row,
                                               tool_name=name, arguments=arguments, cwd=str(root)))
        return root, run, tool_row

    async def _granted_call(self, run, root, arguments, call_id, name, iteration):
        """Queue one more approved tool call inside an existing run."""
        model_call, tool_row = uuid4(), uuid4()
        async with self.database.connection() as connection:
            await connection.execute(
                "INSERT INTO model_calls (id,run_id,iteration,status,request,context_snapshot) "
                "VALUES (%s,%s,%s,'completed','{}'::jsonb,'{}'::jsonb)", (model_call, run, iteration))
            await connection.execute(
                "INSERT INTO tool_calls (id,run_id,model_call_id,call_id,name,arguments,status,call_index) "
                "VALUES (%s,%s,%s,%s,%s,%s,'running',0)",
                (tool_row, run, model_call, call_id, name, Jsonb(arguments)))
        approvals = ApprovalRepository(self.database, RunRepository(self.database))
        request = await approvals.create(run_id=run, tool_record_id=tool_row,
            fingerprint=fingerprint(name, arguments, str(root)),
            tool_name=name, cwd=str(root), subject=str(arguments), ttl=60)
        self.assertEqual(await approvals.decide(request.id, approve=True), "approved")
        self.assertTrue(await approvals.consume(request.id, run_id=run, tool_record_id=tool_row,
                                               tool_name=name, arguments=arguments, cwd=str(root)))
        return request.id

    async def _settled_status(self, approval_id, *, table="native_executions"):
        """Poll the audit row until it leaves its in-flight status."""
        transient = "pending" if table == "native_stdin_writes" else "running"
        for _ in range(100):
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                    f"SELECT status FROM {table} WHERE approval_id=%s", (approval_id,))
                row = await cursor.fetchone()
            if row is not None and row["status"] != transient:
                return row["status"]
            await asyncio.sleep(0.05)
        raise AssertionError("status did not settle in time")

    async def test_native_interactive_stdin_needs_its_own_approval_each_write(self):
        launch = {"command": "cat", "purpose": "interactive test", "workdir": ".",
                  "yield_time_ms": 2000}
        root, run, launch_tool = await self._native_fixture(launch, "launch")
        manager = ProcessManager(wall_timeout=60, max_per_run=4)
        exec_tool = NativeHostExecTool(self.database, manager)
        process_tool = NativeHostProcessTool(self.database, manager)
        context = ToolContext(workspace=root, session_id=None, run_id=run)
        started = await exec_tool.execute("launch", launch, context=context)
        self.assertFalse(started.is_error, started.content)
        process_id = started.details["process_id"]
        self.assertTrue(started.details["running"])
        launch_approval = UUID(str(started.details["approval_id"]))
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT status, interactive, process_id FROM native_executions "
                "WHERE run_id=%s AND process_id=%s", (run, process_id))
            execution = await cursor.fetchone()
        self.assertEqual((execution["status"], execution["interactive"]), ("running", True))
        self.assertEqual(execution["process_id"], process_id)

        # Writing without a matching consumed approval moves no bytes.
        with self.assertRaises(PermissionError):
            await process_tool.execute("raw-write",
                {"action": "write", "process_id": process_id, "chars": "no\n"}, context=context)

        # One write, one approval, bound to the exact input.
        write = {"action": "write", "process_id": process_id, "chars": "hello\n"}
        write_approval = await self._granted_call(run, root, write, "write-1", "host_process", 2)
        written = await process_tool.execute("write-1", write, context=context)
        self.assertFalse(written.is_error, written.content)
        self.assertEqual(written.details["written"], 6)
        self.assertEqual(await self._settled_status(write_approval, table="native_stdin_writes"), "written")

        read = await process_tool.execute("read-1",
            {"action": "read", "process_id": process_id, "since": 0, "wait_ms": 3000}, context=context)
        self.assertFalse(read.is_error, read.content)
        self.assertIn("hello", read.details["output"])

        # The same approval cannot deliver the input a second time.
        replay = await process_tool.execute("write-1", write, context=context)
        self.assertTrue(replay.is_error)
        self.assertTrue(replay.outcome_unknown)
        self.assertIn("already been used", replay.content[0].text)

        # EOF is its own one-time approval; cat then exits and the audit row
        # settles to its terminal status without any further tool call.
        close = {"action": "write", "process_id": process_id, "close_stdin": True}
        close_approval = await self._granted_call(run, root, close, "write-2", "host_process", 3)
        closed = await process_tool.execute("write-2", close, context=context)
        self.assertFalse(closed.is_error, closed.content)
        self.assertTrue(closed.details["closed_stdin"])
        self.assertEqual(await self._settled_status(close_approval, table="native_stdin_writes"), "written")
        self.assertEqual(await self._settled_status(launch_approval), "completed")

    async def test_native_interactive_restart_marks_live_rows_unknown(self):
        launch = {"command": "sleep 30", "purpose": "restart drill", "workdir": ".",
                  "yield_time_ms": 1000}
        root, run, launch_tool = await self._native_fixture(launch, "launch")
        manager = ProcessManager(wall_timeout=60)
        exec_tool = NativeHostExecTool(self.database, manager)
        started = await exec_tool.execute("launch", launch, context=ToolContext(workspace=root, run_id=run))
        process_id = started.details["process_id"]
        write = {"action": "write", "process_id": process_id, "chars": "x\n"}
        write_approval = await self._granted_call(run, root, write, "write-1", "host_process", 2)
        # Simulate a controller that died after claiming the write grant but
        # before settling the row: 'pending' is exactly that half-state.
        async with self.database.connection() as connection:
            await connection.execute(
                "INSERT INTO native_stdin_writes (approval_id, execution_id, run_id, process_id, "
                "chars, close_stdin, status) VALUES (%s, "
                "(SELECT approval_id FROM native_executions WHERE run_id=%s AND process_id=%s), "
                "%s, %s, %s, false, 'pending')",
                (write_approval, run, process_id, run, process_id, "x\n"))
        self.assertEqual(await mark_interrupted_native_executions(self.database), 2)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT status FROM native_executions WHERE run_id=%s AND process_id=%s", (run, process_id))
            execution = await cursor.fetchone()
            cursor = await connection.execute(
                "SELECT status FROM native_stdin_writes WHERE approval_id=%s", (write_approval,))
            stdin = await cursor.fetchone()
        self.assertEqual(execution["status"], "unknown")
        self.assertEqual(stdin["status"], "unknown")
        # And the interrupted process is actually gone, not adopted.
        await manager.terminate_run(run)

    async def test_write_gate_fails_closed_for_unknown_native_process(self):
        root = self.first.resolve()
        session = await ConversationRepository(self.database).create_session(root)
        runs = RunRepository(self.database)
        run = await runs.begin(session, UserMessage("test", 0))
        broker = ApprovalBroker(ApprovalRepository(self.database, runs), runs, timeout=5)
        gate = HumanGate(ApprovalPolicy("on-request"), broker, "managed-container")
        call = ToolCall("w1", "host_process",
                        {"action": "write", "process_id": "missing", "chars": "x\n"})
        decision = await gate.check(call, context=ToolContext(workspace=root, run_id=run),
                                    tool_record_id=uuid4(), signal=asyncio.Event(), budget=None)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.code, "unknown_process")
        # No question was asked: nothing is pending for this run.
        self.assertEqual(await ApprovalRepository(self.database, runs).pending(run), [])
