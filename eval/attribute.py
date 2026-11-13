"""Attribution: what the record supports, and what it does not.

P2 asks for diagnostic clues with reviewable evidence and explicit uncertainty,
not for automatic root causes. Three rules shape everything below.

**A rule may only claim what is recorded.** No rule parses a free-text error
message. Classifying prose needs a labelled sample to calibrate against, and an
uncalibrated pattern presented as a diagnosis is precisely the "looks precise,
is not" failure the plan warns about. Every trigger therefore reads an enum
(``status``, ``end_reason``, an approval ``status``), a structural relationship
(ordering, identical invocation, a retry after an unconfirmed outcome) or a
counter. Error text stays in the database.

**Certainty describes the label, not the observation.** A failed call is
observed; "the tool is defective" is not. ``observed`` is therefore used only
where the label itself is a recorded fact — the run stopped at ``turn_limit``,
an approval was denied. A label that needs interpretation beyond its trigger
("this is a no-progress loop", "the input was wrong") stays ``suspected``.

**``confirmed`` is never produced by a rule.** It requires a recorded human
review that names evidence. Without one, ``root_cause`` is null and
``escalation.pending`` states what would have to be checked, which is the
plan's "at least one clue that cannot be upgraded" made structural rather than
demonstrated once in a document.

Labels follow section 5 of the plan. ``not_evaluable`` is a separate list from
``checked``: the first says a rule cannot run on this evidence, the second says
a rule ran and its condition was absent. Collapsing the two would let "we did
not look" read as "we looked and it was fine".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import UUID

#: Attribution contract version. Bumped when the meaning of a field changes.
ATTRIBUTION_VERSION = 2

#: Rule set version, recorded on every finding. A result is not comparable
#: across versions because a trigger may have moved.
RULE_VERSION = "attr-2"

#: The three levels section 5 allows. There is no numeric confidence: an
#: uncalibrated probability would be a fabricated measurement.
CERTAINTIES = ("observed", "suspected", "confirmed")

#: End reasons that mean a resource limit stopped the run, not that a step
#: failed. Kept next to the labels so the mapping is auditable in one place.
BUDGET_REASONS = (
    "turn_limit", "run_timeout", "context_budget_exhausted", "output_limit",
)

#: End reasons that name the layer which stopped the run. The plan's F5 upgrade
#: asks for exactly this separation; the reason names a layer, never a defect.
EXECUTION_REASONS = {
    "execution_error": "host-side execution layer stopped the run (end_reason)",
    "storage_error": "storage layer stopped the run (end_reason)",
    "model_error": "model provider or transport failed (end_reason)",
    "model_protocol_error": "the provider response did not match the protocol (end_reason)",
}

#: Approval states that mean the request was not granted. ``approved`` is the
#: normal path and produces no finding; ``pending`` means the run is not over.
UNGRANTED_APPROVALS = ("denied", "expired", "cancelled")

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted")


@dataclass(frozen=True, slots=True)
class Label:
    """One row of the plan's label table, as data rather than prose."""

    code: str
    name: str
    observable: str
    confirmation: tuple[str, ...]

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "observable": self.observable,
            "confirmation_requires": list(self.confirmation),
        }


LABELS: dict[str, Label] = {
    "F1": Label(
        "F1", "specification understanding",
        "output or behaviour contradicts an explicit requirement",
        ("the requirement's source and its ambiguity are checked",
         "understanding is separated from execution")),
    "F2": Label(
        "F2", "context information loss",
        "messages removed, a summary substituted, a result truncated",
        ("a key fact is absent from the summary and the task state",
         "the missing fact is tied to the failing behaviour",
         "a controlled comparison is run when the link is disputed")),
    "F3": Label(
        "F3", "tool selection",
        "a tool was used, or a declared tool was never loaded",
        ("the tool contract shows the choice was unsuitable for the goal",
         "a unique call order is not required to prove it")),
    "F4": Label(
        "F4", "parameter problem",
        "schema or type validation failed",
        ("model input error, adapter defect, schema mismatch and file anomaly are separated",)),
    "F5": Label(
        "F5", "execution problem",
        "a tool failed, an outcome is unknown, or execution stopped with an error",
        ("the layer is identified: environment, dependency, provider, application or tool defect",
         "retryability is not treated as a responsibility")),
    "F6": Label(
        "F6", "result understanding",
        "a later claim or action contradicts a recorded result",
        ("the contradiction is confirmed from state or human review, not from a string difference",)),
    "F7": Label(
        "F7", "no-progress loop",
        "a tool is repeated, retried or polled",
        ("results, file state and task progress are checked",
         "legitimate polling and re-verification after an edit are excluded")),
    "F8": Label(
        "F8", "budget limit",
        "a turn, time, output or context limit stopped the run",
        ("whether inefficiency, environment failure or configuration caused it",)),
    "F9": Label(
        "F9", "approval interaction",
        "an approval was denied, expired, unviewed or repeated",
        ("correct interception is distinguished from unnecessary friction",)),
    "F10": Label(
        "F10", "initial environment drift",
        "the pre-trial environment differed from the fixed baseline",
        ("files, dependencies, image and service state are compared with the baseline",)),
}


