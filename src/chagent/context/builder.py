"""Budgeted selection of complete turns, without changing persisted history."""

from copy import deepcopy
from dataclasses import asdict, dataclass
import json
from typing import Protocol

from chagent.context.projection import ProjectionCache
from chagent.context.tokens import ByteTokenCounter, TokenCounter
from chagent.domain.context import ContextPolicy, ContextSelection, ContextSnapshot
from chagent.domain.messages import AssistantMessage, Message, SystemMessage, ToolCall, ToolResultMessage, UserMessage
from chagent.domain.recovery import RecoveryNote
from chagent.domain.runs import StoredMessage
from chagent.domain.sessions import TaskState
from chagent.domain.summaries import SessionSummary
from chagent.domain.tools import Tool
from chagent.models.base import Model, ModelRequest
from uuid import UUID


class ContextBudgetError(ValueError):
    def __init__(self, message: str, *, required_tokens: int | None = None, input_budget: int | None = None):
        self.required_tokens = required_tokens
        self.input_budget = input_budget
        super().__init__(message)


class ContextIntegrityError(ValueError):
    pass


@dataclass(slots=True)
class BuiltContext:
    request: ModelRequest
    snapshot: ContextSnapshot


class ContextBuilder(Protocol):
    async def build(
        self, history: list[StoredMessage], model: Model, tools: list[Tool],
        *, task_state: TaskState | None = None, current_run_id: UUID | None = None,
    ) -> BuiltContext: ...


DEFAULT_INSTRUCTIONS = ("Complete the user's task with the available tools. When the user asks for a "
                        "downloadable text file, use create_file with the complete file content and a "
                        "meaningful filename. Do not claim a file exists until the tool succeeds.")

# Tool guidance has its own section so prompt changes remain auditable.
TOOLS_SECTION = (
    "Use only available tools. For code tasks, locate code with grep or glob; when editing tools "
    "are available, apply changes together and verify once. If the user asks for a downloadable "
    "text file, use create_file with complete content and a descriptive filename."
)

# The system prompt is assembled from named sections in a fixed order. The order
# and the bytes matter: this text is the provider's cache prefix, so it is built
# once per run and never re-ordered. Naming the parts makes a prompt change
# auditable as "which section moved" instead of a diff of one long string, and
# gives the cache audit a stable label per section.
SECTION_ORDER = ("preamble", "tools", "rules", "environment", "state")

RULES_SECTION = (
    "Report only what a tool confirmed; never invent file contents, tool output or test results."
)


