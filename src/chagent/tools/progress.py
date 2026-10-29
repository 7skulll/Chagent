"""Per-run repeat detection: a call that already ran unchanged cannot help.

The measured problem is not a model that fails, but one that repeats a call
whose answer it already has: the same test suite, the same file read, the same
command. Each repeat costs a whole model turn, and ``max_turns`` only stops it
thirty turns later. H batch data: 6 of 10 probe runs contained an identical
``run_tests`` call that produced nothing new.

The guard counts identical calls *within one attempt* and refuses the third. An
attempt ends when a workspace-mutating call succeeds: running the same test
again after an edit is legitimate verification, not a loop. That distinction is
the whole reason the counter is not simply "same arguments anywhere in the run".

A refusal is an ordinary tool error, so the model can still change strategy. If
it repeats the refused call again, the run ends with ``repeated_tool_call``
instead of burning the remaining turns.
"""

from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from chagent.domain.messages import JsonObject, TextContent, ToolCall
from chagent.tools.base import AgentTool, AgentToolResult


# 0 disables the guard. With N, calls 1..N-1 run, the N-th is refused.
REPEAT_LIMIT = 3
# Refused calls for one signature before the run stops instead of retrying.
REPEAT_STOP_AFTER = 2
SUMMARY_CHARS = 600

# A read is identified by what it addresses. Other tools are identified by all
# of their arguments, so two edits to one file remain two different calls.
READ_IDENTITY = {"read_file": ("path", "offset", "limit")}


@dataclass(slots=True)
class Decision:
    execute: bool
    stop: bool = False
    result: AgentToolResult | None = None


@dataclass(slots=True)
class Attempt:
    count: int = 0
    refused: int = 0
    summary: str | None = None


class ProgressGuard:
    def __init__(self, limit: int = REPEAT_LIMIT, stop_after: int = REPEAT_STOP_AFTER):
        if limit != 0 and limit < 2:
            raise ValueError("Repeat limit must be 0 (disabled) or at least 2")
        if stop_after < 1:
            raise ValueError("Repeat stop threshold must be positive")
        self.limit = limit
        self.stop_after = stop_after
        # One attempt map per run: concurrent runs owned by one runner never
        # share counters, and release() bounds the memory.
        self._attempts: dict[UUID, dict[str, Attempt]] = {}

    def release(self, run_id: UUID) -> None:
        self._attempts.pop(run_id, None)

    def signature(self, call: ToolCall, tool: AgentTool | None) -> str:
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        keys = READ_IDENTITY.get(call.name) if tool is not None else None
        if keys is not None:
            normalized: JsonObject = {key: arguments.get(key) for key in keys}
        else:
            normalized = dict(arguments)
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str)
        return f"{call.name}\0{encoded}"

    def check(self, run_id: UUID, call: ToolCall, tool: AgentTool | None) -> Decision:
        if self.limit == 0:
            return Decision(execute=True)
        signature = self.signature(call, tool)
        attempt = self._attempts.setdefault(run_id, {}).setdefault(signature, Attempt())
        attempt.count += 1
        if attempt.count < self.limit:
            return Decision(execute=True)
        attempt.refused += 1
        result = AgentToolResult(
            content=[TextContent(self._refusal(call, attempt))], is_error=True,
            details={"repeated": True, "count": attempt.count, "refusals": attempt.refused,
                     "signature": hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]},
        )
        return Decision(execute=False, stop=attempt.refused >= self.stop_after, result=result)

    def completed(self, run_id: UUID, call: ToolCall, tool: AgentTool | None,
                  result: AgentToolResult) -> None:
        """Record the outcome: a successful mutation starts a new attempt."""
        if self.limit == 0 or tool is None:
            return
        if tool.mutates_workspace and not result.is_error and not result.outcome_unknown:
            # The workspace changed, so earlier repeats are no longer the same
            # attempt. Verification after a change is not a loop.
            self._attempts.pop(run_id, None)
            return
        attempt = self._attempts.get(run_id, {}).get(self.signature(call, tool))
        if attempt is not None and attempt.summary is None:
            attempt.summary = self._summarize(result)

    @staticmethod
    def _summarize(result: AgentToolResult) -> str:
        text = "\n".join(block.text for block in result.content if isinstance(block, TextContent))
        if len(text) <= SUMMARY_CHARS:
            return text
        return text[:SUMMARY_CHARS] + "\n[…truncated]"

    @staticmethod
    def _refusal(call: ToolCall, attempt: Attempt) -> str:
        lines = [f"Repeated tool call: {call.name} with identical arguments has been requested "
                 f"{attempt.count} times without a successful change in between."]
        if attempt.summary:
            lines.append("\nLast result of that call:\n" + attempt.summary)
        lines.append("\nThis attempt is repeating itself. Do not send this call unchanged again: "
                     "change the arguments, use another tool, or stop and explain what is blocked.")
        return "\n".join(lines)
