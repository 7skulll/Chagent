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
    repeated = 0
    sequence = []
    for call in rows:
        fingerprint = _fingerprint(call)
        previous = occurrences.get(fingerprint, 0)
        is_repeated = previous > 0
        repeated += is_repeated
        occurrences[fingerprint] = previous + 1
        sequence.append({
            "call_index": call.get("call_index"),
            "name": call.get("name"),
            "arguments": _json_safe(call.get("arguments")),
            "status": call.get("status"),
            "repeated": is_repeated,
        })
    return {"tool_calls": len(rows), "repeated_tool_calls": repeated,
            "tool_sequence": sequence}
