import asyncio
import unittest
from unittest.mock import patch

from chagent.domain.messages import ToolCall
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.echo import EchoTool
from chagent.tools.executor import ToolExecutor
from chagent.tools.registry import ToolRegistry


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register(EchoTool())
        self.executor = ToolExecutor(self.registry)

    async def test_unknown_and_invalid_calls_become_error_results(self):
        for call in (
            ToolCall("a", "missing", {}),
            ToolCall("b", "echo", {"text": 7}),
            ToolCall("c", "echo", {}),
            ToolCall("d", "echo", {"text": "ok"}, arguments_error="Truncated JSON"),
        ):
            with self.subTest(call=call):
                self.assertTrue((await self.executor.execute(call)).is_error)

    async def test_timeout_and_cancellation(self):
        class SlowEcho(EchoTool):
            async def execute(self, *args, **kwargs):
                await asyncio.Event().wait()

        registry = ToolRegistry()
        registry.register(SlowEcho())
        executor = ToolExecutor(registry, timeout=0.01)
        self.assertTrue((await executor.execute(ToolCall("a", "echo", {"text": "x"}))).is_error)
        task = asyncio.create_task(ToolExecutor(registry).execute(ToolCall("b", "echo", {"text": "x"})))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_tool_budget_and_ceiling(self):
        class BudgetEcho(EchoTool):
            def execution_timeout(self, params):
                return 0.08

            async def execute(self, *args, **kwargs):
                await asyncio.sleep(0.04)
                return await super().execute(*args, **kwargs)

        registry = ToolRegistry()
        registry.register(BudgetEcho())
        call = ToolCall("a", "echo", {"text": "x"})
        with patch.dict("os.environ", {"CHAGENT_TOOL_TIMEOUT_CEILING": "1"}):
            self.assertFalse((await ToolExecutor(registry, timeout=0.01).execute(call)).is_error)
        with patch.dict("os.environ", {"CHAGENT_TOOL_TIMEOUT_CEILING": "0.02"}):
            result = await ToolExecutor(registry, timeout=0.01).execute(call)
            self.assertTrue(result.is_error)
            self.assertIn("0.02 seconds", result.content[0].text)

    async def test_declarations_are_isolated_and_duplicates_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register(EchoTool())
        self.registry.definitions()[0].parameters["properties"]["text"]["type"] = "number"
        result = await self.executor.execute(ToolCall("a", "echo", {"text": "原样返回"}))
        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, "原样返回")


class BuiltinSurfaceTests(unittest.TestCase):
    """``echo`` is a probe tool; the live surface must not spend a slot on it."""

    def test_echo_is_not_registered_by_default(self):
        registry = create_builtin_registry(mode="code")
        self.assertIsNone(registry.get("echo"))
        self.assertEqual({tool.name for tool in registry.definitions()},
                         {"list_dir", "read_file", "write_file", "edit_file", "glob", "grep", "apply_patch", "create_directory", "read_skill"})

    def test_demo_path_opts_in(self):
        registry = create_builtin_registry(mode="code", include_echo=True)
        self.assertIsNotNone(registry.get("echo"))
        self.assertEqual(len(registry.definitions()), 10)
