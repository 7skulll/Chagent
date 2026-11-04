"""Optional decisions made before a tool takes execution resources."""

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from chagent.domain.messages import ToolCall
from chagent.tools.context import ToolContext
from chagent.tools.approvals import ApprovalBroker, UNAVAILABLE
from chagent.tools.policy import ApprovalPolicy


@dataclass(frozen=True, slots=True)
class GateDecision:
    allow: bool
    reason: str = ""
    code: str = "denied"
    approval_id: UUID | None = None


class ToolGate(Protocol):
    name: str

    async def check(self, call: ToolCall, *, context: ToolContext,
                    tool_record_id: UUID, signal: asyncio.Event, budget) -> GateDecision: ...


class HumanGate:
    name = "human"

    def __init__(self, policy: ApprovalPolicy, broker: ApprovalBroker | None, sandbox_mode: str):
        self.policy, self.broker, self.sandbox_mode = policy, broker, sandbox_mode

    async def check(self, call: ToolCall, *, context: ToolContext,
                    tool_record_id: UUID, signal: asyncio.Event, budget) -> GateDecision:
        if not self.policy.requires_approval(call.name, call.arguments, sandbox=self.sandbox_mode):
            return GateDecision(True)
        if self.broker is None:
            return GateDecision(False, UNAVAILABLE, "unavailable")
        outcome = await self.broker.request(
            run_id=context.run_id, tool_record_id=tool_record_id,
            tool_name=call.name, arguments=call.arguments,
            cwd=str(context.workspace), signal=signal, budget=budget)
        return GateDecision(outcome.approved and outcome.approval_id is not None,
                            outcome.message, outcome.reason, outcome.approval_id)