@dataclass(frozen=True, slots=True)
class Ref:
    """Where a claim came from. Table plus primary key, never a copy of a row."""

    table: str
    record: str
    event_sequence: int | None = None

    def to_json(self) -> dict:
        document = {"table": self.table, "record": self.record}
        if self.event_sequence is not None:
            document["event_sequence"] = self.event_sequence
        return document


@dataclass(frozen=True, slots=True)
class Finding:
    label: str
    certainty: str
    claim: str
    rule: str
    evidence: tuple[Ref, ...]
    possible_causes: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    observations: dict | None = None
    confirmation: dict | None = None

    def to_json(self) -> dict:
        document = {
            "label": self.label,
            "certainty": self.certainty,
            "claim": self.claim,
            "rule": self.rule,
            "evidence": [ref.to_json() for ref in self.evidence],
            "possible_causes": list(self.possible_causes),
            "missing_evidence": list(self.missing_evidence),
        }
        if self.observations is not None:
            document["observations"] = self.observations
        if self.confirmation is not None:
            document["confirmation"] = self.confirmation
        return document


@dataclass(frozen=True, slots=True)
class Context:
    """Normalized input to the rules: records, not spans.

    Section 5 requires the input to include events, context, case expectations,
    environment baseline and outcome — a span list alone cannot answer F4, F8 or
    F10, so the projection is separate from the timeline on purpose.
    """

    run: dict
    tools: tuple[dict, ...] = ()
    approvals: tuple[dict, ...] = ()
    compactions: tuple[dict, ...] = ()
    events: tuple[dict, ...] = ()
    case: dict | None = None

    @property
    def end_reason(self) -> str | None:
        return self.run.get("end_reason")

    def of_type(self, *types: str) -> tuple[dict, ...]:
        return tuple(event for event in self.events if event.get("type") in types)


def _context(evidence: dict) -> Context:
    return Context(
        run=evidence.get("run") or {},
        tools=tuple(evidence.get("tools") or ()),
        approvals=tuple(evidence.get("approvals") or ()),
        compactions=tuple(evidence.get("compactions") or ()),
        events=tuple(evidence.get("events") or ()),
        case=evidence.get("case"),
    )


# --------------------------------------------------------------------------- rules
#
# Each rule is a pure function of one Context and returns its findings plus a
# ``checked`` record. A rule that cannot run returns no findings and no record:
# it belongs in ``not_evaluable`` instead, and the two lists are built separately
# so a silent skip cannot look like a clean result.


def _rule_execution(context: Context) -> tuple[list[Finding], dict]:
    """F5: a tool failed, an outcome is unknown, or the run stopped with an error."""
    failed = [call for call in context.tools if call.get("status") == "failed"]
    unknown = [call for call in context.tools if call.get("status") == "unknown"]
    reasons = context.end_reason if context.end_reason in EXECUTION_REASONS else None

    findings: list[Finding] = []
    if failed or unknown:
        causes = []
        if failed:
            causes.append("a tool reported an error (status=failed)")
        if unknown:
            causes.append("a call started and its outcome could not be confirmed (status=unknown)")
        missing = ["which layer failed: environment, dependency, provider, application or tool defect"]
        if any(call.get("details") is None for call in failed + unknown):
            # retry.py raises codes for the model at run time, but the code is
            # not persisted: ``result.details`` is null on every stored failure.
            missing.append("a structured failure class (not persisted in tool_calls.result)")
        findings.append(Finding(
            label="F5", certainty="observed",
            claim=f"{len(failed) + len(unknown)} tool call(s) did not return a usable result",
            rule="F5.tool-failure",
            evidence=tuple(Ref("tool_calls", str(call["id"])) for call in (failed + unknown)),
            possible_causes=tuple(causes),
            missing_evidence=tuple(missing),
            observations={"failed": len(failed), "unknown": len(unknown),
                          "tools_recorded": len(context.tools)},
        ))
    if reasons:
        findings.append(Finding(
            label="F5", certainty="observed",
            claim=f"the run stopped with end_reason={context.end_reason}",
            rule="F5.execution-end-reason",
            evidence=_end_evidence(context),
            possible_causes=(EXECUTION_REASONS[reasons],),
            missing_evidence=("the specific defect inside that layer",),
            observations={"end_reason": context.end_reason},
        ))
    return findings, {
        "rule": "F5.tool-failure", "label": "F5", "triggered": len(findings),
        "observed": {"tools": len(context.tools), "failed": len(failed),
                     "unknown": len(unknown), "end_reason": context.end_reason},
    }


