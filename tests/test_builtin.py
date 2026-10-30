"""The mode boundary: a tool the mode does not allow is never registered.

Two layers are asserted here, because either one alone is a decoration:

* ``create_builtin_registry(mode=...)`` must not list write or host tools in
  ``work``. This is the layer the model can see.
* the executor built for a face must refuse a call naming a tool that face omits,
  even if it arrived from somewhere other than the prompt.
"""

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.context.builder import BasicContextBuilder
from chagent.diagnostics.behavior import describe, describe_warnings
from chagent.domain.messages import ToolCall
from chagent.domain.sessions import SESSION_MODES, validate_mode
from chagent.models.base import Model
from chagent.models.fake import FakeModelClient
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.tools.builtin import EXECUTION_TOOLS, WRITE_TOOLS, create_builtin_registry
from chagent.tools.executor import ToolExecutor
from chagent.tools.web import WebAccess

WORK_TOOLS = frozenset({"list_dir", "read_file", "glob", "grep"})


def names(registry):
    return {tool.name for tool in registry.definitions()}


class ToolFaceTests(unittest.TestCase):
    def test_work_registers_reads_and_nothing_that_writes_or_runs(self):
        registry = create_builtin_registry(mode="work", allow_host_commands=True)
        self.assertEqual(names(registry), WORK_TOOLS)
        self.assertFalse(names(registry) & WRITE_TOOLS)
        # Host tools need both the code face and the switch; the switch alone
        # must never be enough.
        self.assertFalse(names(registry) & EXECUTION_TOOLS)

    def test_code_adds_writes_and_only_then_host_tools(self):
        writes = names(create_builtin_registry(mode="code"))
        self.assertEqual(writes, WORK_TOOLS | WRITE_TOOLS)
        with_host = names(create_builtin_registry(mode="code", allow_host_commands=True))
        self.assertEqual(with_host, WORK_TOOLS | WRITE_TOOLS | {"exec_command", "run_tests"})

    def test_work_keeps_the_read_only_extras(self):
        """Artifacts and the web are reads: they exist in both faces."""
        registry = create_builtin_registry(mode="work", artifacts=object(), web=WebAccess("tvly-k"))
        self.assertLessEqual({"read_artifact", "web_search", "web_fetch"}, names(registry))
        self.assertFalse(names(registry) & WRITE_TOOLS)

    def test_mode_is_required_and_validated(self):
        with self.assertRaises(TypeError):
            create_builtin_registry()  # type: ignore[call-arg]
        with self.assertRaises(ValueError):
            create_builtin_registry(mode="admin")
        self.assertEqual(SESSION_MODES, ("work", "code"))
        self.assertEqual(validate_mode("code"), "code")

    def test_the_executor_of_a_face_refuses_a_tool_that_face_omits(self):
        executor = ToolExecutor(create_builtin_registry(mode="work"))
        result = asyncio.run(executor.execute(ToolCall("c1", "write_file",
                                                       {"path": "x.txt", "content": "no"})))
        self.assertTrue(result.is_error)
        self.assertIn("Unknown tool", result.content[0].text)


