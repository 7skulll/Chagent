"""Versioned context checkpoints; original transcript messages remain unchanged."""

from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Mapping
import math
import os
from uuid import UUID

from chagent.domain.usage import Usage


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    enabled: bool = True
    trigger_ratio: float = 0.8
    keep_recent_ratio: float = 0.5
    max_summary_tokens: int = 2048
    max_calls: int = 8
    timeout: float = 60
    max_compactions_per_run: int = 4

    def __post_init__(self):
        if not 0 < self.keep_recent_ratio < self.trigger_ratio < 1:
            raise ValueError("Compaction ratios require 0 < keep_recent < trigger < 1")
        if self.max_summary_tokens < 1 or not 1 <= self.max_calls <= 32:
            raise ValueError("Invalid compaction limits")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Compaction timeout must be finite and positive")
        if not 1 <= self.max_compactions_per_run <= 32:
            raise ValueError("Invalid per-run compaction limit")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CompactionPolicy":
        values = os.environ if env is None else env
        enabled = values.get("CHAGENT_COMPACTION_ENABLED", "true").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("CHAGENT_COMPACTION_ENABLED must be true or false")
        try:
            return cls(enabled=enabled == "true",
                       trigger_ratio=float(values.get("CHAGENT_COMPACTION_TRIGGER", "0.8")),
                       keep_recent_ratio=float(values.get("CHAGENT_COMPACTION_KEEP_RECENT", "0.5")),
                       max_summary_tokens=int(values.get("CHAGENT_COMPACTION_SUMMARY_TOKENS", "2048")),
                       max_calls=int(values.get("CHAGENT_COMPACTION_MAX_CALLS", "8")),
                       timeout=float(values.get("CHAGENT_COMPACTION_TIMEOUT", "60")),
                       max_compactions_per_run=int(values.get("CHAGENT_COMPACTION_MAX_PER_RUN", "4")))
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