def _rule_retry_after_unknown(context: Context) -> tuple[list[Finding], dict]:
    """F5: the same invocation was issued again after an unconfirmed outcome.

    Structural, not textual: the plan's "unknown 不盲重试" is checkable from the
    records alone. Whether the retry was wrong still needs the effect, so this
    stays ``suspected`` rather than joining the observed failure above.
    """
    findings: list[Finding] = []
    unknown = [call for call in context.tools if call.get("status") == "unknown"]
    for position, call in enumerate(context.tools):
        if call.get("status") != "unknown":
            continue
        fingerprint = _fingerprint(call)
        # Position, not call_index: the column restarts at zero for every model
        # call, so a retry in the next iteration carries a smaller number.
        later = [other for other in context.tools[position + 1:]
                 if _fingerprint(other) == fingerprint]
        if not later:
            continue
        findings.append(Finding(
            label="F5", certainty="suspected",
            claim=f"{call.get('name')} was called again after an unconfirmed outcome",
            rule="F5.retry-after-unknown",
            evidence=(Ref("tool_calls", str(call["id"])),) + tuple(
                Ref("tool_calls", str(other["id"])) for other in later[:3]),
            possible_causes=("the effect may already have happened and was repeated",
                             "the second call was gated by an inspection first"),
            missing_evidence=("whether the file or process was inspected between the two calls",
                              "whether the effect of the first call actually occurred"),
            observations={"tool": call.get("name"), "repeats": len(later)},
        ))
    return findings, {
        "rule": "F5.retry-after-unknown", "label": "F5", "triggered": len(findings),
        "observed": {"unknown_calls": len(unknown)},
    }


def _rule_loop(context: Context) -> tuple[list[Finding], dict]:
    """F7: one invocation repeated, and every attempt returned the same thing.

    Repetition alone is not a loop: polling a service and re-reading a file
    after an edit both repeat a call legitimately. What separates them in the
    record is whether the attempt produced anything new, so this rule compares
    the stored result digests. Three identical calls with three byte-identical
    results is the strongest claim the records support; it is still only
    ``suspected``, because how long a deliberate poll may run is a decision the
    record does not contain.
    """
    groups: dict[str, list[dict]] = {}
    for call in context.tools:
        groups.setdefault(_fingerprint(call), []).append(call)

    findings: list[Finding] = []
    repeated = 0
    identical = 0
    for calls in groups.values():
        if len(calls) < 2:
            continue
        repeated += len(calls) - 1
        digests = [call.get("result_digest") for call in calls]
        if len(calls) >= 3 and all(digest is not None for digest in digests) \
                and len(set(digests)) == 1:
            identical += 1
            findings.append(Finding(
                label="F7", certainty="suspected",
                claim=f"{calls[0].get('name')} ran {len(calls)} times with identical results",
                rule="F7.repeat",
                evidence=tuple(Ref("tool_calls", str(call["id"])) for call in calls[:5]),
                possible_causes=("the earlier result was not used",
                                 "the tool kept failing the same way"),
                missing_evidence=("whether the repetition was a deliberate poll and for how long",
                                  "whether file state or task progress changed between calls"),
                observations={"tool": calls[0].get("name"), "calls": len(calls)},
            ))

    if context.end_reason == "repeated_tool_call":
        findings.append(Finding(
            label="F7", certainty="observed",
            claim="the run stopped because the same call was refused twice",
            rule="F7.guard-stop",
            evidence=_end_evidence(context),
            possible_causes=("an answer was already available and was asked for again",
                             "the model retried a call whose result it could not use"),
            missing_evidence=("whether any repeated call produced new information",),
            observations={"end_reason": context.end_reason, "repeated_calls": repeated},
        ))
    return findings, {
        "rule": "F7.repeat", "label": "F7", "triggered": len(findings),
        "observed": {"repeated_calls": repeated, "identical_result_groups": identical,
                     "end_reason": context.end_reason},
    }


def _end_evidence(context: Context) -> tuple[Ref, ...]:
    """The terminal event when one was written, otherwise the run row itself."""
    events = context.of_type("run_failed", "run_cancelled", "run_interrupted", "run_completed")
    if events:
        return tuple(Ref("run_events", str(event.get("sequence")), event.get("sequence"))
                     for event in events[:2])
    return (Ref("runs", str(context.run.get("id"))),)


def _rule_budget(context: Context) -> tuple[list[Finding], dict]:
    """F8: a limit stopped the run. The limit is the fact; the cause is not."""
    reason = context.end_reason
    findings: list[Finding] = []
    rejections = context.of_type("context_rejected")
    if reason in BUDGET_REASONS:
        causes = [f"the run reached a configured limit (end_reason={reason})"]
        for event in rejections[:2]:
            payload = event.get("payload") or {}
            if "required_tokens" in payload and "input_budget" in payload:
                causes.append(
                    f"the required context ({payload['required_tokens']} tokens) exceeded the "
                    f"input budget ({payload['input_budget']} tokens)")
        findings.append(Finding(
            label="F8", certainty="observed",
            claim=f"the run stopped at a resource limit: {reason}",
            rule="F8.budget-stop",
            evidence=_end_evidence(context) + tuple(
                Ref("run_events", str(event.get("sequence")), event.get("sequence"))
                for event in rejections[:2]),
            possible_causes=tuple(causes),
            missing_evidence=("whether inefficiency, environment failure or configuration "
                              "caused the limit to be reached",),
            observations={"end_reason": reason, "context_rejections": len(rejections)},
        ))
    return findings, {
        "rule": "F8.budget-stop", "label": "F8", "triggered": len(findings),
        "observed": {"end_reason": reason, "context_rejections": len(rejections)},
    }


