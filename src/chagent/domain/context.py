"""Context policy and inspectable selection results, separate from transcript storage."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    output_tokens: int | None = None
    safety_tokens: int = 256
    max_history_messages: int = 200

    def __post_init__(self):
        if self.output_tokens is not None and self.output_tokens < 1:
            raise ValueError("Output token reservation must be positive")
        if self.safety_tokens < 0 or self.max_history_messages < 1:
            raise ValueError("Safety margin must be nonnegative and history limit positive")


@dataclass(slots=True)
class ContextSelection:
    message_id: str
    sequence: int
    selected: bool
    reason: str


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
