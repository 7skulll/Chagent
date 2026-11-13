"""Comparing replayed state and error contracts without hiding what changed.

Two rules, both learned from the failures this project already had:

**Normalisation is declared, never inferred.** A changed UUID or a different
temporary root is not a behaviour change, but deciding that by pattern inside
the comparison makes every future difference arguable. Each allowance is named
in the scenario, listed in the output, and applied only when the value actually
has the declared shape. A value that looks like a UUID but was not declared is a
difference.

**Some fields may not be normalised at all.** ``status``, ``outcome``,
``exit_code``, ``retryable``, an approval ``decision`` or a result ``sha256`` are
the facts a replay exists to check. Allowing a scenario to erase them would turn
a passing comparison into a declaration that the check does not matter, so a
rule naming one of them is rejected when the rules are built, not when it is
convenient.

Error comparison has a third rule: **the message is not the contract.** P2
already refused to parse free-text errors into labels, for the same reason —
``PatchError: hunk 1 does not match`` is prose, and prose changes for cosmetic
reasons. The contract is which fields are set: failed, code, exit code, unknown
outcome. A scenario that wants the message compared says so.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

#: Declaration kinds, and the shape each one insists on before it applies.
#: Only two, on purpose: they are the non-semantic differences the recorded
#: evidence actually contains (identifiers and clock readings). A path rule would
#: need a declared root on *each* side to mean anything, which is a contract
#: question rather than a flag — so it is not offered as one.
NORMALIZERS = ("uuid", "timestamp")

#: Field suffixes that must never be normalised away. Matched segment-wise from
#: the end, so ``meta.error.code`` is protected by ``error.code``.
IRREDUCIBLE = (
    "status", "outcome", "outcome_unknown", "verdict", "exit_code",
    "decision", "retryable", "sha256", "failure_class", "error.code", "kind",
)

UUID_SHAPE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
FIELD_SEGMENT = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|\*)(?:\[[0-9*]+\])?$")
#: Differences print values, and a difference can be a file's whole text. The
#: bound keeps the operator's terminal readable without hiding that it differed.
EXCERPT = 120


class ContractInvalid(ValueError):
    """A comparison contract is unsound: it would hide a fact or match nothing."""


@dataclass(frozen=True)
class Normalization:
    """One declared allowance: ``field`` may differ as ``kind``."""

    field: str
    kind: str

    def __post_init__(self) -> None:
        if self.kind not in NORMALIZERS:
            raise ContractInvalid(f"unknown normalizer: {self.kind!r}")
        if not self.field or any(not FIELD_SEGMENT.fullmatch(part)
                                 for part in _segments(self.field)):
            raise ContractInvalid(f"invalid field pattern: {self.field!r}")

    def to_json(self) -> dict:
        return {"field": self.field, "kind": self.kind}


@dataclass(frozen=True)
class NormalizationRules:
    normalizations: tuple[Normalization, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "normalizations", tuple(self.normalizations))
        seen = set()
        for rule in self.normalizations:
            if _irreducible(rule.field):
                raise ContractInvalid(
                    f"{rule.field} may not be normalised: it is one of the facts the "
                    "comparison exists to check")
            if rule.field in seen:
                raise ContractInvalid(f"duplicate normalisation for {rule.field}")
            seen.add(rule.field)

    def find(self, path: str, *, segments=None) -> Normalization | None:
        """The most specific declaration covering this flattened path."""
        concrete = _concrete(path)
        best = None
        for rule in self.normalizations:
            matches = (_covers_tokens(rule.field, segments) if segments is not None
                       else _covers(rule.field, concrete))
            if matches and (
                    best is None or _specificity(rule.field) > _specificity(best.field)):
                best = rule
        return best

    def apply(self, path: str, value: Any, *, segments=None) -> tuple[Any, bool]:
        fields = tuple(part for part in segments if isinstance(part, str)) if segments else ()
        if any(fields[-len(name.split(".")):] == tuple(name.split(".")) for name in IRREDUCIBLE):
            return _excerpt(value), False
        rule = self.find(path, segments=segments)
        if rule is None:
            return _excerpt(value), False
        normalized = _normalize(rule, value)
        if normalized is None:
            # Declared, but this value does not have the declared shape. Leaving
            # it unnormalised makes it a difference, which is the honest result:
            # a rule is a statement about UUIDs, not a licence for anything.
            return _excerpt(value), False
        return normalized, True


@dataclass(frozen=True)
class Difference:
    path: str
    kind: str  # changed | missing | extra | type
    expected: Any = None
    actual: Any = None

    def to_json(self) -> dict:
        return {"path": self.path, "kind": self.kind,
                "expected": self.expected, "actual": self.actual}


@dataclass(frozen=True)
class Comparison:
    differences: tuple[Difference, ...] = ()
    normalized: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    compared: int = 0

    @property
    def equal(self) -> bool:
        return not self.differences

    def to_json(self) -> dict:
        return {
            "equal": self.equal,
            "compared": self.compared,
            "differences": [item.to_json() for item in self.differences],
            "normalized": list(self.normalized),
            "notes": list(self.notes),
        }


def compare(expected: Any, actual: Any, rules: NormalizationRules | None = None) -> Comparison:
    """Compare the tree itself; key spelling never becomes structural identity."""
    rules = rules or NormalizationRules()
    differences, normalized = [], []
    compared = 0

    def visit(want, have, tokens):
        nonlocal compared
        path = _display_path(tokens)
        both_numbers = (type(want) in (int, float) and type(have) in (int, float))
        if type(want) is not type(have) and not both_numbers:
            compared += 1
            differences.append(Difference(path, "type", _excerpt(want), _excerpt(have)))
            return
        if isinstance(want, dict):
            if not want and not have:
                compared += 1
            for key in sorted(set(want) | set(have)):
                child = tokens + (key,)
                if key not in have:
                    compared += 1
                    differences.append(Difference(_display_path(child), "missing", _excerpt(want[key])))
                elif key not in want:
                    compared += 1
                    differences.append(Difference(_display_path(child), "extra", None, _excerpt(have[key])))
                else:
                    visit(want[key], have[key], child)
            return
        if isinstance(want, list):
            if not want and not have:
                compared += 1
            for index in range(max(len(want), len(have))):
                child = tokens + (index,)
                if index >= len(have):
                    compared += 1
                    differences.append(Difference(_display_path(child), "missing", _excerpt(want[index])))
                elif index >= len(want):
                    compared += 1
                    differences.append(Difference(_display_path(child), "extra", None, _excerpt(have[index])))
                else:
                    visit(want[index], have[index], child)
            return
        compared += 1
        if want == have:
            return
        want_value, applied = rules.apply(path, want, segments=tokens)
        have_value, also_applied = rules.apply(path, have, segments=tokens)
        if applied and also_applied and want_value == have_value:
            normalized.append(path)
        else:
            differences.append(Difference(path, "changed", want_value, have_value))

    visit(expected, actual, ())
    return Comparison(tuple(differences), tuple(normalized), (), compared)


def _display_path(tokens) -> str:
    path = ""
    for token in tokens:
        if isinstance(token, int):
            path += f"[{token}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
            path += ("." if path else "") + token
        else:
            path += "[" + json.dumps(token, ensure_ascii=False) + "]"
    return path or "<root>"


def _covers_tokens(pattern: str, tokens) -> bool:
    parts = []
    for segment in _segments(pattern):
        match = re.fullmatch(r"([^\[]+)(?:\[([0-9]+|\*)\])?", segment)
        if match is None:
            return False
        parts.append((str, match[1]))
        if match[2] is not None:
            parts.append((int, match[2]))
    return len(parts) == len(tokens) and all(
        type(token) is kind and (wanted == "*" or str(token) == wanted)
        for (kind, wanted), token in zip(parts, tokens))


@dataclass(frozen=True)
class ErrorContract:
    """What a failed call promises, separated from what it said about it."""

    failed: bool
    code: str | None = None
    exit_code: int | None = None
    outcome_unknown: bool | None = None
    message: str = ""

    @classmethod
    def from_tool_result(cls, result: dict) -> "ErrorContract":
        details = result.get("details") if isinstance(result.get("details"), dict) else {}
        failure = details.get("failure") if isinstance(details.get("failure"), dict) else {}
        error = details.get("error") if isinstance(details.get("error"), dict) else {}
        messages = [block.get("text") for block in result.get("content") or ()
                    if isinstance(block, dict) and isinstance(block.get("text"), str)]
        return cls(
            failed=bool(result.get("is_error")),
            code=_first_text(details.get("failure_class"), failure.get("code"), error.get("code")),
            exit_code=details.get("exit_code") if isinstance(details.get("exit_code"), int) else None,
            # Present at the top level of a stored result and, for older rows, only
            # inside details; neither spelling is invented, the first one wins.
            outcome_unknown=(_flag(result.get("outcome_unknown"))
                             if _flag(result.get("outcome_unknown")) is not None
                             else _flag(details.get("outcome_unknown"))),
            message="\n".join(messages)[:EXCERPT],
        )

    def to_json(self) -> dict:
        return {"failed": self.failed, "code": self.code, "exit_code": self.exit_code,
                "outcome_unknown": self.outcome_unknown, "message": self.message}


def compare_error(expected: ErrorContract, actual: ErrorContract, *,
                  exact_message: bool = False) -> Comparison:
    """Compare the structured contract; the message only when asked for."""
    want = expected.to_json()
    have = actual.to_json()
    notes = []
    if not exact_message:
        want.pop("message")
        have.pop("message")
        notes.append("message text is recorded but not compared; pass exact_message to compare it")
    compared = compare(want, have)
    return Comparison(compared.differences, compared.normalized, tuple(notes), compared.compared)


def compare_state(expected: dict, actual: dict, rules: NormalizationRules | None = None) -> Comparison:
    """Compare two ``{relative path: facts}`` snapshots.

    Paths are keys, so a renamed file is a removal plus an addition rather than a
    rewrite of the same entry — which is what it is.
    """
    return compare({"files": expected}, {"files": actual}, rules)


def snapshot_facts(data: bytes | None, *, kind: str = "file") -> dict:
    """The facts a snapshot keeps about one path. Content stays a hash."""
    if kind != "file":
        return {"kind": kind}
    if data is None:
        return {"kind": "unreadable"}
    return {"kind": "file", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _segments(field: str) -> list[str]:
    return field.split(".")


def _concrete(path: str) -> list[str]:
    return [part for part in re.split(r"\.(?![^\[]*\])", path) if part]


def _covers(pattern: str, path: list[str]) -> bool:
    parts = _segments(pattern)
    if len(parts) != len(path):
        return False
    return all(_segment_matches(wanted, have) for wanted, have in zip(parts, path))


def _segment_matches(pattern: str, segment: str) -> bool:
    """``*`` and ``[*]`` are the only wildcards; nothing else is a pattern."""
    expression = []
    rest = pattern
    while rest:
        if rest.startswith("[*]"):
            expression.append(r"\[[0-9]+\]")
            rest = rest[3:]
        elif rest[0] == "*":
            expression.append("[^.]*")
            rest = rest[1:]
        else:
            expression.append(re.escape(rest[0]))
            rest = rest[1:]
    return re.fullmatch("".join(expression), segment) is not None


def _specificity(pattern: str) -> int:
    return len(_segments(pattern)) * 10 - _segments(pattern).count("*")


def _irreducible(field: str) -> bool:
    # A wildcard suffix can name a protected field just as an explicit name can.
    parts = _segments(field)
    for name in IRREDUCIBLE:
        suffix = name.split(".")
        if len(parts) >= len(suffix) and all(
                _segment_matches(pattern, concrete)
                for pattern, concrete in zip(parts[-len(suffix):], suffix)):
            return True
    return False


def _normalize(rule: Normalization, value: Any) -> Any | None:
    if rule.kind == "uuid":
        return "<uuid>" if isinstance(value, str) and UUID_SHAPE.match(value) else None
    if rule.kind == "timestamp":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return "<timestamp>"
        return None
    return None


def _excerpt(value: Any) -> Any:
    if isinstance(value, str) and len(value) > EXCERPT:
        return value[:EXCERPT] + "…"
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in ("true", "false"):
        return value == "true"
    return None


__all__ = [
    "Comparison", "ContractInvalid", "Difference", "ErrorContract", "IRREDUCIBLE",
    "NORMALIZERS", "Normalization", "NormalizationRules", "compare", "compare_error",
    "compare_state", "snapshot_facts",
]