def _rule_approval(context: Context) -> tuple[list[Finding], dict]:
    """F9: an approval was not granted.

    A refusal is a decision, so the event is observed. Whether it was the right
    decision is not in the record: the two readings are carried side by side and
    a case that declares an expected decision only tells us whether this run met
    it, never that the refusal was correct.
    """
    ungranted = [row for row in context.approvals if row.get("status") in UNGRANTED_APPROVALS]
    findings: list[Finding] = []
    expected = _expected_approvals(context.case)
    for row in ungranted:
        expectation = None
        if expected is not None:
            expectation = "met" if row.get("status") in expected else "unmet"
        decisions = [event for event in context.of_type("approval_decided")
                     if (event.get("payload") or {}).get("approval_id") == str(row.get("id"))]
        note = (decisions[0].get("payload") or {}).get("note") if decisions else None
        causes = ["correct interception: the request was outside what was allowed",
                  "unnecessary friction: a legitimate request was blocked"]
        # The decision note is written by the controller, not the model, and it
        # is the only place that distinguishes "a person refused" from "nobody
        # was there". It is reported as the record, not interpreted.
        causes.append(f"recorded decision note: {note}" if note
                      else "no approver was present, so the request was never judged")
        findings.append(Finding(
            label="F9", certainty="observed",
            claim=f"approval for {row.get('tool_name')} ended {row.get('status')} without a grant",
            rule="F9.ungranted",
            evidence=(Ref("approval_requests", str(row["id"])),)
                     + tuple(Ref("run_events", str(event.get("sequence")), event.get("sequence"))
                             for event in decisions[:2]),
            possible_causes=tuple(causes),
            missing_evidence=("a reviewer's judgement of whether the request was legitimate",
                              "the policy the decision was made against"),
            observations={"tool": row.get("tool_name"), "status": row.get("status"),
                          "case_expectation": expectation, "decision_note": note},
        ))
    return findings, {
        "rule": "F9.ungranted", "label": "F9", "triggered": len(findings),
        "observed": {"approvals": len(context.approvals), "ungranted": len(ungranted),
                     "case_declares_approvals": expected is not None},
    }


def _rule_input_correction(context: Context) -> tuple[list[Finding], dict]:
    """F4: a failed call is followed by the same tool with different input that works.

    That is what a corrected input looks like structurally. It is not proof: the
    first call may have failed for a reason the second one avoided. The rule
    therefore stays ``suspected`` and asks for the validation message, which is
    the one thing the record does not keep in structured form.
    """
    findings: list[Finding] = []
    corrections: dict[str, list[tuple[Ref, Ref]]] = {}
    for index, call in enumerate(context.tools):
        if call.get("status") != "failed":
            continue
        for later in context.tools[index + 1:]:
            if later.get("name") != call.get("name"):
                continue
            if later.get("status") != "completed":
                break
            if _fingerprint(later) != _fingerprint(call):
                corrections.setdefault(call.get("name"), []).append(
                    (Ref("tool_calls", str(call["id"])), Ref("tool_calls", str(later["id"]))))
            break
    for name, pairs in corrections.items():
        findings.append(Finding(
            label="F4", certainty="suspected",
            claim=f"{name} succeeded after failing with different input "
                  f"({len(pairs)} time(s))",
            rule="F4.input-correction",
            evidence=tuple(ref for pair in pairs[:3] for ref in pair),
            possible_causes=("the first input was invalid and the model corrected it",
                             "the first call failed for a reason the second avoided"),
            missing_evidence=("the tool's structured validation error",
                              "which field was rejected"),
            observations={"tool": name, "corrections": len(pairs)},
        ))
    return findings, {
        "rule": "F4.input-correction", "label": "F4",
        "triggered": sum(len(pairs) for pairs in corrections.values()),
        "observed": {"failed_calls": sum(1 for call in context.tools
                                         if call.get("status") == "failed")},
    }


