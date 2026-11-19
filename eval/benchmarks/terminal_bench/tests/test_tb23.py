"""Live TB2.3 admission and accounting are exercised without vendor calls."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from chagent.domain.messages import AssistantMessage, SystemMessage, ToolCall
from chagent.domain.usage import Usage
from chagent.models.base import ModelRequest
from chagent.models.events import DoneEvent
from eval.benchmarks.terminal_bench.harbor_agent import (
    PROMPT,
    TerminalCommand,
    profile_identity,
)
from eval.benchmarks.terminal_bench.live_policy import (
    LiveDiagnosticBudget,
    protocol,
    settings,
)
from eval.budgets import BudgetExceeded


class OfflineProvider:
    def __init__(self, usage):
        self.usage = usage
        self.calls = 0

    async def stream(self, request):
        self.calls += 1
        message = AssistantMessage(
            content=[ToolCall("call", "exec_command", {"cmd": "pwd"})],
            api=request.model.api,
            provider=request.model.provider,
            model=request.model.id,
            timestamp=1,
            stop_reason="toolUse",
            usage=self.usage,
        )
        yield DoneEvent(reason="toolUse", message=message)


def setup(usage):
    config = protocol()
    model = settings("offline-test").model()
    document = SimpleNamespace(budget=config["budget"], pricing=config["pricing"])
    provider = OfflineProvider(usage)
    guard = LiveDiagnosticBudget(provider, document, profile_identity(model), PROMPT)
    request = ModelRequest(
        model,
        [
            SystemMessage(
                "",
                0,
                sections={"preamble": PROMPT},
                tools_added=[TerminalCommand().declaration()],
            )
        ],
        max_tokens=model.max_tokens,
    )
    return provider, guard, request


class LiveBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_usage_settles_before_tool_call_is_yielded(self):
        provider, guard, request = setup(Usage(80, 10, 20, 0, 110))
        events = [event async for event in guard.stream(request)]
        self.assertEqual(len(events), 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(guard.cost()["usd"], 0.000042)

    async def test_unknown_usage_never_yields_tools_or_retries(self):
        provider, guard, request = setup(None)
        for _ in range(2):
            with self.assertRaises(BudgetExceeded):
                async for _event in guard.stream(request):
                    self.fail("Unknown-cost response was released to the runner")
        self.assertEqual(provider.calls, 1)
        self.assertIsNone(guard.cost()["usd"])

    async def test_changed_settings_fail_before_dispatch(self):
        provider, guard, request = setup(None)
        with self.assertRaises(BudgetExceeded):
            async for _event in guard.stream(
                replace(request, max_tokens=request.max_tokens + 1)
            ):
                self.fail("Unfrozen request was dispatched")
        self.assertEqual(provider.calls, 0)

    async def test_reported_usage_over_reservation_stops_terminal_tool_calls(self):
        provider, guard, request = setup(Usage(50000, 5, 0, 0, 50005))
        with self.assertRaisesRegex(BudgetExceeded, "exceeded"):
            async for _event in guard.stream(request):
                self.fail("Over-budget response was released to the runner")
        self.assertEqual(provider.calls, 1)
        self.assertTrue(guard.violation)
