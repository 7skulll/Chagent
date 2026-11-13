"""Human labels, their separation from tuning, and an honest report.

Section 5 asks for three things this module has to make possible rather than
merely promise: saved labels with their evidence, disagreement kept rather than
averaged away, and a per-label report of precision/recall **or** the reason it
cannot be estimated. Two structural rules follow.

**A sample belongs to exactly one dataset.** Tuning on the samples you report
is how a rule set gets a reputation it has not earned, so ``tune`` and
``validation`` are exclusive per sample and the check happens on write, not in
a review checklist. Nothing here can stop somebody re-labelling a sample in a
second file, so the file is append-only and one file is the ledger.

**Absence of a number is reported, not filled in.** Zero validation samples for
a label produces ``value: null`` with a reason, never ``1.0``, ``0.0`` or a
count of how many failures the rule hit. A rule that fires on every failure has
measured nothing about precision.

Verdicts are about the label, not about the run: ``not_a_defect`` is a real
annotation (a failing call may still be correct behaviour), and a sample can
carry several labels.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path

from eval.attribute import CERTAINTIES, LABELS, RULE_VERSION, valid_reference

DATASETS = ("tune", "validation")
VERDICTS = ("clue", "root_cause", "not_a_defect")

#: Where labels live by default. Committed, because a curated human judgement is
#: an asset; trial output is not (see ``gitignore`` for ``results/``).
LEDGER = Path(__file__).resolve().parent / "annotations" / "labels.jsonl"


class AnnotationInvalid(ValueError):
    """The record cannot be stored as written."""


@dataclass(frozen=True, slots=True)
class Annotation:
    sample_id: str
    label: str
    verdict: str
    dataset: str
    reviewer: str
    certainty: str
    evidence: tuple[str, ...] = ()
    note: str = ""
    review_version: int = 1
    run_id: str | None = None
    recorded_at: str | None = None

    def to_json(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "label": self.label,
            "verdict": self.verdict,
            "dataset": self.dataset,
            "reviewer": self.reviewer,
            "certainty": self.certainty,
            "evidence": list(self.evidence),
            "note": self.note,
            "review_version": self.review_version,
            "run_id": self.run_id,
            "recorded_at": self.recorded_at,
        }


def validate(record: dict) -> None:
    """Reject a record before it becomes history."""
    if not isinstance(record, dict):
        raise AnnotationInvalid("annotation must be an object")
    label = record.get("label")
    if label not in LABELS:
        raise AnnotationInvalid(f"unknown label: {label!r}")
    if record.get("verdict") not in VERDICTS:
        raise AnnotationInvalid(f"verdict must be one of {VERDICTS}")
    if record.get("dataset") not in DATASETS:
        raise AnnotationInvalid(f"dataset must be one of {DATASETS}")
    if record.get("certainty") not in CERTAINTIES:
        raise AnnotationInvalid(f"certainty must be one of {CERTAINTIES}")
    if not isinstance(record.get("reviewer"), str) or not record["reviewer"].strip():
        raise AnnotationInvalid("reviewer is required: an anonymous label cannot be weighed")
    sample = record.get("sample_id")
    if not isinstance(sample, str) or not sample or any(character.isspace() for character in sample):
        raise AnnotationInvalid("sample_id must be non-empty and contain no whitespace")
    if record["verdict"] == "root_cause" and not record.get("evidence"):
        raise AnnotationInvalid("root_cause requires evidence: table:record references")
    if record["verdict"] == "clue" and not record.get("evidence"):
        raise AnnotationInvalid("clue requires evidence: table:record references")
    refs = record.get("evidence", [])
    if not isinstance(refs, (list, tuple)) or not all(valid_reference(ref) for ref in refs):
        raise AnnotationInvalid("evidence must be a list of non-empty table:record references")
    version = record.get("review_version", 1)
    if type(version) is not int or version < 1:
        raise AnnotationInvalid("review_version must be a positive integer")
    if record.get("run_id") is not None and sample != f"run:{record['run_id']}":
        raise AnnotationInvalid("run_id must agree with sample_id")
    if record["verdict"] == "not_a_defect" and not str(record.get("note") or "").strip():
        raise AnnotationInvalid("not_a_defect requires a note saying what was checked")


@contextmanager
def _locked(location: Path, *, exclusive: bool):
    """One stable sibling lock guards read/check/version/append across processes."""
    location.parent.mkdir(parents=True, exist_ok=True)
    with location.with_name(location.name + ".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load(path: Path | str = LEDGER) -> list[Annotation]:
    location = Path(path).resolve()
    if not location.exists():
        return []
    with _locked(location, exclusive=False):
        return _load_unlocked(location)


def _load_unlocked(location: Path) -> list[Annotation]:
    if not location.exists():
        return []
    records: list[Annotation] = []
    for number, line in enumerate(location.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError as error:
            raise AnnotationInvalid(f"{location}:{number}: {error}") from None
        validate(document)
        records.append(Annotation(
            sample_id=document["sample_id"], label=document["label"],
            verdict=document["verdict"], dataset=document["dataset"],
            reviewer=document["reviewer"], certainty=document["certainty"],
            evidence=tuple(document.get("evidence") or ()),
            note=document.get("note", ""),
            review_version=int(document.get("review_version", 1)),
            run_id=document.get("run_id"),
            recorded_at=document.get("recorded_at")))
    _check_partitions(records)
    return records


def _check_partitions(records):
    datasets = {}
    for record in records:
        previous = datasets.setdefault(record.sample_id, record.dataset)
        if previous != record.dataset:
            raise AnnotationInvalid(f"{record.sample_id} belongs to conflicting datasets")


def append(record: dict, path: Path | str = LEDGER) -> Annotation:
    """Add one label. Never rewrites an earlier line.

    The review version increments per ``(sample, reviewer)``, so a correction is
    a new record next to the old one and the change of mind stays visible.
    """
    document = dict(record)
    document.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    validate(document)
    location = Path(path).resolve()
    with _locked(location, exclusive=True):
        existing = _load_unlocked(location)
        for earlier in existing:
            if earlier.sample_id == document["sample_id"] and earlier.dataset != document["dataset"]:
                raise AnnotationInvalid(
                    f"{document['sample_id']} is already in the {earlier.dataset} set; "
                    "a sample cannot be both tuned on and reported")
        previous = [item.review_version for item in existing
                    if item.sample_id == document["sample_id"]
                    and item.reviewer == document["reviewer"]]
        document["review_version"] = 1 + max(previous, default=0)
        annotation = Annotation(
            sample_id=document["sample_id"], label=document["label"],
            verdict=document["verdict"], dataset=document["dataset"],
            reviewer=document["reviewer"], certainty=document["certainty"],
            evidence=tuple(document.get("evidence") or ()), note=document.get("note", ""),
            review_version=document["review_version"], run_id=document.get("run_id"),
            recorded_at=document["recorded_at"])
        with location.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(annotation.to_json(), ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return annotation


# ------------------------------------------------------------------------ report


def latest(records: list[Annotation]) -> list[Annotation]:
    """The newest review by each reviewer for each (sample, label)."""
    _check_partitions(records)
    newest: dict[tuple[str, str, str], Annotation] = {}
    for record in records:
        key = (record.sample_id, record.reviewer, record.label)
        if (key in newest and record.review_version == newest[key].review_version
                and record != newest[key]):
            raise AnnotationInvalid("conflicting reviews share a review_version")
        if key not in newest or record.review_version > newest[key].review_version:
            newest[key] = record
    return list(newest.values())


def agreement(records: list[Annotation]) -> dict:
    """How often two reviewers marked the same label set positive on one sample."""
    by_sample: dict[str, dict[str, set[str]]] = {}
    for record in latest(records):
        positive = record.verdict in ("clue", "root_cause")
        by_sample.setdefault(record.sample_id, {}).setdefault(record.reviewer, set())
        if positive:
            by_sample[record.sample_id][record.reviewer].add(record.label)
    comparable = [sample for sample in by_sample.values() if len(sample) >= 2]
    agree = sum(1 for sample in comparable
                if len({frozenset(labels) for labels in sample.values()}) == 1)
    return {
        "samples_with_two_reviewers": len(comparable),
        "agreed": agree,
        "rate": (agree / len(comparable)) if comparable else None,
        "reason": None if comparable else "no sample has been reviewed by two people",
    }


def evaluate(records: list[Annotation], diagnoses: list[dict]) -> dict:
    """Compare rule output with human verdicts, per dataset and per label.

    A disagreement between reviewers makes the sample ``disputed`` for that
    label: it is reported and excluded from the matrix rather than resolved by
    majority or by the reviewer who agreed with the rule.
    """
    reviewed = latest(records)
    truth: dict[tuple[str, str], dict] = {}
    for record in reviewed:
        key = (record.sample_id, record.label)
        bucket = truth.setdefault(key, {"positive": [], "negative": [], "reviewers": {}})
        bucket["reviewers"][record.reviewer] = record.verdict
        if record.verdict in ("clue", "root_cause"):
            bucket["positive"].append(record.reviewer)
        else:
            bucket["negative"].append(record.reviewer)

    datasets = {record.sample_id: record.dataset for record in reviewed}
    predictions: dict[str, set[str]] = {}
    unavailable = {}
    for document in diagnoses:
        if document.get("rule_version") != RULE_VERSION:
            raise AnnotationInvalid(f"diagnosis rule_version must be {RULE_VERSION}; mixed or missing versions rejected")
        sample = document.get("sample_id")
        if not isinstance(sample, str) or not sample or any(c.isspace() for c in sample):
            raise AnnotationInvalid("diagnosis requires sample_id")
        if sample in predictions:
            raise AnnotationInvalid(f"duplicate diagnosis for {sample}")
        run = document.get("run") or {}
        if run.get("id") is not None and sample != f"run:{run['id']}":
            raise AnnotationInvalid("diagnosis run.id disagrees with sample_id")
        findings = document.get("findings")
        if (not isinstance(findings, list) or any(not isinstance(finding, dict)
                or finding.get("label") not in LABELS for finding in findings)):
            raise AnnotationInvalid("diagnosis findings must be a list of known labels")
        predictions[sample] = {finding["label"] for finding in findings}
        not_evaluable = document.get("not_evaluable", [])
        if (not isinstance(not_evaluable, list) or any(not isinstance(entry, dict)
                or entry.get("label") not in LABELS for entry in not_evaluable)):
            raise AnnotationInvalid("not_evaluable must be a list of known labels")
        unavailable[sample] = {entry["label"] for entry in not_evaluable}
        if unavailable[sample] & predictions[sample]:
            raise AnnotationInvalid("a diagnosis cannot both predict and mark a label not_evaluable")

    matrix: dict[str, dict[str, dict]] = {
        label: {dataset: {"tp": 0, "fp": 0, "fn": 0, "tn": 0,
                          "positives": 0, "disputed": 0, "without_diagnosis": 0, "not_evaluable": 0}
                for dataset in DATASETS}
        for label in LABELS
    }
    disputed: list[dict] = []
    unlabelled = 0
    for (sample, label), bucket in truth.items():
        dataset = datasets.get(sample)
        if dataset is None:
            continue
        positive, negative = bucket["positive"], bucket["negative"]
        if positive and negative:
            matrix[label][dataset]["disputed"] += 1
            disputed.append({"sample_id": sample, "label": label,
                             "reviewers": bucket["reviewers"]})
            continue
        if sample not in predictions:
            matrix[label][dataset]["without_diagnosis"] += 1
            continue
        if label in unavailable[sample]:
            matrix[label][dataset]["not_evaluable"] += 1
            continue
        fired = label in predictions[sample]
        if positive:
            matrix[label][dataset]["positives"] += 1
            matrix[label][dataset]["tp" if fired else "fn"] += 1
        else:
            matrix[label][dataset]["fp" if fired else "tn"] += 1
    for sample in datasets:
        if sample not in predictions:
            unlabelled += 1

    labels: dict[str, dict] = {}
    for label in LABELS:
        row = {"tune": matrix[label]["tune"], "validation": matrix[label]["validation"]}
        row["precision"] = _ratio(row["validation"]["tp"],
                                 row["validation"]["tp"] + row["validation"]["fp"],
                                 "no rule prediction on validation samples for this label")
        row["recall"] = _ratio(row["validation"]["tp"],
                              row["validation"]["tp"] + row["validation"]["fn"],
                              "no evaluable human positive in validation for this label (missing diagnoses excluded)")
        row["accuracy_claim"] = ("validation only; tune counts are shown but never reported "
                                 "as skill")
        labels[label] = row

    return {
        "rule_version": RULE_VERSION,
        "samples": {
            "tune": sum(1 for value in datasets.values() if value == "tune"),
            "validation": sum(1 for value in datasets.values() if value == "validation"),
            "reviewers": sorted({record.reviewer for record in reviewed}),
            "without_diagnosis": unlabelled,
            "evaluated": len(set(datasets) & set(predictions)),
            "missing_diagnoses": sorted(set(datasets) - set(predictions)),
        },
        "agreement": agreement(records),
        "labels": labels,
        "disputed": disputed,
        "notes": [
            "Precision and recall are reported for validation samples only. Tuning samples "
            "are counted, never credited.",
            "A missing denominator yields null and a reason. It is never reported as 0 or 1.",
            "Missing diagnoses and not_evaluable labels are excluded from the confusion matrix, "
            "not counted as negative predictions. Metrics describe only the evaluated subset.",
            "A sample with conflicting reviewer verdicts is disputed and excluded from the "
            "matrix; disagreement is not averaged away.",
            "A rule firing on every failure says nothing about precision: the negative class "
            "comes from labeled not_a_defect samples.",
        ],
    }


def _ratio(numerator: int, denominator: int, reason: str) -> dict:
    if denominator == 0:
        return {"value": None, "n": 0, "reason": reason}
    return {"value": numerator / denominator, "n": denominator, "reason": None}


def format_report(report: dict) -> str:
    samples = report["samples"]
    lines = [f"labels  rule {report['rule_version']}  "
             f"tune {samples['tune']}  validation {samples['validation']}  "
             f"reviewers {', '.join(samples['reviewers']) or 'none'}",
             f"diagnostic coverage  {samples['evaluated']} evaluated, "
             f"{samples['without_diagnosis']} without diagnosis (excluded from metrics)"]
    for sample in samples['missing_diagnoses']:
        lines.append(f"  missing diagnosis: {sample}")
    for label, row in report['labels'].items():
        excluded = sum(row[dataset]['not_evaluable'] for dataset in DATASETS)
        if excluded:
            lines.append(f"  {label}: {excluded} not_evaluable judgement(s) excluded")

    agreement_block = report["agreement"]
    if agreement_block["rate"] is None:
        lines.append(f"agreement  not estimable — {agreement_block['reason']}")
    else:
        lines.append(f"agreement  {agreement_block['agreed']}/"
                     f"{agreement_block['samples_with_two_reviewers']} samples agree")

    lines.append("")
    lines.append(f"{'label':<6}{'tune':<16}{'validation':<16}"
                 f"{'precision':<18}{'recall':<18}")
    for label, row in report["labels"].items():
        lines.append(f"{label:<6}{_counts(row['tune']):<16}{_counts(row['validation']):<16}"
                     f"{_show(row['precision']):<18}{_show(row['recall']):<18}")

    # Two different silences: a label nobody labelled, and a label with labels
    # but no denominator (for example, no positive to divide recall by). Printing
    # ten "no validation sample" lines would drown the second, which is the one
    # that matters.
    unlabelled_labels = [label for label, row in report["labels"].items()
                         if not any(row["validation"][key] for key in ("tp", "fp", "fn", "tn"))]
    reasons = []
    for label, row in report["labels"].items():
        if label in unlabelled_labels:
            continue
        for key in ("precision", "recall"):
            if row[key]["value"] is None and row[key]["reason"]:
                reasons.append(f"{label}: {row[key]['reason']}")
    if reasons or unlabelled_labels:
        lines.append("")
        lines.append("not estimable")
        for reason in sorted(set(reasons)):
            lines.append(f"  {reason}")
        if unlabelled_labels:
            lines.append(f"  no validation sample for: {', '.join(unlabelled_labels)}")

    if report["disputed"]:
        lines.append("")
        lines.append("disputed (excluded from the matrix)")
        for item in report["disputed"]:
            verdicts = ", ".join(f"{who}={what}" for who, what in item["reviewers"].items())
            lines.append(f"  {item['sample_id']} {item['label']}: {verdicts}")

    estimable = [label for label, row in report["labels"].items()
                 if row["precision"]["value"] is not None or row["recall"]["value"] is not None]
    lines.append("")
    if report["samples"]["validation"] == 0:
        lines.append("no validation sample: no accuracy is claimed")
    elif estimable:
        lines.append(f"accuracy is claimed for validation samples only "
                     f"({len(estimable)} label(s) with a denominator)")
    else:
        lines.append(f"{report['samples']['validation']} validation sample(s): "
                     "no accuracy is claimed — no label has both a prediction and a verdict")
    return "\n".join(lines) + "\n"


def _counts(row: dict) -> str:
    return f"{row['tp'] + row['fn']}pos {row['tp'] + row['fp']}pred {row['fp']}fp"


def _show(block: dict) -> str:
    if block["value"] is None:
        return "—"
    return f"{block['value']:.2f} (n={block['n']})"


__all__ = [
    "Annotation", "AnnotationInvalid", "DATASETS", "LEDGER", "VERDICTS",
    "agreement", "append", "evaluate", "format_report", "latest", "load", "validate",
]
