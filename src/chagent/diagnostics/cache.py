"""Audit prompt-cache reuse from recorded requests; sends nothing to a provider.

A provider's prefix cache serves only the bytes that match the previous request
exactly, in prompt order: system text, then tool declarations, then messages one
by one. Rebuilding the payload from ``model_calls.request`` therefore answers
three questions without any traffic:

1. ``hit``: how much of each prompt the provider reported as cached;
2. ``reusable``: of the bytes sent after the first turn, how much overlapped
   the previous request;
3. ``breaks``: for a request that could not reuse its predecessor, which
   section diverged first.

``hit`` and ``reusable`` disagree by design. ``hit`` is the provider's number and
is authoritative; ``reusable`` is our local estimate of the same quantity and
only exists so a drop can be attributed to a section. A run is at its ceiling
when ``hit`` tracks ``reusable``: the only uncached tokens are the ones never
sent before.

Reconstruction covers the transcript, not provider-specific replay fields such
as ``reasoning_content``. That keeps the audit deterministic and provider-free;
it can understate ``reusable`` slightly for adapters that replay reasoning.
"""

import json
from dataclasses import dataclass

from chagent.domain.codec import message_from_dict
from chagent.models.base import Model, ModelRequest
from chagent.models.transcript import anthropic_payload, openai_payload


def payload_of(row) -> dict:
    """Rebuild what the adapter sends for this call from the recorded request."""
    request = row["request"]
    built = ModelRequest(
        model=Model(**request["model"]),
        messages=[message_from_dict(item) for item in request["messages"]],
        max_tokens=request.get("max_tokens"), temperature=request.get("temperature"),
        purpose=request.get("purpose", "task"),
    )
    if built.model.api == "anthropic-messages":
        return anthropic_payload(built)
    return openai_payload(built)


def sections(payload: dict) -> list[tuple[str, str]]:
    """The request in prompt order, as labelled chunks with stable names.

    The chat-completions adapter carries the system prompt as ``messages[0]``
    while the Anthropic one uses a top-level key. Both are folded into one
    ``system`` chunk so a break is attributed the same way for either adapter.
    """
    system = payload.get("system")
    messages = list(payload.get("messages") or [])
    if not system and messages and messages[0].get("role") == "system":
        content = messages.pop(0).get("content")
        system = content if isinstance(content, str) else json.dumps(
            content, ensure_ascii=False, sort_keys=True)
    chunks = [("system", system or "")]
    chunks.append(("tools", json.dumps(payload.get("tools"), ensure_ascii=False, sort_keys=True)))
    for index, message in enumerate(messages):
        chunks.append((f"message #{index} ({message.get('role')})",
                       json.dumps(message, ensure_ascii=False, sort_keys=True)))
    return chunks


@dataclass(frozen=True, slots=True)
class Prefix:
    """How far one request reuses its predecessor, and what stopped it."""

    shared_chunks: int
    shared_bytes: int
    total_bytes: int
    break_section: str | None
    lost_bytes: int


def compare(previous: list[tuple[str, str]], current: list[tuple[str, str]]) -> Prefix:
    shared = 0
    for (_, left), (_, right) in zip(previous, current):
        if left != right:
            break
        shared += 1
    shared_bytes = sum(len(text.encode()) for _, text in current[:shared])
    total = sum(len(text.encode()) for _, text in current)
    # A shorter request (compaction dropped old turns) reuses everything it kept;
    # only a same-position mismatch is a break.
    label = current[shared][0] if shared < len(previous) and shared < len(current) else None
    return Prefix(shared, shared_bytes, total, label, total - shared_bytes if label else 0)


def audit(rows) -> dict:
    """Per-turn cache facts plus aggregates. Rows must be in iteration order."""
    turns, previous = [], None
    for row in rows:
        usage = (row["response"] or {}).get("usage") or {}
        cached = usage.get("cache_read") or 0
        prompt = (usage.get("input") or 0) + cached
        current = sections(payload_of(row))
        entry = {
            "iteration": row["iteration"],
            "messages": sum(1 for label, _ in current if label.startswith("message")),
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "cache_hit": round(cached / prompt, 4) if prompt else None,
            "reusable": None,
            "break": None,
            "shared_bytes": 0,
            "total_bytes": sum(len(text.encode()) for _, text in current),
        }
        if previous is not None:
            prefix = compare(previous, current)
            entry["reusable"] = round(prefix.shared_bytes / prefix.total_bytes, 4) if prefix.total_bytes else None
            entry["break"] = prefix.break_section
            entry["shared_bytes"] = prefix.shared_bytes
            entry["lost_bytes"] = prefix.lost_bytes
        turns.append(entry)
        previous = current
    prompt_total = sum(turn["prompt_tokens"] for turn in turns)
    cached_total = sum(turn["cached_tokens"] for turn in turns)
    # The first turn has no predecessor, so its bytes could never be reused.
    # Counting them would make every run look worse the longer its opening prompt.
    pairs = [turn for turn in turns if turn["reusable"] is not None]
    shared_bytes = sum(turn["shared_bytes"] for turn in pairs)
    total_bytes = sum(turn["total_bytes"] for turn in pairs)
    return {
        "turns": turns,
        "prompt_tokens": prompt_total,
        "cached_tokens": cached_total,
        "shared_bytes": shared_bytes,
        "total_bytes": total_bytes,
        "hit": round(cached_total / prompt_total, 4) if prompt_total else None,
        "reusable": round(shared_bytes / total_bytes, 4) if total_bytes else None,
        "breaks": [{"iteration": turn["iteration"], "section": turn["break"],
                    "lost_bytes": turn.get("lost_bytes", 0)}
                   for turn in turns if turn["break"]],
    }
