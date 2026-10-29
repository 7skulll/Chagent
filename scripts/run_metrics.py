"""Shared, provider-independent run metrics for diagnostic scripts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
from typing import Any


def _fingerprint(call: Mapping[str, Any]) -> str:
    arguments = call.get("arguments") or {}
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = repr(arguments)
    return f"{call.get('name')}\0{encoded}"


def _blocked(call: Mapping[str, Any]) -> bool:
    """True when the progress guard refused the call instead of executing it."""
    result = call.get("result")
    details = result.get("details") if isinstance(result, dict) else None
    return bool(isinstance(details, dict) and details.get("repeated"))


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value or {}, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return repr(value)


def summarize_tool_calls(calls: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Count calls and exact repeats without hiding their execution order.

    A repeated call is every occurrence after the first of the same tool name
    and normalized arguments. Different arguments, including a different edit
    to the same file, are deliberately not repeats.
    """
    rows = list(calls)
    occurrences: dict[str, int] = {}
    repeated = blocked = 0
    sequence = []
    for call in rows:
        fingerprint = _fingerprint(call)
        previous = occurrences.get(fingerprint, 0)
        is_repeated = previous > 0
        repeated += is_repeated
        occurrences[fingerprint] = previous + 1
        refused = _blocked(call)
        blocked += refused
        sequence.append({
            "call_index": call.get("call_index"),
            "name": call.get("name"),
            "arguments": _json_safe(call.get("arguments")),
            "status": call.get("status"),
            "repeated": is_repeated,
            "blocked": refused,
        })
    # ``repeated`` counts every repeat; ``blocked`` counts the ones the guard
    # refused. A run that repeated a call and was still allowed to verify after
    # a change shows repeats with blocked=0, which is the healthy case.
    return {"tool_calls": len(rows), "repeated_tool_calls": repeated,
            "blocked_tool_calls": blocked, "tool_sequence": sequence}
