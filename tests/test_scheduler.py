"""Core batch acceptance: ordering, barriers, bounded work and cancellation."""

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import threading
from time import sleep
import unittest
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.runner import RunFailedError
from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.domain.memories import MemoryPolicy
from chagent.domain.messages import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from chagent.models.events import DoneEvent, StartEvent
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.tools.filesystem import ReadFileTool, WriteFileTool


class CallsClient:
    def __init__(self, calls):
        self.calls, self.requests = calls, []

    async def stream(self, request):
        self.requests.append(deepcopy(request))
        message = AssistantMessage([], "fake", "test", "test", 0, "pending")
        yield StartEvent(message)
        message = deepcopy(message)
        if isinstance(request.messages[-1], UserMessage):
            message.content, message.stop_reason = deepcopy(self.calls), "toolUse"
        else:
            message.content, message.stop_reason = [TextContent("batch complete")], "stop"
        yield DoneEvent(message.stop_reason, message)


class TrackedTool(AgentTool):
    def __init__(self, tracker, name="tracked", *, mode="parallel", delay=0, progress=False):
        super().__init__(name=name, description=name, label=name, execution_mode=mode,
                         parameters={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]})
        self.tracker, self.delay, self.progress = tracker, delay, progress

    async def execute(self, call_id, params, signal=None, on_update=None, *, context=None):
        value = params["value"]
        self.tracker["active"] += 1
        self.tracker["peak"] = max(self.tracker["peak"], self.tracker["active"])
        self.tracker["trace"].append(("start", self.name, value))
        self.tracker["started"][value].set()
        try:
            self.tracker["signals"].append(signal)
            if self.progress and on_update:
                for index in range(100):
                    on_update(AgentToolResult([TextContent(f"preview {index}")]))
                self.tracker["late_update"] = on_update
            await self.tracker["gates"][value].wait()
            await asyncio.sleep(self.delay)
            return AgentToolResult([TextContent(str(value))])
        finally:
            self.tracker["active"] -= 1
            self.tracker["trace"].append(("end", self.name, value))
            self.tracker["ended"][value].set()