class ModeSettingsTests(unittest.TestCase):
    def test_code_mode_is_off_unless_asked(self):
        self.assertFalse(Settings().code_mode)
        self.assertEqual(Settings().enabled_modes, ("work",))
        self.assertEqual(Settings.from_env({"CHAGENT_CODE_MODE": "on"}).enabled_modes, ("work", "code"))

    def test_unknown_switch_values_are_rejected(self):
        for value in ("true", "yes", "1"):
            with self.assertRaises(ValueError):
                Settings.from_env({"CHAGENT_CODE_MODE": value})
        with self.assertRaises(ValueError):
            Settings.from_env({"CHAGENT_DEFAULT_MODE": "auto"})

    def test_a_default_the_build_cannot_serve_is_a_startup_error(self):
        with self.assertRaises(ValueError) as caught:
            Settings(default_mode="code")
        self.assertIn("CHAGENT_CODE_MODE", str(caught.exception))
        self.assertEqual(Settings(code_mode=True, default_mode="code").default_mode, "code")

    def test_the_behavior_line_names_the_mode_and_inert_switches_are_reported(self):
        line = describe(Settings.from_env({"CHAGENT_CODE_MODE": "on", "CHAGENT_DEFAULT_MODE": "code"}))
        self.assertIn("mode=code code=on", line)
        self.assertIn("mode=work code=off", describe(Settings()))
        self.assertEqual(describe_warnings(Settings()), [])
        warnings = describe_warnings(Settings(allow_host_commands=True, sandbox="workspace-write",
                                              approval="untrusted"))
        self.assertEqual(len(warnings), 3)
        self.assertTrue(all("inert" in item for item in warnings))
        # The same switches are exactly what the code face is for.
        self.assertEqual(describe_warnings(Settings(code_mode=True, allow_host_commands=True,
                                                   sandbox="workspace-write", approval="untrusted")), [])


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class RunnerModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.schema = "chagent_mode_test_" + uuid4().hex
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

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def build(self, *, code_mode: bool, tool_name: str, arguments: dict):
        model = Model(id="fake", provider="local-test", api="fake", context_window=16384, max_tokens=512)
        return create_runner(self.database, model, FakeModelClient(tool_name, arguments), code_mode=code_mode)

    async def test_build_switch_decides_which_faces_exist(self):
        off = self.build(code_mode=False, tool_name="read_file", arguments={"path": "a.txt"})
        self.assertEqual(set(off.tool_faces), {"work"})
        on = self.build(code_mode=True, tool_name="read_file", arguments={"path": "a.txt"})
        self.assertEqual(set(on.tool_faces), {"work", "code"})
        self.assertFalse(names(on.tool_faces["work"].registry) & WRITE_TOOLS)
        self.assertTrue(names(on.tool_faces["code"].registry) & WRITE_TOOLS)

    async def test_a_work_session_cannot_call_a_write_tool_even_if_the_model_names_it(self):
        runner = self.build(code_mode=True, tool_name="write_file",
                            arguments={"path": "notes.txt", "content": "no"})
        session = await runner.conversations.create_session(self.workspace, mode="work")
        result = await runner.run(session, "写文件")
        self.assertFalse((self.workspace / "notes.txt").exists())
        self.assertIn("Unknown tool", result.message.content[0].text)

    async def test_a_code_session_writes_through_the_same_runner(self):
        runner = self.build(code_mode=True, tool_name="write_file",
                            arguments={"path": "notes.txt", "content": "yes\n"})
        session = await runner.conversations.create_session(self.workspace, mode="code")
        await runner.run(session, "写文件")
        self.assertEqual((self.workspace / "notes.txt").read_text(), "yes\n")

    async def test_a_mode_this_build_does_not_enable_still_resolves_safely(self):
        runner = self.build(code_mode=False, tool_name="read_file", arguments={"path": "a.txt"})
        self.assertIs(runner.executor_for("code"), runner.tool_faces["work"])
        self.assertIs(runner.executor_for(None), runner.tool_faces["work"])

    async def test_new_sessions_default_to_work_and_old_rows_do_too(self):
        runner = self.build(code_mode=True, tool_name="read_file", arguments={"path": "a.txt"})
        created = await runner.conversations.create_session(self.workspace)
        self.assertEqual((await runner.conversations.get_session(created)).mode, "work")
        # A row written before 0015 has no mode of its own; the migration default
        # is what a reloaded process sees.
        async with self.database.connection() as connection:
            await connection.execute("UPDATE sessions SET mode = DEFAULT WHERE id = %s", (created,))
        self.assertEqual((await runner.conversations.get_session(created)).mode, "work")
        with self.assertRaises(ValueError):
            await runner.conversations.create_session(self.workspace, mode="admin")


class FaceResolutionTests(unittest.TestCase):
    def test_a_runner_without_faces_falls_back_to_its_single_executor(self):
        """The demo/probe harness resolves every run to one face on purpose."""
        from chagent.agent.runner import AgentRunner

        executor = ToolExecutor(create_builtin_registry(mode="code"))
        runner = AgentRunner.__new__(AgentRunner)
        runner.tools = executor
        runner.tool_faces = None
        self.assertIs(AgentRunner.executor_for(runner, "work"), executor)
        self.assertIs(AgentRunner.executor_for(runner, "code"), executor)


if __name__ == "__main__":
    unittest.main()