def _rule_environment(context: Context) -> tuple[list[Finding], dict]:
    """F10: the boundary recorded an unusable environment.

    A healthy ``boundary_applied`` event is not a baseline comparison, so it
    cannot clear F10; it only proves the boundary was applied. Drift is reported
    only when the event says the environment was unavailable or read-only.
    """
    boundaries = context.of_type("boundary_applied")
    broken = [event for event in boundaries
              if (event.get("payload") or {}).get("available") is False
              or (event.get("payload") or {}).get("workspace_writable") is False]
    findings = [Finding(
        label="F10", certainty="observed",
        claim="the execution boundary was not usable for this run",
        rule="F10.boundary-unusable",
        evidence=tuple(Ref("run_events", str(event.get("sequence")), event.get("sequence"))
                       for event in broken),
        possible_causes=("the container or sandbox was unavailable",
                         "the workspace was mounted read-only"),
        missing_evidence=("the pre-trial baseline for files, dependencies, image and services",),
        observations={"boundary_events": len(boundaries), "unusable": len(broken)},
    )] if broken else []
    return findings, {
        "rule": "F10.boundary-unusable", "label": "F10", "triggered": len(findings),
        "observed": {"boundary_events": len(boundaries), "unusable": len(broken)},
    }


RULES = (_rule_execution, _rule_retry_after_unknown, _rule_loop, _rule_budget,
         _rule_approval, _rule_input_correction, _rule_environment)


# ----------------------------------------------------------------------- assembly


def attribute(evidence: dict, *, confirmations=(), now: datetime | None = None) -> dict:
    """Run every rule over one run's records and return a reviewable document.

    ``confirmations`` are annotation records (see ``eval.annotations``). Only a
    record with ``verdict == "root_cause"`` and at least one evidence reference
    upgrades a finding, and only for the label it names.
    """
    context = _context(evidence)
    if not context.run:
        raise LookupError("run not found")

    findings: list[Finding] = []
    checked: list[dict] = []
    for rule in RULES:
        produced, record = rule(context)
        findings.extend(produced)
        checked.append(record)

    findings, escalation = _apply_confirmations(findings, confirmations, context)
    clues = _clues(context)
    labels = _not_evaluable(context)

    document = {
        "attribution_version": ATTRIBUTION_VERSION,
        "rule_version": RULE_VERSION,
        "observed_at": _iso(now or datetime.now(timezone.utc)),
        "sample_id": sample_id(context.run.get("id")),
        "run": {
            "id": str(context.run.get("id")),
            "session_id": str(context.run.get("session_id")),
            "status": context.run.get("status"),
            "end_reason": context.end_reason,
        },
        "outcome": {
            "status": context.run.get("status"),
            "end_reason": context.end_reason,
            "terminal": context.run.get("status") in TERMINAL_STATUSES,
            "note": "completed is a lifecycle state, not a success verdict",
        },
        "case": _case_summary(context.case),
        "findings": [finding.to_json() for finding in findings],
        "clues": clues,
        "checked": checked,
        "not_evaluable": labels,
        "root_cause": _root_cause(findings),
        "escalation": escalation,
        "labels": {code: label.to_json() for code, label in LABELS.items()},
        "notes": [
            "Rules read enums, ordering and counters. No free-text error message is parsed, "
            "so an unclassified error is reported as missing evidence rather than guessed.",
            "Certainty describes the label, not the observation: a failed call is observed, "
            "a defect is not.",
            "confirmed is never produced by a rule; it requires a human review record that names "
            "evidence.",
            "Repetition is compared by the digest of each stored result, so a repeated call that "
            "returned something new is not reported as a loop. Polling remains a judgement the "
            "record cannot make.",
            "Tool arguments never leave the process; evidence carries a fingerprint, not the input.",
        ],
    }
    return _redact(document, context.run.get("root_path"))


def sample_id(run_id) -> str:
    return f"run:{run_id}"


def valid_reference(value) -> bool:
    """Syntax only; confirmation separately checks membership in this run."""
    return isinstance(value, str) and re.fullmatch(
        r"(?:runs|tool_calls|run_events|approval_requests|compaction_calls|model_calls):[A-Za-z0-9_.-]+",
        value) is not None