def tracker(*, released=False):
    value = {"active": 0, "peak": 0, "trace": [], "signals": [],
             "gates": [asyncio.Event() for _ in range(32)],
             "started": [asyncio.Event() for _ in range(32)], "ended": [asyncio.Event() for _ in range(32)]}
    if released:
        for gate in value["gates"]:
            gate.set()
    return value


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.schema = "chagent_scheduler_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.runner = create_demo_runner(self.database, memory_policy=MemoryPolicy(enabled=False), max_tool_concurrency=2)
        self.runner.model.context_window = 100000
        self.session = await self.runner.conversations.create_session(self.workspace)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def calls(self, names):
        return [ToolCall(f"call-{index}", name, {"value": index}) for index, name in enumerate(names)]

    async def rows(self, run_id):
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT call_id, status, call_index FROM tool_calls WHERE run_id = %s ORDER BY call_index", (run_id,))
            return await cursor.fetchall()

    async def start(self, calls):
        self.runner.client = CallsClient(calls)
        task = asyncio.create_task(self.runner.run(self.session, "controlled batch"))

        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        return task

    async def test_parallel_overlap_cap_and_original_result_order(self):
        trace = tracker()
        self.runner.tools.registry.register(TrackedTool(trace))
        task = await self.start(self.calls(["tracked"] * 4))
        await asyncio.wait_for(asyncio.gather(trace["started"][0].wait(), trace["started"][1].wait()), 5)
        self.assertEqual(trace["peak"], 2)
        trace["gates"][1].set()
        await asyncio.wait_for(trace["ended"][1].wait(), 5)
        self.assertFalse(trace["started"][2].is_set(), "Reorder window must remain bounded behind a slow first result")
        trace["gates"][0].set()
        for gate in trace["gates"]:
            gate.set()
        result = await asyncio.wait_for(task, 5)
        self.assertLess(trace["trace"].index(("end", "tracked", 1)), trace["trace"].index(("end", "tracked", 0)))
        results = [message for message in self.runner.client.requests[-1].messages if isinstance(message, ToolResultMessage)]
        self.assertEqual([message.tool_call_id for message in results], [f"call-{i}" for i in range(4)])
        self.assertEqual([row["status"] for row in await self.rows(result.run_id)], ["completed"] * 4)
        self.assertEqual(trace["active"], 0)

    async def test_exclusive_call_is_a_barrier(self):
        trace = tracker(released=True)
        self.runner.tools.registry.register(TrackedTool(trace, delay=.03))
        self.runner.tools.registry.register(TrackedTool(trace, "modify", mode="sequential", delay=.01))
        task = await self.start(self.calls(["tracked", "tracked", "modify", "tracked", "tracked"]))
        await asyncio.wait_for(task, 5)
        events = trace["trace"]
        self.assertLess(events.index(("end", "tracked", 0)), events.index(("start", "modify", 2)))
        self.assertLess(events.index(("end", "tracked", 1)), events.index(("start", "modify", 2)))
        self.assertLess(events.index(("end", "modify", 2)), events.index(("start", "tracked", 3)))
        self.assertLess(events.index(("end", "modify", 2)), events.index(("start", "tracked", 4)))

    async def test_host_execution_remains_exclusive_even_if_marked_parallel(self):
        trace = tracker(released=True)
        self.runner.tools.registry.register(TrackedTool(trace, delay=.01))
        host = TrackedTool(trace, "host", delay=.01)
        host.requires_host_execution = True
        self.runner.tools.registry.register(host)
        self.runner.tools.allow_host_commands = True
        task = await self.start(self.calls(["tracked", "host", "tracked"]))
        await asyncio.wait_for(task, 5)
        self.assertEqual(trace["peak"], 1)

    async def test_cancel_preserves_finished_results_and_cancels_queued_calls(self):
        trace = tracker()
        self.runner.tools.registry.register(TrackedTool(trace))
        task = await self.start(self.calls(["tracked"] * 6))
        await asyncio.wait_for(asyncio.gather(trace["started"][0].wait(), trace["started"][1].wait()), 5)
        trace["gates"][1].set()
        await asyncio.wait_for(trace["ended"][1].wait(), 5)
        await asyncio.sleep(0)
        run_id = next(iter(self.runner._states))
        self.runner.cancel(run_id)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        self.assertEqual([row["status"] for row in await self.rows(run_id)],
                         ["unknown", "completed", "cancelled", "cancelled", "cancelled", "cancelled"])
        history = await self.runner.conversations.messages(self.session)
        results = [record.message for record in history if isinstance(record.message, ToolResultMessage)]
        self.assertEqual([message.tool_call_id for message in results], ["call-1"])
        self.assertEqual(trace["active"], 0)
        self.assertTrue(all(signal.is_set() for signal in trace["signals"]))
        self.assertFalse(any(task.get_name().startswith("tool-worker-") for task in asyncio.all_tasks()))
        self.assertEqual((await self.runner.runs.get(run_id))["end_reason"], "user_cancelled")

    async def test_timeout_stops_workers_and_leaves_clear_states(self):
        trace = tracker()
        self.runner.tools.registry.register(TrackedTool(trace))
        self.runner.timeout = .2
        task = await self.start(self.calls(["tracked"] * 4))
        with self.assertRaises(RunFailedError) as error:
            await asyncio.wait_for(task, 5)
        self.assertEqual(error.exception.end_reason, "run_timeout")
        self.assertEqual([row["status"] for row in await self.rows(error.exception.run_id)],
                         ["unknown", "unknown", "cancelled", "cancelled"])
        self.assertEqual(trace["active"], 0)

    async def test_ordinary_tool_error_does_not_cancel_siblings(self):
        trace = tracker(released=True)
        self.runner.tools.registry.register(TrackedTool(trace))
        calls = self.calls(["tracked", "tracked", "tracked"])
        calls[1].arguments = {"value": "invalid"}
        result = await (await self.start(calls))
        self.assertEqual(result.end_reason, "model_stop")
        self.assertEqual([row["status"] for row in await self.rows(result.run_id)], ["completed", "failed", "completed"])
        self.assertTrue(trace["started"][2].is_set())

    async def test_progress_is_bounded_ordered_and_late_updates_are_ignored(self):
        trace = tracker(released=True)
        self.runner.tools.registry.register(TrackedTool(trace, progress=True))
        result = await (await self.start(self.calls(["tracked"])))
        before = await self.runner.runs.events(result.run_id)
        progress = [event for event in before if event["type"] == "tool_progress"]
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["payload"]["text"], "preview 99")
        self.assertLess(progress[0]["sequence"], next(event["sequence"] for event in before if event["type"] == "tool_completed"))
        trace["late_update"](AgentToolResult([TextContent("late must not appear")]))
        await asyncio.sleep(0)
        self.assertEqual(before, await self.runner.runs.events(result.run_id))

    async def test_cancel_during_result_commit_does_not_duplicate_message(self):
        trace = tracker(released=True)
        self.runner.tools.registry.register(TrackedTool(trace))
        original = self.runner.runs.save_tool_result
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            record = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return record

        self.runner.runs.save_tool_result = delayed
        task = await self.start(self.calls(["tracked"]))
        await asyncio.wait_for(entered.wait(), 5)
        self.runner.cancel(next(iter(self.runner._states)))
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        history = await self.runner.conversations.messages(self.session)
        self.assertEqual(sum(isinstance(item.message, ToolResultMessage) for item in history), 1)

    async def test_result_commit_failure_preserves_sibling_and_never_retries_failed_commit(self):
        trace = tracker(released=True)
        self.runner.tools.registry.register(TrackedTool(trace))
        async with self.database.connection() as connection:
            await connection.execute("""
                CREATE FUNCTION reject_first_result() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.call_id = 'call-0' AND NEW.status = 'completed' THEN RAISE EXCEPTION 'controlled rejection'; END IF;
                    RETURN NEW;
                END $$;
                CREATE CONSTRAINT TRIGGER reject_first_result AFTER UPDATE ON tool_calls
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_first_result();
            """)
        calls = []
        original = self.runner.runs.save_tool_result

        async def counted(*args, **kwargs):
            calls.append(args[-1].tool_call_id)
            return await original(*args, **kwargs)

        self.runner.runs.save_tool_result = counted
        task = await self.start(self.calls(["tracked", "tracked"]))
        with self.assertRaises(RunFailedError) as error:
            await asyncio.wait_for(task, 5)
        self.assertEqual(error.exception.end_reason, "storage_error")
        self.assertEqual(calls.count("call-0"), 1)
        self.assertEqual([row["status"] for row in await self.rows(error.exception.run_id)], ["unknown", "completed"])
        self.assertEqual(len(self.runner.client.requests), 1)
        self.assertEqual(trace["active"], 0)

    async def test_queue_must_commit_before_any_tool_starts(self):
        trace = tracker(released=True)
        self.runner.tools.registry.register(TrackedTool(trace))
        async with self.database.connection() as connection:
            await connection.execute("""
                CREATE FUNCTION reject_queue() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'controlled queue rejection'; END $$;
                CREATE CONSTRAINT TRIGGER reject_queue AFTER INSERT ON tool_calls
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_queue();
            """)
        task = await self.start(self.calls(["tracked"] * 3))
        with self.assertRaises(RunFailedError) as error:
            await asyncio.wait_for(task, 5)
        self.assertEqual(trace["trace"], [])
        self.assertEqual(await self.rows(error.exception.run_id), [])

    async def test_concurrency_limit_is_shared_across_sessions(self):
        trace = tracker(released=True)
        self.runner.tools.registry.register(TrackedTool(trace, delay=.04))
        self.runner.client = CallsClient(self.calls(["tracked"] * 3))
        other = await self.runner.conversations.create_session(self.workspace)
        results = await asyncio.gather(self.runner.run(self.session, "one"), self.runner.run(other, "two"))
        self.assertEqual(trace["peak"], 2)
        self.assertTrue(all(result.end_reason == "model_stop" for result in results))

    async def test_read_threads_overlap_and_write_observes_a_barrier(self):
        path = self.workspace / "data.txt"
        path.write_text("before", encoding="utf-8")
        active, peak = 0, 0
        lock = threading.Lock()
        self.runner.tools.registry.get("read_file").handle = read = ReadFileTool().handle

        def slow_read(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                sleep(.04)
                return read(*args, **kwargs)
            finally:
                with lock:
                    active -= 1

        writer = self.runner.tools.registry.get("write_file")
        write = WriteFileTool().handle

        def guarded_write(*args, **kwargs):
            self.assertEqual(active, 0)
            return write(*args, **kwargs)

        self.runner.tools.registry.get("read_file").handle = slow_read
        writer.handle = guarded_write
        calls = [ToolCall("r0", "read_file", {"path": "data.txt"}), ToolCall("r1", "read_file", {"path": "data.txt"}),
                 ToolCall("w", "write_file", {"path": "data.txt", "content": "after", "overwrite": True}),
                 ToolCall("r2", "read_file", {"path": "data.txt"})]
        await (await self.start(calls))
        results = [message for message in self.runner.client.requests[-1].messages if isinstance(message, ToolResultMessage)]
        self.assertEqual(peak, 2)
        self.assertIn("before", results[0].content[0].text)
        self.assertIn("before", results[1].content[0].text)
        self.assertIn("after", results[3].content[0].text)

    async def test_exclusive_gate_covers_other_sessions_in_same_workspace(self):
        trace = tracker()
        self.runner.tools.registry.register(TrackedTool(trace))
        self.runner.tools.registry.register(TrackedTool(trace, "modify", mode="sequential"))
        reader = await self.start(self.calls(["tracked"]))
        await asyncio.wait_for(trace["started"][0].wait(), 5)
        self.runner.client.calls = [ToolCall("writer", "modify", {"value": 1})]
        other = await self.runner.conversations.create_session(self.workspace)
        writer = asyncio.create_task(self.runner.run(other, "write"))
        try:
            await asyncio.sleep(.05)
            self.assertFalse(trace["started"][1].is_set())
            trace["gates"][0].set()
            await asyncio.wait_for(trace["started"][1].wait(), 5)
            self.assertTrue(trace["ended"][0].is_set())
            trace["gates"][1].set()
            await asyncio.wait_for(asyncio.gather(reader, writer), 5)
            self.assertEqual(self.runner.scheduler._gates, {})
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    async def test_cancelled_read_thread_holds_workspace_gate_until_drained(self):
        (self.workspace / "data.txt").write_text("before")
        started, release, ended = threading.Event(), threading.Event(), threading.Event()
        read = ReadFileTool().handle

        def blocked_read(*args, **kwargs):
            started.set()
            try:
                release.wait(5)
                return read(*args, **kwargs)
            finally:
                ended.set()

        trace = tracker(released=True)
        self.runner.tools.registry.get("read_file").handle = blocked_read
        self.runner.tools.registry.register(TrackedTool(trace, "modify", mode="sequential"))
        reader = await self.start([ToolCall("read", "read_file", {"path": "data.txt"})])
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        reader.cancel()
        self.runner.client.calls = self.calls(["modify"])
        other = await self.runner.conversations.create_session(self.workspace)
        writer = asyncio.create_task(self.runner.run(other, "write"))
        try:
            await asyncio.sleep(.05)
            self.assertFalse(reader.done())
            self.assertEqual(trace["trace"], [])
            release.set()
            await asyncio.wait_for(writer, 5)
            await asyncio.gather(reader, return_exceptions=True)
            self.assertTrue(ended.is_set())
            self.assertEqual(self.runner.scheduler._gates, {})
        finally:
            release.set()
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    async def test_separate_workspaces_do_not_share_exclusive_gate(self):
        trace = tracker()
        self.runner.tools.registry.register(TrackedTool(trace))
        self.runner.tools.registry.register(TrackedTool(trace, "modify", mode="sequential"))
        reader = await self.start(self.calls(["tracked"]))
        await asyncio.wait_for(trace["started"][0].wait(), 5)
        self.runner.client.calls = [ToolCall("writer", "modify", {"value": 1})]
        other_path = self.workspace / "other"
        other_path.mkdir()
        other = await self.runner.conversations.create_session(other_path)
        writer = asyncio.create_task(self.runner.run(other, "write"))
        try:
            await asyncio.wait_for(trace["started"][1].wait(), 5)
            self.assertFalse(trace["ended"][0].is_set())
            trace["gates"][0].set()
            trace["gates"][1].set()
            await asyncio.wait_for(asyncio.gather(reader, writer), 5)
            self.assertEqual(self.runner.scheduler._gates, {})
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    async def test_progress_persistence_failure_stops_workers_and_rejects_late_updates(self):
        trace = tracker()
        self.runner.tools.registry.register(TrackedTool(trace, progress=True))
        original = self.runner.runs.event

        async def fail_progress(run_id, kind, payload):
            if kind == "tool_progress":
                raise RuntimeError("controlled progress failure")
            return await original(run_id, kind, payload)

        self.runner.runs.event = fail_progress
        task = await self.start(self.calls(["tracked"] * 3))
        with self.assertRaises(RunFailedError):
            await asyncio.wait_for(task, 5)
        self.assertEqual(trace["active"], 0)
        trace["late_update"](AgentToolResult([TextContent("late")]))
        self.assertFalse(any(task.get_name().startswith(("tool-worker-", "tool-progress-"))
                             for task in asyncio.all_tasks() if not task.done()))
        self.assertEqual(self.runner.scheduler._gates, {})

    async def test_one_slot_is_deterministically_serial(self):
        self.runner = create_demo_runner(self.database, memory_policy=MemoryPolicy(enabled=False), max_tool_concurrency=1)
        trace = tracker(released=True)
        self.runner.tools.registry.register(TrackedTool(trace))
        await (await self.start(self.calls(["tracked"] * 3)))
        self.assertEqual(trace["trace"], [(kind, "tracked", i) for i in range(3) for kind in ("start", "end")])
