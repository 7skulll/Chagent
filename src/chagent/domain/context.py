"""Context policy and inspectable selection results, separate from transcript storage."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import os


#: Model-visible cap for one tool result, in tokens. Over the cap the projection
#: keeps head and tail and marks the middle omitted; the stored result is intact.
#: The storage layer independently turns results over 8000 bytes into artifacts,
#: which carry a bounded excerpt and stay readable through ``read_artifact``.
DEFAULT_MAX_TOOL_RESULT_TOKENS = 2000

#: Ceiling on the output tokens reserved out of the context window when the caller
#: does not name one. Calibrated on 319 real calls (2026-11-06): p50 98, p90 579,
#: p95 1055, p99 3054, max 5140, and **zero ``output_limit`` failures** under the
#: previous 16384 reservation. 8192 keeps a 1.6x margin over the longest answer
#: ever produced while handing half of a 16k reservation back to the input budget
#: — a quarter of the window is a lot to hold for an answer that is usually 98
#: tokens long. A cap that turns out too small is reported as ``output_limit``
#: instead of being silently accepted, so the cost of guessing low is one failed
#: turn, not a truncated artifact.
DEFAULT_OUTPUT_RESERVATION = 8192


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    output_tokens: int | None = None
    safety_tokens: int = 256
    max_history_messages: int = 200
    max_tool_result_tokens: int = DEFAULT_MAX_TOOL_RESULT_TOKENS

    def __post_init__(self):
        if self.output_tokens is not None and self.output_tokens < 1:
            raise ValueError("Output token reservation must be positive")
        if self.safety_tokens < 0 or self.max_history_messages < 1:
            raise ValueError("Safety margin must be nonnegative and history limit positive")
        if not 1 <= self.max_tool_result_tokens <= 1_000_000:
            raise ValueError("Tool result token limit must be between 1 and 1000000")

    def reservation(self, model) -> int:
        """Output tokens held back from the window for this model.

        A chat-completions provider enforces ``input + output <= window``, so the
        amount reserved must be exactly what is requested: there is no way to ask
        for 16k and plan against 4k. This is therefore also the request cap.
        """
        return self.output_tokens or min(model.max_tokens, DEFAULT_OUTPUT_RESERVATION)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ContextPolicy":
        """Read the two budget knobs that change the size of a request.

        Both were previously only settable in code, which made them invisible in
        ``config show`` and in the snapshot's ``output_reserved``.
        """
        values = os.environ if env is None else env
        defaults = cls()
        try:
            output = values.get("CHAGENT_OUTPUT_TOKENS", "").strip()
            tool_result = values.get("CHAGENT_TOOL_RESULT_TOKENS", "").strip()
            return cls(
                output_tokens=int(output) if output else defaults.output_tokens,
                safety_tokens=defaults.safety_tokens,
                max_history_messages=defaults.max_history_messages,
                max_tool_result_tokens=int(tool_result) if tool_result else defaults.max_tool_result_tokens,
            )
        except (ValueError, OverflowError):
            raise ValueError("Invalid context budget configuration") from None


@dataclass(slots=True)
class ContextSelection:
    message_id: str
    sequence: int
    selected: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ObservedUsage:
    """What the provider actually charged for the last prompt in this run.

    ``prompt_tokens`` is the provider's own total for the request that produced
    ``after_sequence``: prompt plus completion. The completion becomes part of
    the next prompt, so the pair is the exact size of everything up to and
    including that assistant message. Only messages after it need estimating.

    ``input_tokens`` is the prompt half alone, paired with
    ``coarse_prompt_tokens`` — what the heuristic said about the same request.
    Their ratio is how far the heuristic reads off on this content, and it is used
    to correct turn selection: without it, selection would keep using an
    under-estimate while the final check uses the exact number, and a
    budget-maximal selection would be rejected by the very anchor that should
    have saved it.

    Every count here excludes memory: the recalled reference is re-added by
    ``MemoryService.enrich`` on every turn, and counting it twice would inflate
    the budget by its own size. A structural change to the prompt (different
    tools, an installed summary) invalidates the anchor instead of adjusting it.
    """

    prompt_tokens: int
    input_tokens: int
    after_sequence: int
    coarse_prompt_tokens: int = 0

    def __post_init__(self):
        if self.prompt_tokens < 1:
            raise ValueError("Observed prompt tokens must be positive")

    @property
    def scale(self) -> float:
        """How many real tokens the heuristic reads as one, bounded to tighten only.

        The provider's number is used to *shrink* what selection admits, never to
        enlarge it: loosening a budget on the strength of a heuristic that just
        disagreed with the provider is how a request overflows. A ratio below 1
        therefore reads as 1, and the upper bound stops one odd response (a
        provider counting reasoning into its input, a partially cached request)
        from collapsing the budget.
        """
        if self.coarse_prompt_tokens <= 0 or self.input_tokens <= 0:
            return 1.0
        return min(3.0, max(1.0, self.input_tokens / self.coarse_prompt_tokens))


@dataclass(slots=True)
class ContextSnapshot:
    strategy: str
    source_message_ids: list[str]
    message_count: int
    token_estimate: int
    token_counter: str
    input_budget: int
    output_reserved: int
    safety_reserved: int
    task_state_revision: int | None
    selections: list[ContextSelection] = field(default_factory=list)
    # Every available estimate under its own counter name; the primary one is
    # ``token_counter``. They are recorded side by side, never averaged.
    token_estimates: dict[str, int] = field(default_factory=dict)
    summary_id: str | None = None
    summary_revision: int | None = None
    summary_covered_through: int | None = None
    projected_token_estimate: int = 0
    artifact_ids: list[str] = field(default_factory=list)
    summary_source_run_id: str | None = None
    protected_input_id: str | None = None
    memory_references: list[dict] = field(default_factory=list)
    memory_token_estimate: int = 0
    memory_omitted: int = 0
    recovery_fact_count: int = 0
    recovery_source_runs: list[str] = field(default_factory=list)
    # ``token_estimate`` is authoritative for the budget. ``estimate_source``
    # says whether it came from the provider's own count plus an estimate of the
    # appended messages, or from the estimate alone.
    estimate_source: str = "coarse"
    observed_prompt_tokens: int | None = None
    tail_token_estimate: int | None = None
    # What the heuristic alone said about this request, excluding memory. The
    # difference against ``observed_prompt_tokens`` is what ``scale`` measures.
    coarse_token_estimate: int = 0
    # Characters the per-result cap removed from tool results the model saw.
    truncated_tool_results: int = 0
