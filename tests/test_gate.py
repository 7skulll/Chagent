"""Optional tool gates refuse safely before a tool is scheduled."""

import asyncio
from pathlib import Path
import unittest
from uuid import uuid4

from chagent.domain.messages import ToolCall
from chagent.tools.context import ToolContext
from chagent.tools.gate import GateDecision, HumanGate
from chagent.tools.policy import ApprovalPolicy
from chagent.tools.scheduler import ToolScheduler


class RefusingGate:
    name = "test"

    async def check(self, call, **kwargs):
        return GateDecision(False, "blocked by rule", "denied")


class BrokenGate:
    name = "broken"

    async def check(self, call, **kwargs):
        raise RuntimeError("gate failed")


class EventRecorder:
    def __init__(self):
        self.events = []

    async def event(self, run_id, kind, data):
        self.events.append((kind, data))


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_chain_allows_and_refusal_is_preserved(self):
        call = ToolCall("c1", "exec_command", {"cmd": "echo ok"})
        context = ToolContext(Path("/tmp"), uuid4(), uuid4())
        scheduler = ToolScheduler(None, gates=())
        refused, grants = await scheduler._ask([call], [0], context, None, [uuid4()], asyncio.Event(), None)
        self.assertEqual((refused, grants), ({}, {}))
        scheduler.gates = (RefusingGate(),)
        refused, grants = await scheduler._ask([call], [0], context, None, [uuid4()], asyncio.Event(), None)
        self.assertEqual(refused[0].details["approval"], "denied")
        self.assertEqual(refused[0].content[0].text, "blocked by rule")
        self.assertEqual(grants, {})

    async def test_gate_exception_fails_closed(self):
        recorder = EventRecorder()
        scheduler = ToolScheduler(recorder, gates=(BrokenGate(),))
        call = ToolCall("c1", "exec_command", {"cmd": "echo ok"})
        refused, _ = await scheduler._ask([call], [0], ToolContext(Path("/tmp"), uuid4(), uuid4()),
                                           None, [uuid4()], asyncio.Event(), None)
        self.assertEqual(refused[0].details["approval"], "unavailable")
        self.assertEqual(recorder.events[0][0], "tool_call_refused")

    async def test_human_gate_skips_broker_when_policy_does_not_require_it(self):
        gate = HumanGate(ApprovalPolicy("never"), None, "isolated")
        decision = await gate.check(ToolCall("c1", "exec_command", {"cmd": "echo ok"}),
                                    context=ToolContext(Path("/tmp"), uuid4(), uuid4()),
                                    tool_record_id=uuid4(), signal=asyncio.Event(), budget=None)
        self.assertTrue(decision.allow)