def _apply_confirmations(findings: list[Finding], confirmations, context: Context) -> tuple[list[Finding], dict]:
    """Use latest reviews, reject conflicts, and bind evidence to this finding."""
    latest = {}
    rejected = []
    for record in confirmations or ():
        def reject(reason):
            rejected.append({"reason": reason, "record": _safe_record(record)})

        if record.get("sample_id") != sample_id(context.run.get("id")):
            reject("review belongs to another sample")
            continue
        if record.get("run_id") not in (None, str(context.run.get("id"))):
            reject("review belongs to another run")
            continue
        if record.get("label") not in LABELS:
            reject("unknown label")
            continue
        version = record.get("review_version", 1)
        if (not isinstance(record.get("reviewer"), str) or not record["reviewer"].strip()
                or type(version) is not int or version < 1):
            reject("reviewer and positive integer review_version are required")
            continue
        key = (record["sample_id"], record["reviewer"], record["label"])
        previous = latest.get(key)
        if previous and previous["review_version"] == version and previous != record:
            # Refuse ambiguous history rather than picking by file order.
            raise ValueError("conflicting reviews share a review_version")
        if previous is None or version > previous["review_version"]:
            latest[key] = {**record, "review_version": version}

    by_label = {}
    for record in latest.values():
        by_label.setdefault(record["label"], []).append(record)
    known = {f"runs:{context.run.get('id')}"}
    for table, rows in (("tool_calls", context.tools), ("approval_requests", context.approvals),
                        ("compaction_calls", context.compactions)):
        known.update(f"{table}:{row['id']}" for row in rows)
    known.update(f"run_events:{row['sequence']}" for row in context.events)
    known.update(f"model_calls:{row['model_call_id']}" for row in context.tools
                 if row.get("model_call_id"))

    accepted = {}
    for label, records in by_label.items():
        roots = [record for record in records if record.get("verdict") == "root_cause"]
        if not roots:
            continue
        if any(record.get("verdict") != "root_cause" for record in records):
            rejected.append({"reason": "conflicting reviewer verdicts", "label": label})
            continue
        for record in roots:
            refs = record.get("evidence")
            if (not isinstance(refs, (list, tuple)) or not refs
                    or not all(valid_reference(ref) for ref in refs)):
                rejected.append({"reason": "a confirmation must name evidence as table:record references",
                                 "record": _safe_record(record)})
            elif not set(refs) <= known:
                rejected.append({"reason": "evidence is not present in this run's diagnostic input",
                                 "record": _safe_record(record)})
            else:
                accepted.setdefault(label, []).append(record)

    upgraded, confirmed, matched = [], [], set()
    for finding in findings:
        finding_refs = {f"{ref.table}:{ref.record}" for ref in finding.evidence}
        records = [record for record in accepted.get(finding.label, ())
                   if finding_refs.intersection(record["evidence"])]
        if not records:
            upgraded.append(finding)
            continue
        record = sorted(records, key=lambda item: item["reviewer"])[0]
        matched.add(finding.label)
        confirmed.append(finding.label)
        upgraded.append(_replace(finding, certainty="confirmed", confirmation={
            key: record.get(key) for key in
            ("sample_id", "reviewer", "review_version", "note", "evidence")
        }))
    return upgraded, {
        "rule": "latest non-conflicting review with evidence present in this run and linked to this finding",
        "confirmed": sorted(set(confirmed)), "rejected": rejected,
        "unmatched": sorted(set(accepted) - matched),
        "pending": [{"label": finding.label, "needs": list(finding.missing_evidence)}
                    for finding in upgraded if finding.certainty != "confirmed"],
    }


def _clues(context: Context) -> list[dict]:
    """Observations that are evidence but not labels.

    A summary replacing earlier messages is a fact. "Information was lost" is a
    conclusion the record cannot support, so it stays here and F2 stays
    not-evaluable. Same for recovering facts from an earlier failed run.
    """
    clues: list[dict] = []
    completed = context.of_type("compaction_completed")
    # Only summary installation emits completed. Call rows are model work, not
    # evidence of adoption; a failed/discarded speculative candidate changes no context.
    if completed:
        identities = {event.get("payload", {}).get("compaction_id") or event["sequence"]
                      for event in completed}
        clues.append({
            "kind": "context_replaced",
            "claim": "a summary replaced earlier messages during this run",
            "count": len(identities),
            "evidence": [Ref("run_events", str(event["sequence"]), event["sequence"]).to_json()
                         for event in completed[:3]],
            "observations": {"token_sizes": [
                {"before": (event.get("payload") or {}).get("tokens_before"),
                 "after": (event.get("payload") or {}).get("tokens_after")}
                for event in completed[:3]]},
            "note": "installation is recorded; whether a needed fact survived is not",
        })
    for status in ("started", "failed", "cancelled", "discarded"):
        events = context.of_type(f"compaction_{status}")
        if events:
            clues.append({
                "kind": f"compaction_{status}", "claim": f"compaction {status} was recorded",
                "count": len(events),
                "evidence": [Ref("run_events", str(event["sequence"]), event["sequence"]).to_json()
                             for event in events[:3]],
                "note": "this lifecycle event does not prove that context was replaced",
            })
    if context.compactions and not completed:
        clues.append({
            "kind": "compaction_calls_recorded", "claim": "compaction model calls were recorded",
            "count": len(context.compactions),
            "evidence": [Ref("compaction_calls", str(row["id"])).to_json()
                         for row in context.compactions[:3]],
            "note": "model work alone does not prove installation of a summary",
        })
    recovery = context.of_type("recovery_projected")
    if recovery:
        payload = recovery[0].get("payload") or {}
        clues.append({
            "kind": "recovered_from_earlier_run",
            "claim": "the run started with facts recovered from an earlier failed run",
            "count": payload.get("fact_count"),
            "evidence": [Ref("run_events", str(recovery[0].get("sequence")),
                             recovery[0].get("sequence")).to_json()],
            "note": "the source runs are named in the event; their failures are attributed separately",
            "source_runs": payload.get("source_runs") or [],
        })
    return clues


