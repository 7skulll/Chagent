"""Session processes: yield, cursor reads, stdin, stop, ownership and reclamation.

The behaviours worth a test are the ones a wrong implementation still passes by
accident: a process that dies with its invocation, output read twice, an id from
another run, a child that outlives the kill. Each test names the failure it
would catch.
"""

import asyncio
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.domain.messages import ToolCall
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.command import ExecCommandTool
from chagent.tools.context import ToolContext
from chagent.tools.executor import ToolExecutor
from chagent.tools.processes import ProcessLimitError, ProcessManager, ProcessTool


def python(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


@unittest.skipUnless(os.name == "posix", "POSIX process sessions")
class ProcessSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def context(self, run_id=None):
        return ToolContext(self.root, session_id=uuid4(), run_id=run_id or uuid4())

    async def start(self, manager, context, command, **params):
        tool = ExecCommandTool(processes=manager)
        return await tool.execute("start", {"cmd": command, **params}, context=context)

    def process_tool(self, manager):
        return ProcessTool(manager)

    async def test_yield_returns_a_handle_and_the_process_keeps_running(self):
        manager = ProcessManager(wall_timeout=20)
        context = self.context()
        result = await self.start(manager, context, "echo first; sleep 1; echo second",
                                  yield_time_ms=100)
        self.assertFalse(result.is_error)
        self.assertTrue(result.details["running"])
        self.assertIsNone(result.details["exit_code"])
        self.assertIn("first", result.details["output"])
        self.assertNotIn("second", result.details["output"])
        process_id = result.details["process_id"]

        # The whole point of the batch: the child outlived the tool call.
        follow = await self.process_tool(manager).execute(
            "read", {"action": "read", "process_id": process_id, "since": result.details["cursor"],
                     "wait_ms": 5000}, context=context)
        self.assertIn("second", follow.content[0].text)
        self.assertEqual(follow.details["exit_code"], 0)
        self.assertFalse(follow.details["running"])
        await manager.terminate_run(context.run_id)

    async def test_cursor_reads_never_repeat_output(self):
        manager = ProcessManager(wall_timeout=20)
        context = self.context()
        result = await self.start(manager, context, "printf aaa; sleep 0.4; printf bbb",
                                  yield_time_ms=100)
        tool, process_id = self.process_tool(manager), result.details["process_id"]
        first = await tool.execute("r1", {"action": "read", "process_id": process_id, "since": 0},
                                   context=context)
        self.assertEqual(first.details["output"], "aaa")
        second = await tool.execute(
            "r2", {"action": "read", "process_id": process_id, "since": first.details["next_since"],
                   "wait_ms": 5000}, context=context)
        self.assertEqual(second.details["output"], "bbb")
        third = await tool.execute(
            "r3", {"action": "read", "process_id": process_id, "since": second.details["next_since"]},
            context=context)
        self.assertEqual(third.details["output"], "")
        await manager.terminate_run(context.run_id)

    async def test_a_bounded_window_reports_the_gap_instead_of_wrong_bytes(self):
        manager = ProcessManager(window_chars=10, wall_timeout=20)
        context = self.context()
        result = await self.start(manager, context, "printf '0123456789abcdefghij'; sleep 0.3",
                                  yield_time_ms=250, max_output_chars=10)
        process_id = result.details["process_id"]
        # Only the tail is retained, and the model is told what it missed rather
        # than being handed a page that silently starts mid-output.
        read = await self.process_tool(manager).execute(
            "read", {"action": "read", "process_id": process_id, "since": 0}, context=context)
        self.assertGreater(read.details["dropped"], 0)
        self.assertIn("Gap: output before cursor", read.content[0].text)
        self.assertTrue(read.details["output"].endswith("j"))
        await manager.terminate_run(context.run_id)

    async def test_write_stdin_and_close_stdin_end_an_interactive_process(self):
        manager = ProcessManager(wall_timeout=20)
        context = self.context()
        result = await self.start(manager, context, "cat", yield_time_ms=200)
        process_id = result.details["process_id"]
        tool = self.process_tool(manager)
        written = await tool.execute("w1", {"action": "write", "process_id": process_id,
                                            "chars": "hello stdin\n"}, context=context)
        self.assertEqual(written.details["written"], 12)
        await tool.execute("w2", {"action": "write", "process_id": process_id,
                                  "close_stdin": True}, context=context)
        read = await tool.execute("r1", {"action": "read", "process_id": process_id,
                                         "since": 0, "wait_ms": 5000}, context=context)
        self.assertIn("hello stdin", read.details["output"])
        self.assertEqual(read.details["exit_code"], 0)
        # A closed process refuses stdin instead of pretending to accept it.
        refused = await tool.execute("w3", {"action": "write", "process_id": process_id,
                                            "chars": "late"}, context=context)
        self.assertTrue(refused.is_error)
        self.assertIn("already exited", refused.content[0].text)
        await manager.terminate_run(context.run_id)

    async def test_stop_kills_the_process_group(self):
        manager = ProcessManager(wall_timeout=20)
        context = self.context()
        result = await self.start(
            manager, context,
            f"sh -c 'sleep 0.6; printf late > {shlex.quote(str(self.root / 'late-marker'))}'",
            yield_time_ms=150)
        self.assertTrue(result.details["running"])
        stopped = await self.process_tool(manager).execute(
            "s1", {"action": "stop", "process_id": result.details["process_id"]}, context=context)
        self.assertIn("Stopped", stopped.content[0].text)
        self.assertFalse(stopped.details["running"])
        await asyncio.sleep(0.8)
        # A surviving member of the group would write this after the stop.
        self.assertFalse((self.root / "late-marker").exists())

    async def test_wall_clock_terminates_a_process_nobody_stops(self):
        manager = ProcessManager(wall_timeout=0.3)
        context = self.context()
        result = await self.start(manager, context, "sleep 30", yield_time_ms=100)
        read = await self.process_tool(manager).execute(
            "r1", {"action": "read", "process_id": result.details["process_id"], "since": 0,
                   "wait_ms": 5000}, context=context)
        self.assertTrue(read.details["timed_out"])
        self.assertFalse(read.details["running"])
        self.assertIn("wall clock expired", read.content[0].text)
        await manager.terminate_run(context.run_id)

    async def test_process_ids_are_scoped_to_one_run(self):
        manager = ProcessManager(wall_timeout=20)
        mine, other = self.context(), self.context()
        result = await self.start(manager, mine, "sleep 30", yield_time_ms=100)
        for action in ("read", "stop"):
            with self.assertRaises(LookupError) as error:
                await self.process_tool(manager).execute(
                    action, {"action": action, "process_id": result.details["process_id"]},
                    context=other)
            # The message must not confirm that the id exists elsewhere.
            self.assertIn("known: none", str(error.exception))
        await manager.terminate_run(mine.run_id)

    async def test_live_processes_are_capped_per_run(self):
        manager = ProcessManager(max_per_run=1, wall_timeout=20)
        context = self.context()
        await self.start(manager, context, "sleep 30", yield_time_ms=100)
        with self.assertRaises(ProcessLimitError) as error:
            await self.start(manager, context, "sleep 30", yield_time_ms=100)
        self.assertIn("process(action='stop')", str(error.exception))
        await manager.terminate_run(context.run_id)

    async def test_terminate_run_reclaims_every_child(self):
        manager = ProcessManager(wall_timeout=20)
        context = self.context()
        marker = self.root / "orphan"
        await self.start(manager, context, f"sleep 0.5; printf bad > {shlex.quote(str(marker))}",
                         yield_time_ms=150)
        self.assertEqual(await manager.terminate_run(context.run_id), 1)
        self.assertEqual(manager.owned(context.run_id), [])
        await asyncio.sleep(0.7)
        self.assertFalse(marker.exists())

    async def test_one_shot_calls_do_not_enter_the_process_table(self):
        manager = ProcessManager(wall_timeout=20)
        context = self.context()
        result = await self.start(manager, context, "printf one-shot")
        self.assertEqual(result.details["exit_code"], 0)
        self.assertNotIn("process_id", result.details)
        self.assertEqual(manager.owned(context.run_id), [])

    async def test_a_session_without_a_manager_is_refused(self):
        executor = ToolExecutor(create_builtin_registry(allow_host_commands=True), allow_host_commands=True)
        result = await executor.execute(
            ToolCall("c", "exec_command", {"cmd": "true", "yield_time_ms": 100}),
            context=self.context())
        self.assertTrue(result.is_error)
        self.assertIn("no process manager", result.content[0].text)

    async def test_the_registry_exposes_process_only_with_host_commands(self):
        manager = ProcessManager()
        self.assertIsNone(create_builtin_registry().get("process"))
        registry = create_builtin_registry(allow_host_commands=True, processes=manager)
        self.assertIsNotNone(registry.get("process"))
        self.assertIs(registry.get("exec_command").processes, manager)
        # The tool surface stays inside the documented cap of twelve.
        self.assertLessEqual(len(registry.definitions()), 12)

    async def test_read_requires_a_process_id_and_list_does_not(self):
        executor = ToolExecutor(create_builtin_registry(allow_host_commands=True,
                                                       processes=ProcessManager()),
                                allow_host_commands=True)
        context = self.context()
        missing = await executor.execute(ToolCall("c1", "process", {"action": "read"}), context=context)
        self.assertTrue(missing.is_error)
        self.assertIn("process_id", missing.content[0].text)
        listed = await executor.execute(ToolCall("c2", "process", {"action": "list"}), context=context)
        self.assertFalse(listed.is_error)
        self.assertIn("No processes in this run", listed.content[0].text)

    async def test_a_finished_command_is_reported_once_and_dropped(self):
        manager = ProcessManager(wall_timeout=20)
        context = self.context()
        started = time.monotonic()
        result = await self.start(manager, context, "printf quick", yield_time_ms=5000)
        # It finished inside the yield window, so the call waited rather than
        # handing back a handle nothing needs.
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(result.details["exit_code"], 0)
        self.assertFalse(result.details["running"])
        self.assertNotIn("process_id", result.content[0].text)
        self.assertEqual(manager.owned(context.run_id), [])


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class ProcessRunTests(unittest.IsolatedAsyncioTestCase):
    """Run end is the only promise a session process gets."""

    async def asyncSetUp(self):
        schema = "chagent_process_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        self.addAsyncCleanup(self.drop_schema, schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()

    async def drop_schema(self, schema):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    async def test_run_end_terminates_the_process_it_started(self):
        marker = self.workspace / "run-orphan"
        runner = create_demo_runner(
            self.database, tool_name="exec_command",
            tool_arguments={"cmd": f"sleep 0.6; printf bad > {shlex.quote(str(marker))}",
                            "yield_time_ms": 150},
            allow_host_commands=True)
        runner.model.context_window = 16_384
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "start a long command")
        self.assertEqual(result.end_reason, "model_stop")
        # The model was told it holds a handle, and the run still took it away.
        self.assertIn("Running in background", result.message.content[0].text)
        self.assertEqual(runner.processes.owned(result.run_id), [])
        await asyncio.sleep(0.8)
        self.assertFalse(marker.exists())

