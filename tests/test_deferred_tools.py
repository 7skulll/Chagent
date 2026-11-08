"""Deferred tool declarations: index them, load them on request.

The rule under test is that a deferred tool costs nothing until the model asks
for it, and that asking changes exactly one thing on the next request — the tool
becomes declarable. The index itself must not move, or every load would break the
prompt prefix twice.
"""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.turn_note import last_request_message
from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.domain.messages import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from chagent.models.events import ContentStartEvent, DoneEvent, StartEvent, ToolCallEndEvent
from chagent.storage.database import Database
from chagent.storage.migrate import migrate

from chagent.tools.builtin import (
    DEFERRABLE_TOOLS, MIN_DEFERRED_TOOLS, create_builtin_registry,
)
from chagent.tools.context import DeferredTools, ToolContext
from chagent.tools.deferred import MAX_RESULTS, ToolSearchTool
from chagent.tools.processes import ProcessManager
from chagent.tools.registry import INDEX_DESCRIPTION_CHARS, ToolRegistry, index_line
from chagent.tools.web import WebAccess
from chagent.tools.echo import EchoTool


def registry_with(*names):
    registry = ToolRegistry()
    for name in names:
        tool = EchoTool()
        tool.name = name
        registry.register(tool)
    return registry


class IndexTests(unittest.TestCase):
    def test_the_index_line_is_one_bounded_line(self):
        self.assertEqual(index_line("  Read a file.\nSecond line is ignored. "), "Read a file.")
        long = index_line("word " * 100)
        self.assertLessEqual(len(long), INDEX_DESCRIPTION_CHARS + 2)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(index_line(""), "")

    def test_a_deferred_tool_is_indexed_not_declared(self):
        registry = registry_with("read_file", "run_tests")
        registry.defer(["run_tests"])
        self.assertEqual([tool.name for tool in registry.declarations()], ["read_file"])
        self.assertEqual([tool.name for tool in registry.declarations({"run_tests"})],
                         ["read_file", "run_tests"])
        self.assertEqual([name for name, _ in registry.deferred_index()], ["run_tests"])
        # Inspection paths still see everything.
        self.assertEqual(len(registry.definitions()), 2)

    def test_deferring_an_unknown_tool_is_a_registration_mistake(self):
        with self.assertRaises(ValueError):
            registry_with("read_file").defer(["not_a_tool"])

    def test_the_index_does_not_change_when_a_tool_is_loaded(self):
        """The index is per run, not per load: otherwise one load breaks two things."""
        registry = registry_with("read_file", "run_tests")
        registry.defer(["run_tests"])
        before = registry.deferred_index()
        registry.declarations({"run_tests"})
        self.assertEqual(registry.deferred_index(), before)


class FaceTests(unittest.TestCase):
    def face(self, **overrides):
        values = dict(mode="code", allow_host_commands=True, processes=ProcessManager(),
                      web=WebAccess("synthetic-key"))
        values.update(overrides)
        return create_builtin_registry(**values)

    def test_deferral_applies_to_the_whole_deferrable_set_at_once(self):
        eager = self.face()
        deferred = self.face(defer_tools=True)
        self.assertLessEqual(DEFERRABLE_TOOLS & {tool.name for tool in eager.definitions()},
                             DEFERRABLE_TOOLS)
        self.assertEqual(names(deferred), names(eager) | {"tool_search"} - {
            name for name, _ in deferred.deferred_index()})
        self.assertGreaterEqual(len(deferred.deferred_index()), MIN_DEFERRED_TOOLS)
        self.assertIn("tool_search", names(deferred))
        self.assertIn("tool_search", [tool.name for tool in deferred.declarations()])
        # The model is told they exist, just not how to call them.
        self.assertEqual({name for name, _ in deferred.deferred_index()},
                         {name for name in DEFERRABLE_TOOLS if deferred.get(name) is not None})

    def test_turning_it_off_declares_everything(self):
        registry = self.face(defer_tools=False)
        self.assertEqual(registry.deferred_index(), [])
        self.assertIsNone(registry.get("tool_search"))
        self.assertEqual(len(registry.declarations()), len(registry.definitions()))

    def test_too_few_deferrable_tools_pays_for_itself_so_none_are_deferred(self):
        """Below the threshold the index plus tool_search costs what it saves."""
        registry = create_builtin_registry(mode="code", allow_host_commands=True, defer_tools=True)
        candidate = DEFERRABLE_TOOLS & {tool.name for tool in registry.definitions()}
        self.assertLess(len(candidate), MIN_DEFERRED_TOOLS)
        self.assertEqual(registry.deferred_index(), [])
        self.assertIsNone(registry.get("tool_search"))


class ToolSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = registry_with("read_file", "run_tests", "web_fetch", "process", "create_file")
        self.registry.defer(["run_tests", "web_fetch", "process", "create_file"])
        self.tool = ToolSearchTool(self.registry)
        self.deferred = DeferredTools()
        self.context = ToolContext(workspace=MagicMock(), deferred=self.deferred)

    async def call(self, query):
        return await self.tool.execute("call", {"query": query}, context=self.context)

    def test_a_query_about_running_tests_finds_the_tool_by_purpose(self):
        matches = [name for name, _ in self.tool.matching("run the test suite")]
        self.assertEqual(matches[0], "run_tests")

    def test_a_name_query_outranks_a_description_match(self):
        self.assertEqual([name for name, _ in self.tool.matching("process")][0], "process")
        self.assertEqual([name for name, _ in self.tool.matching("web fetch")][0], "web_fetch")

    async def test_loading_records_the_tool_for_the_run_and_returns_its_schema(self):
        result = await self.call("run tests")
        self.assertFalse(result.is_error)
        text = result.content[0].text
        self.assertIn("## run_tests", text)
        self.assertIn("parameters:", text)
        self.assertEqual(self.deferred.names, {"run_tests"})
        self.assertIn("Not loaded yet:", text)

    async def test_one_query_loads_at_most_the_cap(self):
        await self.call("tool")
        self.assertLessEqual(len(self.deferred.names), MAX_RESULTS)

    async def test_a_query_naming_an_already_declared_tool_loads_nothing(self):
        """Searching for a tool you already have must not load an unrelated one.

        Measured on 2026-11-08: the model asked for "host_process" (declared),
        got the deferred "host_exec" back because both descriptions say
        "native", and reported to the user that host_process had no parameters.
        """
        result = await self.call("read_file with a line range")
        self.assertFalse(result.is_error)
        self.assertIn("read_file", result.content[0].text)
        self.assertIn("already declared", result.content[0].text)
        self.assertEqual(self.deferred.names, set())

    async def test_a_query_that_matches_nothing_says_what_exists(self):
        result = await self.call("draw a picture")
        self.assertTrue(result.is_error)
        self.assertIn("run_tests", result.content[0].text)
        self.assertEqual(self.deferred.names, set())

    async def test_loading_needs_a_run_that_declares_tools_on_demand(self):
        with self.assertRaises(PermissionError):
            await self.tool.execute("call", {"query": "run tests"},
                                    context=ToolContext(workspace=MagicMock()))


def names(registry):
    return {tool.name for tool in registry.definitions()}


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class LoadThenCallTests(unittest.IsolatedAsyncioTestCase):
    """A loaded tool must be callable on the very next request."""

    async def asyncSetUp(self):
        self.schema = "chagent_deferred_test_" + uuid4().hex
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

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def runner(self):
        runner = create_demo_runner(self.database)
        registry = runner.tools.registry
        # The harness decides which tools are deferred; the face builder's own
        # choice is covered above.
        registry.defer(["echo"])
        registry.register(ToolSearchTool(registry))
        runner.client = LoadingClient()
        return runner

    async def test_the_model_searches_then_calls_without_being_told_twice(self):
        runner = self.runner()
        session = await runner.conversations.create_session(Path(self.temporary.name))
        result = await runner.run(session, "load echo and use it")
        self.assertEqual(len(runner.client.requests), 3)
        first, second, _ = runner.client.requests
        def declared(request):
            return [tool.name for tool in request.messages[0].tools_added]
        self.assertNotIn("echo", declared(first))
        self.assertIn("tool_search", declared(first))
        self.assertIn("echo", declared(second), "loading must reach the next request")
        self.assertEqual(first.messages[0].sections["tool_index"],
                         second.messages[0].sections["tool_index"],
                         "the index is per run; a load may not move it")
        self.assertIn("- echo:", first.messages[0].sections["tool_index"])
        self.assertEqual(result.message.content[0].text, "done")

    async def test_the_search_result_carries_the_schema_the_declaration_omits(self):
        runner = self.runner()
        session = await runner.conversations.create_session(Path(self.temporary.name))
        await runner.run(session, "load echo and use it")
        result = next(item for item in runner.client.requests[2].messages
                      if isinstance(item, ToolResultMessage) and item.tool_name == "tool_search")
        text = result.content[0].text
        self.assertIn("## echo", text)
        self.assertIn("loaded", text.lower())

    async def test_the_loaded_call_actually_runs(self):
        runner = self.runner()
        session = await runner.conversations.create_session(Path(self.temporary.name))
        result = await runner.run(session, "load echo and use it")
        history = await runner.conversations.messages(session)
        echo_result = next(item for item in history
                           if isinstance(item.message, ToolResultMessage) and item.message.tool_name == "echo")
        self.assertIn("loaded", echo_result.message.content[0].text)
        self.assertEqual(result.message.stop_reason, "stop")


class LoadingClient:
    """Searches for ``echo``, calls it, then stops."""

    def __init__(self):
        self.requests = []

    async def stream(self, request):
        from copy import deepcopy
        from time import time_ns
        self.requests.append(deepcopy(request))
        partial = AssistantMessage([], request.model.api, request.model.provider, request.model.id,
                                   time_ns() // 1_000_000, "pending")
        yield StartEvent(partial=partial)
        last = last_request_message(request.messages)
        if isinstance(last, UserMessage):
            call = ToolCall("search-1", "tool_search", {"query": "echo"})
        elif isinstance(last, ToolResultMessage) and last.tool_name == "tool_search":
            call = ToolCall("call-1", "echo", {"text": "loaded and called"})
        elif isinstance(last, ToolResultMessage):
            final = deepcopy(partial)
            final.content = [TextContent("done")]
            final.stop_reason = "stop"
            yield DoneEvent(reason="stop", message=final)
            return
        else:
            raise AssertionError(f"unexpected turn ending in {type(last).__name__}")
        partial.content.append(call)
        yield ContentStartEvent(type="toolcall_start", content_index=0, partial=partial)
        yield ToolCallEndEvent(content_index=0, tool_call=call, partial=partial)
        final = deepcopy(partial)
        final.stop_reason = "toolUse"
        yield DoneEvent(reason="toolUse", message=final)
