"""Complete runner checks against isolated real PostgreSQL schemas."""

import asyncio
from contextlib import suppress
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
import httpx

from chagent.agent.runner import RunFailedError
from chagent.bootstrap import create_demo_runner, create_runner
from chagent.config import Settings
from chagent.domain.codec import message_from_dict
from chagent.domain.messages import AssistantMessage, ToolCall, ToolResultMessage
from chagent.models.events import DoneEvent, ErrorEvent, StartEvent
from chagent.models.fake import FakeModelClient
from chagent.models.factory import create_model_client
from chagent.models.settings import ModelSettings
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import ConversationRepository, RunBusyError, RunRepository
from chagent.tools.echo import EchoTool
from chagent.tools.executor import ToolExecutor
from chagent.tools.registry import ToolRegistry


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_test_" + uuid4().hex
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
        self.runner = create_demo_runner(self.database)
        self.workspace_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace_directory.cleanup)
        self.session = await self.runner.conversations.create_session(Path(self.workspace_directory.name))

    async def test_invalid_old_workspace_never_reaches_model(self):
        from chagent.boundaries import protected_source
        invalid = await self.runner.conversations.create_session(protected_source())
        with self.assertRaises(RunFailedError):
            await self.runner.run(invalid, "must not call model")
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT count(*) AS n FROM model_calls")
            self.assertEqual((await cursor.fetchone())["n"], 0)

    async def test_protected_source_in_input_is_blocked_and_audited_before_network(self):
        import httpx
        from chagent.boundaries import protected_source
        from chagent.bootstrap import create_runner
        from chagent.models.factory import create_model_client
        from chagent.models.settings import ModelSettings
        settings = ModelSettings.from_env({"CHAGENT_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret"})
        sent = []
        def transport(request):
            sent.append(request)
            return httpx.Response(400)
        async with create_model_client(settings, transport=httpx.MockTransport(transport)) as client:
            runner = create_runner(self.database, settings.model(), client)
            with self.assertRaises(RunFailedError) as failure:
                await runner.run(self.session, f"请读取 {protected_source()}")
        self.assertEqual(sent, [])
        events = await runner.runs.events(failure.exception.run_id)
        self.assertEqual(sum(event["type"] == "model_export_blocked" for event in events), 1)

    async def test_virtual_workspace_file_argument_executes_in_session_root(self):
        root = Path(self.workspace_directory.name)
        (root / "notes.txt").write_text("virtual-path-ok", encoding="utf-8")
        runner = create_demo_runner(self.database, tool_name="read_file",
                                    tool_arguments={"path": "/workspace/notes.txt"})
        result = await runner.run(self.session, "read")
        self.assertEqual(result.end_reason, "model_stop")
        self.assertIn("virtual-path-ok", result.message.content[0].text)

    async def test_run_records_real_file_change(self):
        root = Path(self.workspace_directory.name)
        runner = create_demo_runner(self.database, tool_name="write_file",
                                    tool_arguments={"path": "site.txt", "content": "built"})
        result = await runner.run(self.session, "build")
        self.assertEqual((root / "site.txt").read_text(), "built")
        events = await runner.runs.events(result.run_id)
        before = next(event["payload"] for event in events if event["type"] == "workspace_manifest_before")
        after = next(event["payload"] for event in events if event["type"] == "workspace_manifest_after")
        self.assertEqual(before["files"], [])
        self.assertEqual(after["diff"]["counts"], {"added": 1, "modified": 0, "deleted": 0})
        self.assertEqual(after["diff"]["added"][0]["path"], "site.txt")
        self.assertEqual(await runner.runs.file_diff(result.run_id), after["diff"])

    async def test_workspace_replaced_between_runs_is_refused(self):
        workspace = Path(self.workspace_directory.name)
        await self.runner.run(self.session, "first")
        moved = workspace.with_name(workspace.name + "-old")
        workspace.rename(moved)
        workspace.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(moved))
        with self.assertRaisesRegex(RunFailedError, "replaced"):
            await self.runner.run(self.session, "second")

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def test_success_round_trip_and_continue_with_new_runner(self):
        # This test asserts that a short history is retained in full. The prompt
        # now also carries the run-static environment snapshot, so the window has
        # to leave room for it rather than sit just above the message count.
        self.runner.model.context_window = 16384
        result = await self.runner.run(self.session, "第一轮")
        self.assertEqual(result.message.content[0].text, "工具执行结果：第一轮")
        self.assertEqual((await self.runner.runs.get(result.run_id))["status"], "completed")
        events = await self.runner.runs.events(result.run_id)
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[-1]["type"], "run_completed")
        self.assertIn("workspace_manifest_before", [event["type"] for event in events])
        self.assertEqual(events[-2]["type"], "workspace_manifest_after")
        self.assertEqual(await self.runner.runs.file_diff(result.run_id),
                         {"complete": True, "added": [], "modified": [], "deleted": [],
                          "counts": {"added": 0, "modified": 0, "deleted": 0}})
        restored = await ConversationRepository(self.database).messages(self.session)
        self.assertEqual([item.message.role for item in restored], ["user", "assistant", "toolResult", "assistant"])
        self.assertIsInstance(restored[2].message, ToolResultMessage)
        self.assertEqual(restored[1].message.content[0].id, restored[2].message.tool_call_id)
        other = create_demo_runner(self.database)
        other.model.context_window = 16384
        await other.run(self.session, "第二轮")
        self.assertEqual(len(await other.conversations.messages(self.session)), 8)
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT request, context_snapshot FROM model_calls ORDER BY created_at")
            rows = await cursor.fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(rows[2]["context_snapshot"]["source_message_ids"]), 5)
        declared = [tool["name"] for tool in rows[0]["request"]["messages"][0]["tools_added"]]
        self.assertIn("echo", declared)
        self.assertEqual(declared, sorted(declared))

    async def test_error_persisted_and_failed_run_not_replayed(self):
        class ErrorClient:
            async def stream(self, request):
                yield ErrorEvent(reason="error", error=AssistantMessage(
                    [], "fake", "test", "test", 1, "error", error_message="Provider rejected request",
                ))

        self.runner.client = ErrorClient()
        with self.assertRaises(RunFailedError) as raised:
            await self.runner.run(self.session, "失败输入")
        failed = await self.runner.runs.get(raised.exception.run_id)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Provider rejected", failed["error"])
        result = await create_demo_runner(self.database).run(self.session, "恢复输入")
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT request FROM model_calls WHERE run_id = %s AND iteration = 0", (result.run_id,))
            request = (await cursor.fetchone())["request"]
        users = [item["content"] for item in request["messages"] if item["role"] == "user"]
        self.assertEqual(users, ["恢复输入"])
        self.assertEqual(len(await self.runner.conversations.messages(self.session)), 6)

    async def test_small_window_still_runs_in_a_large_workspace(self):
        """The snapshot must not push a small model over its own context budget.

        9216 rather than 8192: tool declarations now carry their numeric bounds,
        which is ~160 tokens of fixed prefix. The assertion this test exists for
        is that 60 directories do not enter the prompt — not that the prompt is
        exactly 8 KiB.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(60):
                (root / f"module-{index:03d}-with-a-long-name").mkdir()
            session = await self.runner.conversations.create_session(root)
            self.runner.model.context_window = 9216
            result = await self.runner.run(session, "hello")
        self.assertEqual(result.end_reason, "model_stop")

    async def test_environment_is_not_injected_into_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "app").mkdir()
            (workspace / "app" / "pricing.py").write_text("value = 1\n", encoding="utf-8")
            session = await self.runner.conversations.create_session(workspace)
            result = await self.runner.run(session, "第一轮")
        events = await self.runner.runs.events(result.run_id)
        projected = [event for event in events if event["type"] == "environment_projected"]
        self.assertEqual(projected, [])
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT iteration, request FROM model_calls WHERE run_id = %s ORDER BY iteration", (result.run_id,))
            rows = await cursor.fetchall()
        self.assertEqual([row["iteration"] for row in rows], [0, 1])

    async def test_progress_note_is_attached_from_the_second_turn(self):
        """The model cannot plan against a horizon it is never told about."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            session = await self.runner.conversations.create_session(workspace)
            result = await self.runner.run(session, "第一轮")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT iteration, request FROM model_calls WHERE run_id = %s ORDER BY iteration", (result.run_id,))
            rows = await cursor.fetchall()
        first, second = (json.dumps(row["request"], ensure_ascii=False) for row in rows[:2])
        self.assertNotIn("[turn ", first, "the opening turn has no tool result to attach it to")
        self.assertIn("[turn 2/", second)
        self.assertIn("not a new request", second)
        system = [message_from_dict(row["request"]["messages"][0]).text() for row in rows]
        self.assertEqual(system[0], system[1])
        self.assertNotIn(str(workspace.resolve()), system[0])
        self.assertNotIn("pricing.py", system[0])
        self.assertNotIn("Working directory:", system[0])

    async def test_invalid_arguments_return_to_model_without_execution(self):
        class InvalidArgsClient(FakeModelClient):
            async def stream(self, request):
                async for event in super().stream(request):
                    if isinstance(event, DoneEvent) and event.reason == "toolUse":
                        event.message.content[0].arguments = {"text": 42}
                    yield event

        self.runner.client = InvalidArgsClient()
        result = await self.runner.run(self.session, "输入")
        messages = await self.runner.conversations.messages(self.session)
        self.assertTrue(messages[2].message.is_error)
        self.assertIn("工具返回错误", result.message.content[0].text)
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT status FROM tool_calls WHERE run_id = %s", (result.run_id,))
            self.assertEqual((await cursor.fetchone())["status"], "failed")

    async def test_truncated_calls_and_incomplete_stream_never_execute(self):
        class TruncatedClient(FakeModelClient):
            async def stream(self, request):
                async for event in super().stream(request):
                    if isinstance(event, DoneEvent):
                        event.reason = "length"
                        event.message.stop_reason = "length"
                    yield event

        class IncompleteClient:
            async def stream(self, request):
                yield StartEvent(partial=AssistantMessage([], "fake", "test", "test", 1, "pending"))

        for client in (TruncatedClient(), IncompleteClient()):
            self.runner.client = client
            with self.assertRaises(RunFailedError) as raised:
                await self.runner.run(self.session, "输入")
            async with self.database.connection() as connection:
                cursor = await connection.execute("SELECT count(*) AS total FROM tool_calls WHERE run_id = %s", (raised.exception.run_id,))
                self.assertEqual((await cursor.fetchone())["total"], 0)

    async def test_repeated_identical_tool_calls_end_the_run(self):
        class RepeatingClient:
            """Same call every turn, whatever the last tool result said."""

            def __init__(self, name, arguments):
                self.name, self.arguments, self.calls = name, arguments, 0

            async def stream(self, request):
                self.calls += 1
                partial = AssistantMessage([], request.model.api, request.model.provider,
                                           request.model.id, 0, "pending")
                yield StartEvent(partial=partial)
                final = deepcopy(partial)
                final.content = [ToolCall(f"call-{self.calls}", self.name, deepcopy(self.arguments))]
                final.stop_reason = "toolUse"
                yield DoneEvent(reason="toolUse", message=final)

        self.runner.client = RepeatingClient("echo", {"text": "same"})
        self.runner.max_turns = 12
        self.runner.model.context_window = 16384
        with self.assertRaises(RunFailedError) as raised:
            await self.runner.run(self.session, "开始")
        self.assertEqual(raised.exception.end_reason, "repeated_tool_call", str(raised.exception))
        run = await self.runner.runs.get(raised.exception.run_id)
        self.assertEqual((run["status"], run["end_reason"]), ("failed", "repeated_tool_call"))
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT status, count(*) AS total FROM tool_calls WHERE run_id = %s GROUP BY status",
                (raised.exception.run_id,))
            statuses = {row["status"]: row["total"] for row in await cursor.fetchall()}
            cursor = await connection.execute(
                "SELECT count(*) AS total FROM model_calls WHERE run_id = %s", (raised.exception.run_id,))
            turns = (await cursor.fetchone())["total"]
        # Two calls run, the third and fourth are refused, then the run stops.
        self.assertEqual(statuses, {"completed": 2, "failed": 2})
        self.assertEqual(turns, 4)
        self.assertLess(turns, self.runner.max_turns)

    async def test_cancellation_and_concurrent_submission(self):
        class BlockingEcho(EchoTool):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()

            async def execute(self, *args, **kwargs):
                self.started.set()
                await asyncio.Event().wait()

        tool = BlockingEcho()
        registry = ToolRegistry()
        registry.register(tool)
        self.runner.tools = ToolExecutor(registry)
        task = asyncio.create_task(self.runner.run(self.session, "等待取消"))

        async def cleanup():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self.addAsyncCleanup(cleanup)
        await asyncio.wait_for(tool.started.wait(), timeout=5)
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT id FROM runs WHERE session_id = %s", (self.session,))
            run_id = (await cursor.fetchone())["id"]
        with self.assertRaises(RunBusyError):
            await create_demo_runner(self.database).run(self.session, "不能并发")
        self.assertTrue(self.runner.cancel(run_id))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.runner.cancel(run_id))
        self.assertEqual((await RunRepository(self.database).get(run_id))["status"], "cancelled")
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT status FROM tool_calls WHERE run_id = %s", (run_id,))
            self.assertEqual((await cursor.fetchone())["status"], "unknown")
        await create_demo_runner(self.database).run(self.session, "取消后继续")

    async def test_cancellation_after_lease_loss_is_a_classified_failure(self):
        """A cancel that cannot prove ownership must not escape as a raw
        RunOwnershipLost, and must not claim a 'cancelled' terminal state."""
        started = asyncio.Event()

        class BlockingClient:
            async def stream(self, request):
                started.set()
                yield StartEvent(partial=AssistantMessage([], "fake", "test", "test", 1, "pending"))
                await asyncio.Event().wait()

        self.runner.client = BlockingClient()
        task = asyncio.create_task(self.runner.run(self.session, "等待取消"))

        async def cleanup():
            task.cancel()
            with suppress(asyncio.CancelledError, RunFailedError):
                await task

        self.addAsyncCleanup(cleanup)
        await asyncio.wait_for(started.wait(), timeout=5)
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT id FROM runs WHERE session_id = %s", (self.session,))
            run_id = (await cursor.fetchone())["id"]
            await connection.execute(
                "UPDATE runs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = %s", (run_id,))
        self.assertTrue(self.runner.cancel(run_id))
        with self.assertRaises(RunFailedError) as raised:
            await task
        self.assertEqual(raised.exception.end_reason, "storage_error")
        runs = RunRepository(self.database)
        # The owner cannot record an outcome it cannot prove: the row stays
        # running and the recovery scan closes it as interrupted.
        self.assertEqual((await runs.get(run_id))["status"], "running")
        self.assertEqual(await runs.reap_expired(), [run_id])
        self.assertEqual((await runs.get(run_id))["status"], "interrupted")

    async def test_iteration_limit_persists_failure(self):
        self.runner.max_turns = 1
        with self.assertRaisesRegex(RunFailedError, "Maximum model turns") as raised:
            await self.runner.run(self.session, "输入")
        self.assertEqual((await self.runner.runs.get(raised.exception.run_id))["status"], "failed")

    async def test_file_tools_use_persisted_workspace_and_keep_execution_records(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            session = await self.runner.conversations.create_session(workspace)
            writer = create_demo_runner(self.database, tool_name="write_file", tool_arguments={"path": "notes.txt", "content": "before\n"})
            await writer.run(session, "创建文件")
            editor = create_demo_runner(self.database, tool_name="edit_file", tool_arguments={"path": "notes.txt", "old_text": "before", "new_text": "after"})
            await editor.run(session, "编辑文件")
            reader = create_demo_runner(self.database, tool_name="read_file", tool_arguments={"path": "notes.txt"})
            result = await reader.run(session, "读取文件")
            self.assertIn("L1: after", result.message.content[0].text)
            self.assertEqual((workspace / "notes.txt").read_text(), "after\n")
            messages = await self.runner.conversations.messages(session)
            self.assertEqual(len(messages), 12)
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                    "SELECT t.name, t.status, t.result FROM tool_calls t JOIN runs r ON r.id = t.run_id "
                    "WHERE r.session_id = %s ORDER BY t.created_at", (session,),
                )
                records = await cursor.fetchall()
            self.assertEqual([row["name"] for row in records], ["write_file", "edit_file", "read_file"])
            self.assertTrue(all(row["status"] == "completed" for row in records))
            self.assertIn("sha256", records[-1]["result"]["details"])

    async def test_context_rejection_is_recorded_before_model_or_tool_execution(self):
        self.runner.model.context_window = 1000
        with self.assertRaises(RunFailedError) as raised:
            await self.runner.run(self.session, "x" * 2000)
        recorded = await self.runner.runs.events(raised.exception.run_id)
        # The environment snapshot is projected before the context is built, so it
        # is audited even for a run that never reaches the model.
        events = [item for item in recorded if item["type"] not in {
            "environment_projected", "workspace_manifest_before", "workspace_manifest_after"}]
        self.assertEqual([item["type"] for item in events], ["run_started", "context_rejected", "run_failed"])
        rejection = events[1]["payload"]
        self.assertGreater(rejection["required_tokens"], rejection["input_budget"])
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT count(*) AS total FROM model_calls WHERE run_id = %s", (raised.exception.run_id,))
            self.assertEqual((await cursor.fetchone())["total"], 0)

    async def test_provider_adapters_complete_tool_loop_without_persisting_credentials(self):
        for provider in ("openai", "deepseek", "qwen", "anthropic"):
            settings = ModelSettings.from_env({"CHAGENT_PROVIDER": provider, {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "qwen": "DASHSCOPE_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]: "mock-only-private-key"})
            calls = []

            async def handler(request):
                body = json.loads(request.content)
                calls.append(body)
                if provider == "anthropic":
                    if len(calls) == 1:
                        block = {"type": "tool_use", "id": "tool-one", "name": "echo", "input": {"text": "from network"}}
                        stop = "tool_use"
                    else:
                        results = body["messages"][-1]["content"]
                        self.assertEqual(results[0]["content"][0]["text"], "from network")
                        block, stop = {"type": "text", "text": "provider completed"}, "end_turn"
                    events = [
                        {"type": "message_start", "message": {"id": "msg", "model": settings.model_id, "usage": {"input_tokens": 10}}},
                        {"type": "content_block_start", "index": 0, "content_block": block},
                        {"type": "content_block_stop", "index": 0},
                        {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}},
                        {"type": "message_stop"},
                    ]
                    text = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
                else:
                    if len(calls) == 1:
                        delta = {"tool_calls": [{"index": 0, "id": "tool-one", "type": "function", "function": {"name": "echo", "arguments": '{"text":"from network"}'}}]}
                        stop = "tool_calls"
                    else:
                        # The runner appends its turn note as a trailing user
                        # message, so the tool result is the last message with
                        # role "tool", not simply the last message.
                        tool_messages = [item for item in body["messages"] if item.get("role") == "tool"]
                        self.assertEqual(tool_messages[-1]["content"], "from network")
                        delta, stop = {"content": "provider completed"}, "stop"
                    event = {"choices": [{"index": 0, "delta": delta, "finish_reason": stop}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
                    text = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

            async with create_model_client(settings, transport=httpx.MockTransport(handler)) as client:
                # The scripted provider calls ``echo``; the live surface does not
                # carry it, so the probe tool is opted in explicitly here.
                runner = create_runner(self.database, settings.model(), client, include_echo=True)
                result = await runner.run(self.session, "完成一次真实协议的工具调用")
            self.assertEqual(result.message.content[0].text, "provider completed")
            self.assertEqual(len(calls), 2)
            async with self.database.connection() as connection:
                cursor = await connection.execute("SELECT request, response FROM model_calls WHERE run_id = %s", (result.run_id,))
                records = await cursor.fetchall()
            self.assertNotIn("mock-only-private-key", json.dumps(records, default=str))
            self.assertEqual(records[0]["request"]["model"]["endpoint"], settings.base_url)

    async def test_provider_authentication_error_is_persisted_without_raw_credentials(self):
        secret = "mock-auth-error-private-key"
        settings = ModelSettings.from_env({"OPENAI_API_KEY": secret})
        transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": {"message": secret}}))
        async with create_model_client(settings, transport=transport) as client:
            runner = create_runner(self.database, settings.model(), client)
            with self.assertRaises(RunFailedError) as raised:
                await runner.run(self.session, "认证失败测试")
        run = await runner.runs.get(raised.exception.run_id)
        self.assertEqual(run["status"], "failed")
        self.assertIn("401", run["error"])
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT request, response FROM model_calls WHERE run_id = %s", (raised.exception.run_id,))
            records = await cursor.fetchall()
        self.assertNotIn(secret, json.dumps(records, default=str))
        self.assertNotIn(secret, json.dumps(await runner.runs.events(raised.exception.run_id), default=str))
