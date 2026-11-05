"""Host workspace identity and native approval rules."""

import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

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
from chagent.domain.messages import UserMessage
from chagent.hostexec.native import NativeHostExecTool
from chagent.tools.context import ToolContext
from chagent.tools.policy import fingerprint
from chagent.tools.policy import ApprovalPolicy
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