def _not_evaluable(context: Context) -> list[dict]:
    """Labels with no executable rule on this evidence, and what would unblock them.

    F1, F2, F3 and F6 have no rule at all: each needs an input a single run does
    not record. F10 has a narrow rule (an unusable boundary), so it is listed
    here only when no boundary event exists to read.
    """
    missing: list[dict] = [{
        "label": "F1",
        "why": "a single run records no requirement source and no grader result",
        "missing_inputs": ["the requirement the output is measured against",
                           "a grader or human verdict on whether it was met"],
    }, {
        "label": "F2",
        "why": "summary substitution is recorded, but whether a key fact was dropped is not",
        "missing_inputs": ["the facts the task needed after the summary",
                           "evidence that a dropped fact caused the failing behaviour"],
    }, {
        "label": "F3",
        "why": "no goal statement or tool contract is recorded with the run",
        "missing_inputs": ["the goal", "the contract of the tools that were available",
                           "why the chosen tool was unsuitable"],
    }, {
        "label": "F6",
        "why": "claims are free text; a contradiction needs semantic comparison",
        "missing_inputs": ["the claim and the result compared by a reviewer or a calibrated rubric"],
    }]
    if not context.of_type("boundary_applied"):
        missing.append({
            "label": "F10",
            "why": "no pre-trial baseline snapshot is recorded",
            "missing_inputs": ["file, dependency, image and service state before the trial"],
        })
    return missing


def _root_cause(findings: list[Finding]) -> dict | None:
    for finding in findings:
        if finding.certainty == "confirmed":
            return {"label": finding.label, "claim": finding.claim,
                    "confirmed_by": finding.confirmation}
    return None


def _case_summary(case: dict | None) -> dict | None:
    if not case:
        return None
    return {"id": case.get("id"), "version": case.get("version"),
            "sha256": case.get("sha256"),
            "declares_approvals": _expected_approvals(case) is not None}


def _expected_approvals(case: dict | None):
    if not case:
        return None
    authority = case.get("authority") or {}
    approvals = authority.get("approvals")
    return approvals if isinstance(approvals, list) and approvals else None


# --------------------------------------------------------------------------- utils


def _fingerprint(call: dict) -> str:
    """Stable identity for one invocation, matching the runtime guard.

    A read is identified by what it addresses; everything else by all of its
    arguments. The rule is imported from ``tools/progress`` rather than copied,
    so a change in the runtime guard moves this count with it. The import is
    local because the rest of this module is usable — and testable — without
    ``chagent`` on the path, which keeps the operator CLI importable for the
    commands that only read files.
    """
    from chagent.tools.progress import READ_IDENTITY

    arguments = call.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    keys = READ_IDENTITY.get(call.get("name"))
    if keys is not None:
        arguments = {key: arguments.get(key) for key in keys}
    blob = json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(f"{call.get('name')}\0{blob}".encode("utf-8")).hexdigest()[:16]


def _replace(finding: Finding, **changes) -> Finding:
    values = {field: getattr(finding, field) for field in finding.__slots__}
    values.update(changes)
    return Finding(**values)


def _safe_record(record: dict) -> dict:
    return {key: record.get(key) for key in
            ("sample_id", "label", "verdict", "reviewer", "review_version")}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _redact(document: dict, root_path: str | None) -> dict:
    """Reuse the timeline's filter rather than writing a second one."""
    from eval.spans import _redact as redact

    return redact(document, root_path)


# ---------------------------------------------------------------------- rendering


