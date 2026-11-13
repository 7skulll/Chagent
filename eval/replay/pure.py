"""Pure logic: fixed input, hand-written expectation, no model and no database.

This tier exists because a regression in a projection or a classification is
invisible until it produces a wrong answer somewhere else. The vectors are
authored by hand from the module contract — not generated from a run — because a
snapshot of today's output can only ever prove "still the same", including the
same mistake. A vector that fails says which documented behaviour changed.

Nothing here executes a run, a command or a query. ``tool.echo`` is the only
target that goes through the tool machinery, and it is the one tool whose entire
contract is returning its input.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

from chagent.models import outbound
from chagent.tools.echo import EchoTool
from chagent.tools.retry import FAILURES

from eval.replay.contract import EXCERPT, compare

#: The fixed workspace a projection vector names. It does not exist, and it must
#: not: a real temporary root would change with the machine, and ``resolve``
#: would follow ``/tmp`` to ``/private/tmp`` on macOS and change the answer.
VECTOR_ROOT = "/srv/chagent-eval/ws"


class ReplayInvalid(ValueError):
    """A vector file does not describe a check this module can run."""


@dataclass(frozen=True)
class Vector:
    id: str
    target: str
    payload: dict
    expected: Any = None
    expect_error: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class VectorResult:
    id: str
    target: str
    ok: bool
    expected: Any
    actual: Any = None
    error: str | None = None
    notes: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "target": self.target, "ok": self.ok,
                "expected": _bounded(self.expected), "actual": _bounded(self.actual),
                "error": self.error, "notes": self.notes}


def _to_model(payload: dict) -> str:
    text = _text(payload, "text")
    return outbound.to_model(text, Path(payload.get("workspace") or VECTOR_ROOT))


def _to_host(payload: dict) -> str:
    text = _text(payload, "text")
    return outbound.to_host(text, Path(payload.get("workspace") or VECTOR_ROOT))


def _invocation_to_host(payload: dict) -> dict:
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        raise ReplayInvalid("invocation_to_host needs an arguments object")
    return outbound.invocation_to_host(dict(arguments), Path(payload.get("workspace") or VECTOR_ROOT))


def _protect_payload(payload: dict) -> Any:
    if "payload" not in payload:
        raise ReplayInvalid("protect_payload needs a payload")
    return outbound.protect_payload(payload["payload"], Path(payload.get("workspace") or VECTOR_ROOT))


def _retry_table(_payload: dict) -> dict:
    return {code: failure.retryable for code, failure in sorted(FAILURES.items())}


def _echo(payload: dict) -> str:
    result = asyncio.run(EchoTool().execute("replay-vector", dict(payload)))
    texts = [getattr(block, "text", "") for block in getattr(result, "content", ()) or ()]
    return "\n".join(texts)


@dataclass(frozen=True)
class Target:
    id: str
    description: str
    run: Callable[[dict], Any]


TARGETS: dict[str, Target] = {
    "outbound.to_model": Target(
        "outbound.to_model", "workspace paths are projected before a request leaves", _to_model),
    "outbound.to_host": Target(
        "outbound.to_host", "virtual roots are resolved before execution", _to_host),
    "outbound.invocation_to_host": Target(
        "outbound.invocation_to_host", "path-bearing arguments are converted once", _invocation_to_host),
    "outbound.protect_payload": Target(
        "outbound.protect_payload", "an unmapped workspace path is refused, not sent", _protect_payload),
    "retry.table": Target(
        "retry.table", "which failures may be tried again", _retry_table),
    "tool.echo": Target(
        "tool.echo", "the one tool with no state, executed through the real tool path", _echo),
}

VECTORS_PATH = Path(__file__).with_name("vectors") / "pure.json"


def load_vectors(path: Path = VECTORS_PATH) -> list[Vector]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReplayInvalid(f"{path}: {error}") from None
    if not isinstance(document, dict) or not isinstance(document.get("vectors"), list):
        raise ReplayInvalid(f"{path}: needs a vectors array")
    vectors = []
    seen = set()
    for index, raw in enumerate(document["vectors"]):
        if not isinstance(raw, dict):
            raise ReplayInvalid(f"{path}: vector {index} is not an object")
        vector_id = raw.get("id")
        if not isinstance(vector_id, str) or not vector_id:
            raise ReplayInvalid(f"{path}: vector {index} has no id")
        if vector_id in seen:
            raise ReplayInvalid(f"{path}: duplicate vector id {vector_id}")
        seen.add(vector_id)
        target = raw.get("target")
        if target not in TARGETS:
            raise ReplayInvalid(f"{path}: {vector_id} names unknown target {target!r}")
        payload = raw.get("input")
        if not isinstance(payload, dict):
            raise ReplayInvalid(f"{path}: {vector_id} needs an input object")
        has_expected = "expected" in raw
        error = raw.get("expect_error")
        if has_expected == (error is not None):
            raise ReplayInvalid(f"{path}: {vector_id} needs exactly one of expected or expect_error")
        vectors.append(Vector(vector_id, target, payload, raw.get("expected"), error,
                              str(raw.get("notes", ""))))
    if not vectors:
        raise ReplayInvalid(f"{path}: no vectors")
    return vectors


def run(vectors) -> list[VectorResult]:
    results = []
    for vector in vectors:
        target = TARGETS[vector.target]
        try:
            actual = target.run(dict(vector.payload))
        except Exception as error:  # the exception type is part of the contract
            name = type(error).__name__
            ok = vector.expect_error == name
            results.append(VectorResult(vector.id, vector.target, ok, vector.expect_error,
                                        None, name if not ok else None, vector.notes))
            continue
        if vector.expect_error is not None:
            results.append(VectorResult(vector.id, vector.target, False, vector.expect_error,
                                        _bounded(actual), "expected an error, got a value",
                                        vector.notes))
            continue
        ok = compare(vector.expected, actual).equal
        results.append(VectorResult(vector.id, vector.target, ok, vector.expected,
                                    actual if not ok else None, None, vector.notes))
    return results


def format_results(results) -> str:
    passed = sum(1 for result in results if result.ok)
    lines = [f"{passed}/{len(results)} pure vector(s) match"]
    for result in results:
        if result.ok:
            continue
        lines.append(f"  {result.id}  [{result.target}]")
        if result.error:
            lines.append(f"    {result.error}")
        else:
            lines.append(f"    expected {result.expected!r}")
            lines.append(f"    actual   {result.actual!r}")
        if result.notes:
            lines.append(f"    contract: {result.notes}")
    return "\n".join(lines) + "\n"


def report(results) -> dict:
    return {
        "vector_version": 1,
        "vectors": len(results),
        "passed": sum(1 for result in results if result.ok),
        "results": [result.to_json() for result in results],
    }


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ReplayInvalid(f"vector input needs a {key} string")
    return value


def _bounded(value: Any) -> Any:
    if isinstance(value, str) and len(value) > EXCERPT:
        return value[:EXCERPT] + "…"
    return value


__all__ = [
    "TARGETS", "VECTORS_PATH", "VECTOR_ROOT", "ReplayInvalid", "Vector",
    "VectorResult", "format_results", "load_vectors", "report", "run",
]