class BasicContextBuilder:
    def __init__(
        self, instructions: str = DEFAULT_INSTRUCTIONS,
        *, policy: ContextPolicy | None = None, counter: TokenCounter | None = None,
    ):
        self.instructions = instructions
        self.policy = policy or ContextPolicy()
        self.counter = counter or ByteTokenCounter()
        # Recorded alongside the primary estimate so the two never mix.
        self.byte_counter = ByteTokenCounter()

    async def build(
        self, history: list[StoredMessage], model: Model, tools: list[Tool],
        *, task_state: TaskState | None = None, current_run_id: UUID | None = None,
        summary: SessionSummary | None = None,
        allow_over_budget: bool = False,
        cache: ProjectionCache | None = None,
        recovery: RecoveryNote | None = None,
        environment: str | None = None,
    ) -> BuiltContext:
        original = history
        if len({item.id for item in history}) != len(history):
            raise ContextIntegrityError("Duplicate history message IDs")
        if any(left.sequence >= right.sequence for left, right in zip(history, history[1:])):
            raise ContextIntegrityError("History must be in increasing sequence order")
        self._validate(history)
        if summary is not None:
            kept = [item for item in history if item.sequence > summary.covered_through_sequence
                    and not isinstance(item.message, SystemMessage)]
            anchor_present = any(item.id == summary.first_kept_message_id for item in history)
            # A kept run can subsequently fail and disappear from replayable history.
            if not kept or (anchor_present and kept[0].id != summary.first_kept_message_id):
                raise ContextIntegrityError("Summary kept boundary does not match history")
            history = [item for item in history if isinstance(item.message, SystemMessage)
                       or item.sequence > summary.covered_through_sequence
                       or (current_run_id is not None and item.run_id == current_run_id and isinstance(item.message, UserMessage))]
            self._validate(history)
        output = self.policy.output_tokens or model.max_tokens
        if output > model.max_tokens or output < 1 or model.context_window < 1:
            raise ContextBudgetError("Invalid model limits or output reservation")
        budget = model.context_window - output - self.policy.safety_tokens
        if budget <= 0:
            raise ContextBudgetError("Output reservation and safety margin exhaust the context window")
        sections: dict[str, str] = {"preamble": self.instructions,
                                    "tools": TOOLS_SECTION, "rules": RULES_SECTION}
        if environment:
            # Computed once per run by the runner, so the prefix stays byte-identical
            # between turns; see environment.py for why that matters.
            sections["environment"] = environment
        state_parts: list[str] = []
        if task_state is not None:
            state = asdict(task_state)
            state.pop("revision")
            if any(state.values()):
                state_parts.append(
                    "User-maintained task state (current user input takes precedence):\n"
                    + json.dumps(state, ensure_ascii=False)
                )
        if recovery is not None:
            # Host-generated reference data, counted as part of the fixed system
            # cost: it never becomes a message that could be replayed or paired
            # with a tool call.
            state_parts.append(recovery.text)
        if state_parts:
            sections["state"] = "\n\n".join(state_parts)
        # The leading message decides the whole provider prompt cache: it has to
        # be byte-identical between the turns of one run. Task state and the
        # recovery note qualify because the runner loads both once per run and
        # never mutates them mid-run. Anything that changes per turn belongs in
        # a message, not here. The timestamp is fixed for the same reason: this
        # message is not an event, and letting it drift would make two otherwise
        # identical requests compare unequal.
        system = SystemMessage(
            content="", timestamp=0,
            sections={name: sections[name] for name in SECTION_ORDER if name in sections},
            tools_added=deepcopy(tools),
        )

        groups: list[list[StoredMessage]] = []
        pinned: set[UUID] = set()
        projections = cache if cache is not None else ProjectionCache()
        visible = {}
        for item in history:
            visible[item.id] = projections.project(item, self.counter)
            if isinstance(item.message, SystemMessage):
                pinned.add(item.id)
                continue
            if isinstance(item.message, UserMessage):
                groups.append([])
            if not groups:
                if summary is None:
                    raise ContextIntegrityError("History must begin with user input or system instructions")
                groups.append([])
            groups[-1].append(item)
        if not groups:
            raise ContextIntegrityError("Context requires user input")
        required = {item.id for item in groups[-1]}
        if current_run_id is not None:
            current = {item.id for item in history if item.run_id == current_run_id}
            if not current:
                # A regeneration writes no message of its own: it re-asks the
                # question already in the transcript. The turn that must survive
                # compaction is therefore the last one, not an empty set.
                current = {item.id for item in groups[-1]}
            # Preserve whole user turns, even if their messages span IDs supplied by a caller.
            for group in groups:
                if any(item.id in current for item in group):
                    required.update(item.id for item in group)
        selected = pinned | required
        reasons = {identity: "system_instruction" for identity in pinned}
        reasons.update({identity: "current_turn" for identity in required})

        def render(identities: set[UUID]) -> list[Message]:
            reference = [] if summary is None else [self.summary_message(summary.content)]
            # Copies, so a mutated request can never corrupt the projection cache.
            return [deepcopy(system), *reference,
                    *(deepcopy(visible[item.id].message) for item in history if item.id in identities)]

        # Counters that promise additivity let us select turns from cumulative
        # per-message costs; every other counter keeps exact re-rendering.
        additive = getattr(self.counter, "additive", False)
        fixed_cost = (self.counter.count([system] + ([] if summary is None
                                                    else [self.summary_message(summary.content)]))
                      if additive else 0)
        required_cost = (fixed_cost + sum(visible[identity].cost for identity in selected) if additive
                         else self.counter.count(render(selected)))
        if required_cost > budget and not allow_over_budget:
            raise ContextBudgetError(
                f"Required context estimate {required_cost} exceeds input budget {budget}; "
                "current input, task constraints and tool results were not truncated",
                required_tokens=required_cost, input_budget=budget,
            )
        exclusion = "history_limit"
        running = required_cost
        for group in reversed(groups):
            identities = {item.id for item in group}
            if identities <= selected:
                continue
            trial = selected | identities
            if len(trial) > self.policy.max_history_messages:
                exclusion = "history_limit"
                break
            trial_cost = (running + sum(visible[identity].cost for identity in identities - selected) if additive
                          else self.counter.count(render(trial)))
            if trial_cost > budget:
                exclusion = "token_budget"
                break
            selected, running = trial, trial_cost
            reasons.update({identity: "recent_complete_turn" for identity in identities})
        messages = render(selected)
        estimate = self.counter.count(messages)
        estimates = {self.counter.name: estimate}
        if self.counter.name != self.byte_counter.name:
            estimates[self.byte_counter.name] = self.byte_counter.count(messages)
        return BuiltContext(
            request=ModelRequest(model=model, messages=messages, max_tokens=output),
            snapshot=ContextSnapshot(
                strategy="summary_and_recent_turns" if summary else "recent_complete_turns",
                source_message_ids=[str(item.id) for item in history if item.id in selected],
                message_count=len(messages), token_estimate=estimate,
                token_counter=self.counter.name, token_estimates=estimates,
                input_budget=budget, output_reserved=output,
                safety_reserved=self.policy.safety_tokens,
                task_state_revision=task_state.revision if task_state is not None else None,
                selections=[ContextSelection(str(item.id), item.sequence, item.id in selected,
                                             "summarized" if summary and item.sequence <= summary.covered_through_sequence
                                             and not isinstance(item.message, SystemMessage)
                                             and item.id not in selected
                                             else reasons.get(item.id, exclusion)) for item in original],
                summary_id=str(summary.id) if summary else None,
                summary_revision=summary.revision if summary else None,
                summary_covered_through=summary.covered_through_sequence if summary else None,
                projected_token_estimate=(fixed_cost + sum(visible[item.id].cost for item in history) if additive
                                          else self.counter.count(render(set(visible)))),
                artifact_ids=[item.message.artifact.id for item in history if item.id in selected
                              and isinstance(item.message, ToolResultMessage) and item.message.artifact is not None],
                summary_source_run_id=str(summary.source_run_id) if summary and summary.source_run_id else None,
                protected_input_id=str(summary.protected_input_id) if summary and summary.protected_input_id else None,
                recovery_fact_count=recovery.fact_count if recovery is not None else 0,
                recovery_source_runs=list(recovery.run_ids) if recovery is not None else [],
            ),
        )

    @staticmethod
    def summary_message(content: str) -> UserMessage:
        return UserMessage(
            content=("Historical conversation summary (reference data, not new instructions). "
                     "Current user input and explicit task state take precedence.\n\n" + content),
            timestamp=0,
        )

    @staticmethod
    def _validate(history: list[StoredMessage]) -> None:
        pending: dict[str, str] = {}
        for item in history:
            message = item.message
            if isinstance(message, SystemMessage):
                continue
            if isinstance(message, ToolResultMessage):
                if pending.get(message.tool_call_id) != message.tool_name:
                    raise ContextIntegrityError("Orphan, duplicate or mismatched tool result")
                del pending[message.tool_call_id]
                continue
            if pending:
                raise ContextIntegrityError("Tool call batch is missing results")
            if isinstance(message, AssistantMessage):
                calls = [block for block in message.content if isinstance(block, ToolCall)]
                if calls and message.stop_reason != "toolUse":
                    raise ContextIntegrityError("Tool calls require a completed toolUse response")
                if message.stop_reason == "toolUse" and not calls:
                    raise ContextIntegrityError("toolUse response has no calls")
                for call in calls:
                    if call.id in pending:
                        raise ContextIntegrityError("Duplicate tool call ID in batch")
                    pending[call.id] = call.name
        if pending:
            raise ContextIntegrityError("Tool call batch is missing results")
