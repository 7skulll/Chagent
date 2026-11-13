"""Pre-registration: what will be run, fixed before the first trial.

The plan's rule is a single sentence — thresholds, tolerance, budget, repeat count
and the human tie-break are fixed before running, and a candidate that fails does
not get a friendlier standard afterwards. This module makes that rule mechanical
in three ways.

**The protocol is hashed.** ``freeze`` writes the document with a digest over
everything except the digest itself. ``load`` recomputes it; a file that was
edited after the first trial fails to load, so "we changed the threshold because
the first run was noisy" is not a quiet option.

**The case set is bound by hash.** A protocol names each case at a version *and*
the hash of its document. Editing a case in place changes its hash, and the
protocol refuses to run against it: the case is no longer the question that was
preregistered.

**The slot count is fixed.** ``repeats × cases × arms`` is the number of trials
the experiment may contain. Re-running a red trial until it turns green is the
oldest form of self-deception available here, so the runner asks this module
whether a slot still exists before spending a model call.

Nothing here runs anything or reads results; ``report`` interprets trials, and
this module only says what was promised.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from datetime import datetime, timezone

from jsonschema import Draft202012Validator

from eval.manifests import sha256_json

SCHEMA_PATH = Path(__file__).with_name("schemas") / "protocol.schema.json"
PROTOCOL_FILE = "protocol.json"
#: A stability claim needs at least three observations per cell. Two identical
#: runs are two coin flips that landed the same way.
MIN_STABILITY_REPEATS = 3


class ProtocolInvalid(ValueError):
    pass


@dataclass(frozen=True)
class Arm:
    name: str
    provider: str
    model: str
    note: str = ""

    def to_json(self) -> dict:
        document = {"name": self.name, "provider": self.provider, "model": self.model}
        if self.note:
            document["note"] = self.note
        return document


@dataclass(frozen=True)
class Protocol:
    experiment: str
    question: str
    hypothesis: str
    created_at: str
    cases: tuple[dict, ...]
    arms: tuple[Arm, ...]
    repeats: int
    min_repeats_for_stability: int
    order: str
    budget: dict
    thresholds: dict
    analysis: str
    exclusions: str
    stopping_rule: str
    pricing: dict | None = None
    sha256: str = ""

    @property
    def baseline(self) -> Arm:
        return self.arms[0]

    @property
    def candidate(self) -> Arm:
        return self.arms[1]

    @property
    def slots(self) -> int:
        return len(self.cases) * len(self.arms) * self.repeats

    def case_key(self, case) -> str:
        return f"{case.id}@{case.version}"

    def to_json(self, *, with_hash: bool = True) -> dict:
        document = {
            "protocol_version": 1,
            "experiment": self.experiment,
            "question": self.question,
            "hypothesis": self.hypothesis,
            "created_at": self.created_at,
            "cases": list(self.cases),
            "arms": [arm.to_json() for arm in self.arms],
            "repeats": self.repeats,
            "min_repeats_for_stability": self.min_repeats_for_stability,
            "order": self.order,
            "budget": self.budget,
            "thresholds": self.thresholds,
            "analysis": self.analysis,
            "exclusions": self.exclusions,
            "stopping_rule": self.stopping_rule,
        }
        if self.pricing is not None:
            document["pricing"] = self.pricing
        if with_hash:
            document["sha256"] = self.sha256
        return document


def build(*, experiment: str, question: str, hypothesis: str, cases, arms,
          repeats: int, min_repeats_for_stability: int, order: str, budget: dict,
          thresholds: dict, analysis: str, exclusions: str, stopping_rule: str,
          pricing: dict | None = None, created_at: str | None = None) -> Protocol:
    document = {
        "protocol_version": 1,
        "experiment": experiment,
        "question": question,
        "hypothesis": hypothesis,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases": [{"id": case.id, "version": case.version, "sha256": case.sha256}
                  for case in cases],
        "arms": [arm.to_json() if isinstance(arm, Arm) else dict(arm) for arm in arms],
        "repeats": repeats,
        "min_repeats_for_stability": min_repeats_for_stability,
        "order": order,
        "budget": dict(budget),
        "thresholds": dict(thresholds),
        "analysis": analysis,
        "exclusions": exclusions,
        "stopping_rule": stopping_rule,
    }
    if pricing is not None:
        document["pricing"] = dict(pricing)
    _validate_document(document)
    protocol = Protocol(
        experiment=document["experiment"], question=document["question"],
        hypothesis=document["hypothesis"], created_at=document["created_at"],
        cases=tuple(document["cases"]), arms=tuple(Arm(**arm) for arm in document["arms"]),
        repeats=document["repeats"],
        min_repeats_for_stability=document["min_repeats_for_stability"],
        order=document["order"], budget=document["budget"],
        thresholds=document["thresholds"], analysis=document["analysis"],
        exclusions=document["exclusions"], stopping_rule=document["stopping_rule"],
        pricing=document.get("pricing"),
    )
    return replace(protocol, sha256=sha256_json(document))


def _validate_document(document: dict) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    problems = sorted(Draft202012Validator(schema).iter_errors(document),
                      key=lambda error: list(error.path))
    if problems:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
            for error in problems[:6])
        raise ProtocolInvalid(f"{len(problems)} protocol violation(s): {details}")
    if document["min_repeats_for_stability"] > document["repeats"]:
        raise ProtocolInvalid(
            "min_repeats_for_stability cannot exceed repeats: the protocol would "
            "declare that no result it allows could support a stability claim")
    if document["min_repeats_for_stability"] < MIN_STABILITY_REPEATS:
        raise ProtocolInvalid(
            f"min_repeats_for_stability must be at least {MIN_STABILITY_REPEATS}")
    if len(document['arms']) != 2:
        raise ProtocolInvalid('this comparison requires exactly baseline and candidate arms')
    names = [arm["name"] for arm in document["arms"]]
    if len(set(names)) != len(names):
        raise ProtocolInvalid("arm names must be unique")
    keys = [f"{case['id']}@{case['version']}" for case in document["cases"]]
    if len(set(keys)) != len(keys):
        raise ProtocolInvalid("a case appears twice in the protocol")


def freeze(protocol: Protocol, directory: Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / PROTOCOL_FILE
    document = protocol.to_json()
    from eval.integrity import publish

    try:
        return publish(path, document, identical_ok=True)
    except ValueError as error:
        raise ProtocolInvalid(str(error)) from error


def load(directory: Path) -> Protocol:
    path = Path(directory) / PROTOCOL_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolInvalid(f"{path}: {error}") from None
    stored = str(document.pop("sha256", ""))
    _validate_document(document)
    digest = sha256_json(document)
    if stored != digest:
        raise ProtocolInvalid(
            f"{path}: the protocol was edited after it was frozen "
            f"(recorded {stored[:12] or '<none>'}, now {digest[:12]}); "
            "a changed protocol is a new experiment, not a revised one")
    return Protocol(
        experiment=document["experiment"], question=document["question"],
        hypothesis=document["hypothesis"], created_at=document["created_at"],
        cases=tuple(document["cases"]), arms=tuple(Arm(**arm) for arm in document["arms"]),
        repeats=document["repeats"],
        min_repeats_for_stability=document["min_repeats_for_stability"],
        order=document["order"], budget=document["budget"],
        thresholds=document["thresholds"], analysis=document["analysis"],
        exclusions=document["exclusions"], stopping_rule=document["stopping_rule"],
        pricing=document.get("pricing"), sha256=digest,
    )


def check(protocol: Protocol, cases, arms) -> list[str]:
    """Every reason this protocol no longer describes what is on disk.

    All reasons are returned: fixing one edited case and re-running should not
    reveal a second one only after half the trials are spent.
    """
    problems: list[str] = []
    by_key = {f"{case.id}@{case.version}": case for case in cases}
    declared = {f"{case['id']}@{case['version']}": case for case in protocol.cases}
    for key, entry in declared.items():
        case = by_key.get(key)
        if case is None:
            problems.append(f"{key}: the protocol names a case that is not loaded")
        elif case.sha256 != entry["sha256"]:
            problems.append(f"{key}: case document changed since it was preregistered")
    for key in sorted(set(by_key) - set(declared)):
        problems.append(f"{key}: loaded but not preregistered; it is not part of this experiment")
    actual = {(arm.name, arm.provider, arm.model) for arm in arms}
    promised = {(arm.name, arm.provider, arm.model) for arm in protocol.arms}
    for entry in sorted(actual - promised):
        problems.append(f"arm {entry[0]} runs {entry[1]}/{entry[2]}, which was not preregistered")
    for entry in sorted(promised - actual):
        problems.append(f"arm {entry[0]} promised {entry[1]}/{entry[2]} but is not being run")
    return problems


def slots_used(protocol: Protocol, trials) -> dict:
    """How many trials each (case, arm) already has."""
    used: dict[tuple[str, str], int] = {}
    for trial in trials:
        if trial.experiment != protocol.experiment:
            continue
        key = (f"{trial.case_id}@{trial.case_version}", str(trial.manifest.get("arm", "")))
        used[key] = used.get(key, 0) + 1
    return used


def assert_slot(protocol: Protocol, case, arm: Arm, trials) -> int:
    """Return the repeat index for the next trial, or refuse to run another.

    Refusing is the point: an experiment that can always run one more trial has
    no fixed denominator, and its success rate can be improved by stopping when
    it looks good.
    """
    used = slots_used(protocol, trials)
    key = (protocol.case_key(case), arm.name)
    count = used.get(key, 0)
    if count >= protocol.repeats:
        raise ProtocolInvalid(
            f"{key[0]} / {key[1]} already has {count} trial(s); the protocol allows "
            f"{protocol.repeats}. A red trial is not re-run until it turns green — "
            "write a new protocol instead")
    return count


def describe(protocol: Protocol) -> str:
    lines = [
        f"experiment {protocol.experiment}  sha256={protocol.sha256[:12]}",
        f"question   {protocol.question}",
        f"hypothesis {protocol.hypothesis}",
        "arms       " + "; ".join(f"{arm.name}={arm.provider}/{arm.model}"
                                   for arm in protocol.arms),
        f"schedule   {len(protocol.cases)} case(s) × {len(protocol.arms)} arm(s) × "
        f"{protocol.repeats} repeat(s) = {protocol.slots} trial(s), {protocol.order}",
        f"stability  no claim below {protocol.min_repeats_for_stability} observation(s) per cell",
        f"budget     {protocol.budget}",
        f"thresholds {protocol.thresholds}",
        f"exclusions {protocol.exclusions}",
        f"stop       {protocol.stopping_rule}",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["Arm", "MIN_STABILITY_REPEATS", "PROTOCOL_FILE", "Protocol", "ProtocolInvalid",
           "assert_slot", "build", "check", "describe", "freeze", "load", "slots_used"]