def format_diagnosis(document: dict, *, explain: bool = False) -> str:
    """Text rendering. Every line is a field of the JSON, nothing is added."""
    run = document["run"]
    lines = [f"run {run['id']}  {run['status']}/{run['end_reason']}  "
             f"rules {document['rule_version']}"]

    if document["findings"]:
        lines.append("")
        lines.append("findings")
        for finding in document["findings"]:
            lines.append(f"  {finding['label']:<4}{finding['certainty']:<10}{finding['claim']}")
            lines.append(f"       rule      {finding['rule']}")
            for ref in finding["evidence"][:4]:
                suffix = (f"#{ref['event_sequence']}"
                          if "event_sequence" in ref
                          and ref["record"] != str(ref["event_sequence"]) else "")
                lines.append(f"       evidence  {ref['table']}:{ref['record']}{suffix}")
            if len(finding["evidence"]) > 4:
                lines.append(f"       evidence  … {len(finding['evidence']) - 4} more")
            for cause in finding["possible_causes"]:
                lines.append(f"       possible  {cause}")
            for item in finding["missing_evidence"]:
                lines.append(f"       missing   {item}")
            if finding.get("confirmation"):
                who = finding["confirmation"].get("reviewer") or "unknown"
                lines.append(f"       confirmed by {who}")
    else:
        lines.append("")
        lines.append("findings  none — no executable rule fired on this run")

    if document["clues"]:
        lines.append("")
        lines.append("clues (facts, not conclusions)")
        for clue in document["clues"]:
            lines.append(f"  {clue['kind']}: {clue['claim']} ({clue.get('count', '?')})")
            lines.append(f"       {clue['note']}")

    lines.append("")
    lines.append("not evaluable")
    for item in document["not_evaluable"]:
        lines.append(f"  {item['label']:<4}{item['why']}")

    lines.append("")
    root = document["root_cause"]
    if root:
        lines.append(f"root cause  {root['label']}: {root['claim']}")
    else:
        lines.append("root cause  not established — rules stop at observed/suspected; "
                     "confirmation needs a human review that names evidence")

    if explain:
        lines.append("")
        lines.append("checks")
        for record in document["checked"]:
            observed = ", ".join(f"{key}={value}" for key, value in record["observed"].items())
            lines.append(f"  {record['rule']:<26} triggered={record['triggered']}  {observed}")
        if document["escalation"]["rejected"]:
            lines.append("")
            lines.append("rejected confirmations")
            for item in document["escalation"]["rejected"]:
                lines.append(f"  {item['reason']}")
        if document["escalation"]["unmatched"]:
            lines.append(f"  unmatched confirmation labels: "
                         f"{', '.join(document['escalation']['unmatched'])}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------- loader


class AttributeProjection:
    """Read-only load of one run's records, then the pure rules."""

    def __init__(self, database) -> None:
        self.database = database

    async def diagnose(self, run_id, *, case: dict | None = None, confirmations=()) -> dict:
        target = UUID(str(run_id))
        async with self.database.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            await connection.execute("SET LOCAL statement_timeout = '10s'")
            evidence = await self._load(connection, target)
        evidence["case"] = case
        return attribute(evidence, confirmations=confirmations)

    @staticmethod
    async def _load(connection, run_id: UUID) -> dict:
        cursor = await connection.execute(
            "SELECT r.id, r.session_id, r.status, r.end_reason, r.created_at, r.ended_at, "
            "w.root_path FROM runs r JOIN sessions s ON s.id = r.session_id "
            "JOIN workspaces w ON w.id = s.workspace_id WHERE r.id = %s", (run_id,))
        run = await cursor.fetchone()
        if run is None:
            raise LookupError("run not found")

        # Only the fields the rules read: the result body stays in the database,
        # because it can contain file content and command output. The repeat rule
        # needs "did this attempt return anything new", which a digest answers
        # without the body ever leaving Postgres.
        # Ordered by the model call that produced it, then by its position in
        # that call. ``call_index`` restarts at zero each iteration, so ordering
        # by the column alone would scatter one iteration's calls through the run.
        cursor = await connection.execute(
            "SELECT t.id, t.call_id, t.name, t.status, t.attempt, t.call_index, "
            "t.model_call_id, t.arguments, t.created_at, t.ended_at, "
            "t.result->>'is_error' AS is_error, "
            "t.result->>'outcome_unknown' AS outcome_unknown, "
            "t.result->'details' AS details, md5(t.result::text) AS result_digest "
            "FROM tool_calls t LEFT JOIN model_calls m ON m.id = t.model_call_id "
            "WHERE t.run_id = %s "
            "ORDER BY m.iteration NULLS LAST, t.call_index, t.created_at", (run_id,))
        tools = [dict(row) for row in await cursor.fetchall()]

        cursor = await connection.execute(
            "SELECT id, tool_record_id, tool_name, status, created_at, decided_at "
            "FROM approval_requests WHERE run_id = %s ORDER BY created_at", (run_id,))
        approvals = [dict(row) for row in await cursor.fetchall()]

        cursor = await connection.execute(
            "SELECT id, compaction_id, status, duration_ms, created_at FROM compaction_calls "
            "WHERE run_id = %s ORDER BY created_at", (run_id,))
        compactions = [dict(row) for row in await cursor.fetchall()]

        cursor = await connection.execute(
            "SELECT sequence, type, created_at, payload FROM run_events WHERE run_id = %s "
            "AND type = ANY(%s) ORDER BY sequence", (run_id, list(_EVENT_TYPES)))
        events = [dict(row) for row in await cursor.fetchall()]

        return {"run": dict(run), "tools": tools, "approvals": approvals,
                "compactions": compactions, "events": events}


#: Only the event types a rule reads. Loading every event would put text deltas
#: and payloads the rules never use through the export boundary.
_EVENT_TYPES = (
    "run_failed", "run_completed", "run_cancelled", "run_interrupted",
    "tool_started", "tool_completed", "tool_interrupted",
    "approval_requested", "approval_decided",
    "compaction_started", "compaction_completed", "compaction_discarded",
    "compaction_failed", "compaction_cancelled",
    "context_rejected", "recovery_projected", "boundary_applied",
)


__all__ = [
    "ATTRIBUTION_VERSION", "AttributeProjection", "BUDGET_REASONS", "CERTAINTIES",
    "EXECUTION_REASONS", "Finding", "LABELS", "RULE_VERSION", "Ref",
    "attribute", "format_diagnosis", "sample_id",
]
