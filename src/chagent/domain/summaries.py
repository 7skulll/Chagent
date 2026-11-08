"""Versioned context checkpoints; original transcript messages remain unchanged."""

from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Mapping
import math
import os
from uuid import UUID

from chagent.domain.usage import Usage


#: How the host-authored checkpoint may be used. See ``CompactionPolicy.fresh_window``.
FRESH_WINDOW_OFF = "off"
FRESH_WINDOW_FALLBACK = "fallback"
FRESH_WINDOW_ALWAYS = "always"
FRESH_WINDOW_MODES = frozenset({FRESH_WINDOW_OFF, FRESH_WINDOW_FALLBACK, FRESH_WINDOW_ALWAYS})


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    enabled: bool = True
    trigger_ratio: float = 0.8
    keep_recent_ratio: float = 0.5
    max_summary_tokens: int = 2048
    max_calls: int = 8
    timeout: float = 60
    max_compactions_per_run: int = 4
    # Newest tool results that must stay verbatim, never summarized: they are the
    # evidence the model just acted on. 0 restores summarize-everything.
    retained_tool_results: int = 2
    # Newest user requests carried verbatim inside the checkpoint. The summarizer
    # never sees them, so a later generation copies the words instead of
    # paraphrasing an earlier paraphrase. 0 turns the block off.
    retained_user_requests: int = 4
    # Compact after the run has already answered, while nothing is waiting on it:
    # the next request then starts smaller instead of paying the summary on its
    # critical path. Off by default because it spends calls the user did not ask
    # for; enable with CHAGENT_COMPACTION_POST_TURN.
    post_turn: bool = False
    # Where post-turn work becomes worth paying for, as a fraction of the input
    # budget. Below ``trigger_ratio`` so it fires before the next run is forced
    # to compact, and next to ``speculative_trigger`` because both answer the
    # same question: is a checkpoint already overdue?
    post_turn_trigger: float = 0.6
    # Speculative compaction: pay for a summary before the budget forces it, so
    # the pause happens while the model is busy with tools instead of between the
    # tool result and the next request. A candidate is computed below the trigger
    # and adopted later; one that is never adopted costs its calls and nothing else.
    speculative: bool = True
    # Where a speculative run starts, as a fraction of the input budget. It must be
    # below ``trigger_ratio`` or the synchronous path would already have fired.
    speculative_trigger: float = 0.6
    # How long run teardown waits for a cancelled speculative request to unwind.
    speculative_shutdown_timeout: float = 5.0
    # Second compaction implementation: a host-authored checkpoint that drops the
    # span instead of summarizing it. "off" never uses it, "fallback" uses it when
    # the summarizer fails, "always" skips the summarizer entirely.
    fresh_window: str = "fallback"

    def __post_init__(self):
        if not 0 < self.keep_recent_ratio < self.trigger_ratio < 1:
            raise ValueError("Compaction ratios require 0 < keep_recent < trigger < 1")
        if self.max_summary_tokens < 1 or not 1 <= self.max_calls <= 32:
            raise ValueError("Invalid compaction limits")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Compaction timeout must be finite and positive")
        if not 1 <= self.max_compactions_per_run <= 32:
            raise ValueError("Invalid per-run compaction limit")
        if isinstance(self.retained_tool_results, bool) or not 0 <= self.retained_tool_results <= 64:
            raise ValueError("Retained tool results must be an integer between 0 and 64")
        if isinstance(self.retained_user_requests, bool) or not 0 <= self.retained_user_requests <= 64:
            raise ValueError("Retained user requests must be an integer between 0 and 64")
        if not 0 < self.speculative_trigger < self.trigger_ratio:
            raise ValueError("Speculative trigger must be positive and below the compaction trigger")
        if not 0 < self.post_turn_trigger <= self.trigger_ratio:
            raise ValueError("Post-turn trigger must be positive and not above the compaction trigger")
        if not math.isfinite(self.speculative_shutdown_timeout) or self.speculative_shutdown_timeout <= 0:
            raise ValueError("Speculative shutdown timeout must be finite and positive")
        if self.fresh_window not in FRESH_WINDOW_MODES:
            raise ValueError("Compaction fresh window must be off, fallback or always")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CompactionPolicy":
        values = os.environ if env is None else env
        enabled = values.get("CHAGENT_COMPACTION_ENABLED", "true").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("CHAGENT_COMPACTION_ENABLED must be true or false")
        speculative = values.get("CHAGENT_COMPACTION_SPECULATIVE", "true").lower()
        if speculative not in {"true", "false"}:
            raise ValueError("CHAGENT_COMPACTION_SPECULATIVE must be true or false")
        post_turn = values.get("CHAGENT_COMPACTION_POST_TURN", "false").lower()
        if post_turn not in {"true", "false"}:
            raise ValueError("CHAGENT_COMPACTION_POST_TURN must be true or false")
        fresh_window = values.get("CHAGENT_COMPACTION_FRESH_WINDOW", "fallback").strip().lower()
        if fresh_window not in FRESH_WINDOW_MODES:
            raise ValueError("CHAGENT_COMPACTION_FRESH_WINDOW must be off, fallback or always")
        try:
            # Parsed first because the post-turn default is relative to it: an
            # absent value must never produce a policy its own constructor rejects.
            trigger_ratio = float(values.get("CHAGENT_COMPACTION_TRIGGER", "0.8"))
            return cls(enabled=enabled == "true",
                       trigger_ratio=trigger_ratio,
                       keep_recent_ratio=float(values.get("CHAGENT_COMPACTION_KEEP_RECENT", "0.5")),
                       max_summary_tokens=int(values.get("CHAGENT_COMPACTION_SUMMARY_TOKENS", "2048")),
                       max_calls=int(values.get("CHAGENT_COMPACTION_MAX_CALLS", "8")),
                       timeout=float(values.get("CHAGENT_COMPACTION_TIMEOUT", "60")),
                       max_compactions_per_run=int(values.get("CHAGENT_COMPACTION_MAX_PER_RUN", "4")),
                       retained_tool_results=int(values.get("CHAGENT_COMPACTION_RETAINED_TOOL_RESULTS", "2")),
                       retained_user_requests=int(values.get("CHAGENT_COMPACTION_RETAINED_USER_REQUESTS", "4")),
                       speculative=speculative == "true",
                       speculative_trigger=float(values.get("CHAGENT_COMPACTION_SPECULATIVE_TRIGGER", "0.6")),
                       speculative_shutdown_timeout=float(values.get("CHAGENT_COMPACTION_SPECULATIVE_TIMEOUT", "5")),
                       post_turn=post_turn == "true",
                       post_turn_trigger=float(values.get(
                           "CHAGENT_COMPACTION_POST_TURN_TRIGGER", min(0.6, trigger_ratio))),
                       fresh_window=fresh_window)
        except (ValueError, OverflowError):
            raise ValueError("Invalid compaction configuration") from None


@dataclass(slots=True)
class SessionSummary:
    id: UUID
    session_id: UUID
    revision: int
    previous_summary_id: UUID | None
    covered_through_sequence: int
    first_kept_message_id: UUID
    content: str
    model: str
    provider: str
    prompt_version: str
    compaction_id: UUID
    source_message_ids: list[str] = field(default_factory=list)
    usage: Usage | None = None
    created_at: datetime | None = None
    source_run_id: UUID | None = None
    protected_input_id: UUID | None = None
