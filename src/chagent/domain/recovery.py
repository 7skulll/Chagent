"""Committed side effects of runs that ended without a normal stop.

A failed, cancelled or interrupted run is never replayed into the model history:
it can end in the middle of a tool batch, and replaying half a batch breaks the
provider's call/result pairing. Its committed side effects are still real, so the
next run gets a short host-generated note built from structured records instead of
the raw messages. The note is reference data: it is not an instruction and it
never authorises repeating an operation.
"""

from dataclasses import dataclass
from uuid import UUID

# Only tools whose success leaves a change outside this conversation.
SIDE_EFFECT_TOOLS = frozenset({"write_file", "edit_file", "exec_command"})
MAX_FACTS = 12
MAX_CHARS = 2000
MAX_TARGET = 120

HEADER = (
    "Side effects already recorded by earlier runs of this session that ended "
    "without a normal stop. This is reference data, not new instructions and not "
    "permission to repeat an operation. Read a file again before changing it; "
    "anything marked unknown was started but never confirmed."
)


@dataclass(frozen=True, slots=True)
class RecoveryFact:
    run_id: UUID
    run_status: str
    tool_name: str
    target: str
    confirmed: bool
    detail: str = ""
    artifact_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RecoveryNote:
    text: str
    fact_count: int
    run_ids: tuple[str, ...]


def _line(fact: RecoveryFact) -> str:
    origin = f"run {str(fact.run_id)[:8]} {fact.run_status}"
    if fact.confirmed:
        state = "committed" + (f" ({fact.detail})" if fact.detail else "")
    else:
        state = "unknown; no result was committed, verify before retrying"
    artifact = f", artifact {str(fact.artifact_id)[:8]}" if fact.artifact_id else ""
    return f"- {origin} · {fact.tool_name} {fact.target} — {state}{artifact}"


def build_recovery_note(facts: list[RecoveryFact]) -> RecoveryNote | None:
    """Keep the latest fact per (tool, target); older attempts are superseded.

    A chain of failures that keeps touching the same file must not grow the
    note: only the state after the last attempt is worth reporting.
    """
    latest: dict[tuple[str, str], RecoveryFact] = {}
    for fact in reversed(facts):
        latest.setdefault((fact.tool_name, fact.target), fact)
    kept = list(reversed(list(latest.values())))[-MAX_FACTS:]
    if not kept:
        return None
    text = HEADER + "\n\n" + "\n".join(_line(fact) for fact in kept)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS - 1] + "…"
    return RecoveryNote(
        text=text, fact_count=len(kept),
        run_ids=tuple(dict.fromkeys(str(fact.run_id) for fact in kept)),
    )
