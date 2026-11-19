"""Frozen commands and Docker evidence contracts independent of a daemon."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from chagent.tools.context import ToolContext
from eval.benchmarks.terminal_bench import tb22
from eval.benchmarks.terminal_bench.harbor_agent import HarborExecution, TerminalCommand
from eval.manifests import sha256_json


def event(kind, action, identifier, name, time_ns):
    return {
        "Type": kind,
        "Action": action,
        "timeNano": time_ns,
        "Actor": {"ID": identifier, "Attributes": {"name": name}},
    }


def interrupt_events():
    return [
        event("container", "create", "c", "trial__env-main-1", 1),
        event("network", "create", "n", "trial__env_default", 2),
        event("container", "exec_start: TB22_CANCEL", "c", "trial__env-main-1", 3),
        event("container", "stop", "c", "trial__env-main-1", 4),
        event("container", "archive-path", "c", "trial__env-main-1", 5),
        event("container", "destroy", "c", "trial__env-main-1", 6),
        event("network", "destroy", "n", "trial__env_default", 7),
    ]


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_resources_reject_wrong_order_and_wrong_topology(self):
        rows = interrupt_events()
        self.assertEqual(tb22.resources(rows, "trial", "cancel")["main_stop_ns"], 4)
        with self.assertRaisesRegex(ValueError, "topology"):
            tb22.resources(rows, "trial", "missing")
        rows[4]["timeNano"] = 2
        with self.assertRaisesRegex(ValueError, "stop before Harbor collection"):
            tb22.resources(rows, "trial", "cancel")

    def test_cancel_matches_the_started_command_in_the_bound_main(self):
        row = event("container", "exec_start: TB22_CANCEL", "c", "trial__env-main-1", 1)
        self.assertTrue(tb22.is_cancel_start(row, "TRIAL"))
        self.assertFalse(tb22.is_cancel_start(row, "other"))
        row["Actor"]["Attributes"]["name"] = "trial__verifier__trial-main-1"
        self.assertFalse(tb22.is_cancel_start(row, "trial"))

    def test_ledger_matches_frozen_arguments_and_fixture_is_pinned(self):
        arguments = {"cmd": "pwd"}
        plan = {
            "profile": {"tools_sha256": "tools"},
            "control": {"arguments_sha256": sha256_json(arguments)},
        }
        record = {
            "request_tool_sha256": ["tools"],
            "tool": {"name": "exec_command", "arguments": arguments},
        }
        tb22.validate_ledger(record, plan)
        record["tool"]["arguments"] = {"cmd": "pwd", "timeout_sec": 1}
        with self.assertRaisesRegex(ValueError, "arguments differ"):
            tb22.validate_ledger(record, plan)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "changed.mxl"
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "artifact differs"):
                tb22.controls(path)

    async def test_event_capture_accepts_delayed_destroy_within_bound(self):
        def ndjson(events):
            return "\n".join(json.dumps(row) for row in events)

        rows = interrupt_events()
        capture = AsyncMock(side_effect=(ndjson(rows[:-1]), ndjson(rows)))
        with TemporaryDirectory() as tmp, patch.object(tb22, "docker", capture):
            proof = await tb22.capture_cleanup_events(
                Path(tmp), "start", "trial", "cancel", wait=2
            )
        self.assertEqual(proof["destroyed"], {"c": "container", "n": "network"})
        self.assertEqual(capture.await_count, 2)

    async def test_second_cancellation_waits_for_main_stop_then_propagates(self):
        started, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Backend:
            context_id = uuid4()

            async def exec(self, command, *, timeout_sec):
                started.set()
                await asyncio.Future()

            async def stop_service(self, service):
                stopping.set()
                await release.wait()

        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            session, run = uuid4(), uuid4()
            backend = Backend()
            execution = HarborExecution(backend, backend.context_id, session, path)
            await execution.bind(session, run, path)
            context = ToolContext(path, session, run, task_id=str(backend.context_id))
            task = asyncio.create_task(
                TerminalCommand(execution).execute(
                    "id", {"cmd": "pwd"}, context=context
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                await asyncio.wait_for(stopping.wait(), 1)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertEqual(
                execution.interruption, {"reason": "cancelled", "main_stop": "returned"}
            )
