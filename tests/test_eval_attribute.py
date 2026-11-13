"""P2: attribution rules, the annotation ledger and the report's honesty.

The tests are grouped by the claim they defend rather than by function, because
the risk in this module is not that a rule crashes — it is that a rule overstates
what it knows. ``CleanRunTests`` and ``NegativeSampleTests`` therefore matter as
much as the ones that make a label fire.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from eval import annotations, cli
from eval.attribute import (CERTAINTIES, LABELS, RULE_VERSION, attribute,
                            format_diagnosis, sample_id)

BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
REPOSITORY = Path(__file__).resolve().parent.parent
RUN = "11111111-1111-1111-1111-111111111111"


# ------------------------------------------------------------------------ fixtures


def run_row(**overrides) -> dict:
    row = {"id": RUN, "session_id": "22222222-2222-2222-2222-222222222222",
           "status": "failed", "end_reason": "execution_error", "root_path": None}
    row.update(overrides)
    return row


def tool(call_id, *, name="exec_command", status="completed", arguments=None,
         digest=None, index=0, details=None, attempt=1) -> dict:
    return {"id": call_id, "name": name, "status": status,
            "arguments": {"argv": ["true"]} if arguments is None else arguments,
            "result_digest": digest, "call_index": index, "attempt": attempt,
            "details": details}


def event(sequence, type, **payload) -> dict:
    return {"sequence": sequence, "type": type, "payload": payload}


def evidence(**overrides) -> dict:
    bundle = {"run": run_row(), "tools": [], "approvals": [], "compactions": [],
              "events": [], "case": None}
    bundle.update(overrides)
    return bundle


def labels_of(document: dict) -> list[str]:
    return [finding["label"] for finding in document["findings"]]


def rules_of(document: dict) -> list[str]:
    return [finding["rule"] for finding in document["findings"]]


def checked(document: dict, rule: str) -> dict:
    return next(record for record in document["checked"] if record["rule"] == rule)


# --------------------------------------------------------------------------- catalog


class CatalogTests(unittest.TestCase):
    def test_every_plan_label_is_present_with_both_halves_of_its_contract(self):
        self.assertEqual(sorted(LABELS), sorted(f"F{index}" for index in range(1, 11)))
        for label in LABELS.values():
            self.assertTrue(label.observable.strip(), label.code)
            self.assertGreaterEqual(len(label.confirmation), 1, label.code)

    def test_certainty_vocabulary_is_the_three_the_plan_allows(self):
        self.assertEqual(CERTAINTIES, ("observed", "suspected", "confirmed"))

    def test_a_rule_never_emits_confirmed(self):
        document = attribute(evidence(tools=[tool("t1", status="failed")]), now=BASE)
        self.assertTrue(document["findings"])
        for finding in document["findings"]:
            self.assertIn(finding["certainty"], ("observed", "suspected"))

    def test_a_clean_run_still_reports_what_could_not_be_evaluated(self):
        document = attribute(evidence(run=run_row(status="completed", end_reason="model_stop")),
                             now=BASE)
        self.assertEqual(document["findings"], [])
        self.assertEqual([item["label"] for item in document["not_evaluable"]],
                         ["F1", "F2", "F3", "F6", "F10"])

    def test_every_finding_points_at_a_record(self):
        document = attribute(evidence(
            tools=[tool("t1", status="failed")],
            approvals=[{"id": "a1", "tool_name": "write_file", "status": "denied"}],
            events=[event(3, "run_failed", end_reason="storage_error")]), now=BASE)
        self.assertGreater(len(document["findings"]), 0)
        for finding in document["findings"]:
            self.assertTrue(finding["evidence"], finding["rule"])
            for ref in finding["evidence"]:
                self.assertTrue(ref["table"])
                self.assertTrue(ref["record"])


# ----------------------------------------------------------------------- rule: F5


class ExecutionRuleTests(unittest.TestCase):
    def test_failed_tool_is_observed_but_is_not_a_root_cause(self):
        document = attribute(evidence(tools=[tool("t1", status="failed")]), now=BASE)
        finding = document["findings"][0]
        self.assertEqual(finding["label"], "F5")
        self.assertEqual(finding["certainty"], "observed")
        self.assertIsNone(document["root_cause"])
        self.assertIn("which layer failed", " ".join(finding["missing_evidence"]))

    def test_unconfirmed_outcome_is_observed(self):
        document = attribute(evidence(run=run_row(status="failed", end_reason="execution_error"),
                                      tools=[tool("t1", status="unknown")]), now=BASE)
        finding = next(item for item in document["findings"]
                       if item["rule"] == "F5.tool-failure")
        self.assertIn("outcome could not be confirmed", " ".join(finding["possible_causes"]))

    def test_retry_after_unconfirmed_outcome_is_only_suspected(self):
        document = attribute(evidence(tools=[
            tool("t1", status="unknown", index=0),
            tool("t2", status="failed", index=1)]), now=BASE)
        finding = next(item for item in document["findings"]
                       if item["rule"] == "F5.retry-after-unknown")
        self.assertEqual(finding["certainty"], "suspected")
        self.assertEqual([ref["record"] for ref in finding["evidence"]], ["t1", "t2"])

    def test_no_retry_means_no_suspicion(self):
        document = attribute(evidence(tools=[tool("t1", status="unknown", index=0)]), now=BASE)
        self.assertNotIn("F5.retry-after-unknown", rules_of(document))

    def test_a_retry_in_the_next_iteration_is_still_a_retry(self):
        # ``call_index`` restarts at zero for every model call, so comparing the
        # column would miss exactly the retry that follows the failed iteration.
        document = attribute(evidence(tools=[
            tool("t1", status="unknown", index=3),
            tool("t2", status="failed", index=0)]), now=BASE)
        finding = next(item for item in document["findings"]
                       if item["rule"] == "F5.retry-after-unknown")
        self.assertEqual([ref["record"] for ref in finding["evidence"]], ["t1", "t2"])

    def test_end_reason_names_the_layer_without_naming_a_defect(self):
        document = attribute(evidence(run=run_row(status="failed", end_reason="storage_error")),
                             now=BASE)
        finding = next(item for item in document["findings"]
                       if item["rule"] == "F5.execution-end-reason")
        self.assertIn("storage layer", finding["possible_causes"][0])
        self.assertIn("specific defect", finding["missing_evidence"][0])

    def test_the_layer_list_does_not_include_a_successful_stop(self):
        document = attribute(evidence(run=run_row(status="completed", end_reason="model_stop")),
                             now=BASE)
        self.assertEqual(document["findings"], [])


# ----------------------------------------------------------------------- rule: F7


class LoopTests(unittest.TestCase):
    def test_three_identical_calls_with_identical_results(self):
        calls = [tool(f"t{index}", name="run_tests", arguments={"suite": "unit"},
                      digest="aaa", index=index) for index in range(3)]
        document = attribute(evidence(tools=calls), now=BASE)
        finding = next(item for item in document["findings"] if item["rule"] == "F7.repeat")
        self.assertEqual(finding["certainty"], "suspected")
        self.assertIn("3 times with identical results", finding["claim"])

    def test_a_new_result_breaks_the_claim(self):
        calls = [tool("t0", name="run_tests", arguments={"suite": "unit"}, digest="aaa", index=0),
                 tool("t1", name="run_tests", arguments={"suite": "unit"}, digest="aaa", index=1),
                 tool("t2", name="run_tests", arguments={"suite": "unit"}, digest="bbb", index=2)]
        document = attribute(evidence(tools=calls), now=BASE)
        self.assertNotIn("F7.repeat", rules_of(document))

    def test_two_calls_are_not_a_loop(self):
        calls = [tool("t0", name="run_tests", arguments={"suite": "unit"}, digest="aaa", index=0),
                 tool("t1", name="run_tests", arguments={"suite": "unit"}, digest="aaa", index=1)]
        document = attribute(evidence(tools=calls), now=BASE)
        self.assertNotIn("F7.repeat", rules_of(document))

    def test_same_file_at_a_different_offset_is_not_a_repeat(self):
        calls = [tool("t0", name="read_file", arguments={"path": "a.txt", "offset": 1},
                      digest="aaa", index=0),
                 tool("t1", name="read_file", arguments={"path": "a.txt", "offset": 2},
                      digest="aaa", index=1),
                 tool("t2", name="read_file", arguments={"path": "a.txt", "offset": 3},
                      digest="aaa", index=2)]
        document = attribute(evidence(tools=calls), now=BASE)
        self.assertNotIn("F7.repeat", rules_of(document))

    def test_guard_stop_is_observed(self):
        document = attribute(evidence(run=run_row(status="failed", end_reason="repeated_tool_call"),
                                      events=[event(9, "run_failed",
                                                    end_reason="repeated_tool_call")]), now=BASE)
        finding = next(item for item in document["findings"] if item["rule"] == "F7.guard-stop")
        self.assertEqual(finding["certainty"], "observed")


# ----------------------------------------------------------------------- rule: F8


class BudgetTests(unittest.TestCase):
    def test_turn_limit_is_a_recorded_fact(self):
        document = attribute(evidence(run=run_row(status="failed", end_reason="turn_limit")),
                             now=BASE)
        finding = next(item for item in document["findings"] if item["label"] == "F8")
        self.assertEqual(finding["certainty"], "observed")
        self.assertIn("turn_limit", finding["claim"])

    def test_context_budget_carries_the_numbers_that_explain_it(self):
        document = attribute(evidence(
            run=run_row(status="failed", end_reason="context_budget_exhausted"),
            events=[event(4, "context_rejected", required_tokens=8231, input_budget=7424)]),
            now=BASE)
        finding = next(item for item in document["findings"] if item["label"] == "F8")
        self.assertIn("8231", " ".join(finding["possible_causes"]))
        self.assertIn("7424", " ".join(finding["possible_causes"]))

    def test_a_cancelled_run_is_not_a_budget_stop(self):
        document = attribute(evidence(run=run_row(status="cancelled", end_reason="user_cancelled")),
                             now=BASE)
        self.assertEqual(document["findings"], [])

    def test_the_limit_is_not_reported_as_its_cause(self):
        document = attribute(evidence(run=run_row(status="failed", end_reason="turn_limit")),
                             now=BASE)
        finding = next(item for item in document["findings"] if item["label"] == "F8")
        self.assertIn("whether inefficiency", " ".join(finding["missing_evidence"]))


# ----------------------------------------------------------------------- rule: F9


class ApprovalTests(unittest.TestCase):
    def approval(self, **overrides) -> dict:
        row = {"id": "a1", "tool_name": "write_file", "status": "denied"}
        row.update(overrides)
        return row

    def test_denied_keeps_both_readings_open(self):
        document = attribute(evidence(run=run_row(status="completed", end_reason="model_stop"),
                                      approvals=[self.approval()]), now=BASE)
        finding = document["findings"][0]
        self.assertEqual(finding["label"], "F9")
        self.assertEqual(finding["certainty"], "observed")
        causes = " ".join(finding["possible_causes"])
        self.assertIn("correct interception", causes)
        self.assertIn("unnecessary friction", causes)

    def test_the_controller_note_is_reported_not_interpreted(self):
        document = attribute(evidence(
            run=run_row(status="completed", end_reason="model_stop"),
            approvals=[self.approval()],
            events=[event(8, "approval_decided", approval_id="a1", decision="denied",
                          note="no interactive approver")]), now=BASE)
        finding = document["findings"][0]
        self.assertIn("no interactive approver", " ".join(finding["possible_causes"]))

    def test_an_approved_request_is_the_normal_path(self):
        document = attribute(evidence(run=run_row(status="completed", end_reason="model_stop"),
                                      approvals=[self.approval(status="approved")]), now=BASE)
        self.assertEqual(document["findings"], [])

    def test_expiry_is_reported_as_ungranted(self):
        document = attribute(evidence(run=run_row(status="completed", end_reason="model_stop"),
                                      approvals=[self.approval(status="expired")]), now=BASE)
        self.assertIn("expired", document["findings"][0]["claim"])

    def test_a_case_expectation_is_compared_never_credited(self):
        case = {"id": "x", "version": 1, "authority": {"approvals": ["denied"]}}
        document = attribute(evidence(run=run_row(status="completed", end_reason="model_stop"),
                                      approvals=[self.approval()], case=case), now=BASE)
        finding = document["findings"][0]
        self.assertEqual(finding["observations"]["case_expectation"], "met")
        self.assertIsNone(document["root_cause"])

    def test_a_mismatched_expectation_is_recorded_as_unmet(self):
        case = {"id": "x", "version": 1, "authority": {"approvals": ["approved"]}}
        document = attribute(evidence(run=run_row(status="completed", end_reason="model_stop"),
                                      approvals=[self.approval()], case=case), now=BASE)
        self.assertEqual(document["findings"][0]["observations"]["case_expectation"], "unmet")


class NegativeSampleTests(unittest.TestCase):
    """Completed, cancelled and correctly refused runs must not become defects."""

    def test_a_completed_run_with_no_incident_has_no_labels(self):
        document = attribute(evidence(
            run=run_row(status="completed", end_reason="model_stop"),
            tools=[tool("t1", index=0), tool("t2", name="read_file", index=1)]), now=BASE)
        self.assertEqual(document["findings"], [])

    def test_a_correctly_refused_write_is_one_label_not_a_verdict(self):
        document = attribute(evidence(
            run=run_row(status="completed", end_reason="model_stop"),
            approvals=[{"id": "a1", "tool_name": "write_file", "status": "denied"}]),
            now=BASE)
        self.assertEqual(labels_of(document), ["F9"])
        self.assertIsNone(document["root_cause"])
        self.assertIn("a reviewer's judgement", " ".join(
            document["findings"][0]["missing_evidence"]))

    def test_an_interrupted_run_produces_no_failure_label(self):
        document = attribute(evidence(
            run=run_row(status="interrupted", end_reason="owner_lost")), now=BASE)
        self.assertEqual(document["findings"], [])


# ----------------------------------------------------------------------- rule: F4


class ParameterTests(unittest.TestCase):
    def test_corrected_input_is_suspected(self):
        document = attribute(evidence(tools=[
            tool("t1", status="failed", arguments={"path": "a.txt"}, index=0),
            tool("t2", status="completed", arguments={"path": "b.txt"}, index=1)]), now=BASE)
        finding = next(item for item in document["findings"] if item["label"] == "F4")
        self.assertEqual(finding["certainty"], "suspected")
        self.assertIn("which field", " ".join(finding["missing_evidence"]))

    def test_a_failure_with_no_successful_retry_is_not_a_correction(self):
        document = attribute(evidence(tools=[
            tool("t1", status="failed", arguments={"path": "a.txt"}, index=0)]), now=BASE)
        self.assertNotIn("F4.input-correction", rules_of(document))

    def test_repeating_the_same_input_is_not_a_correction(self):
        document = attribute(evidence(tools=[
            tool("t1", status="failed", arguments={"path": "a.txt"}, index=0),
            tool("t2", status="completed", arguments={"path": "a.txt"}, index=1)]), now=BASE)
        self.assertNotIn("F4.input-correction", rules_of(document))


# ---------------------------------------------------------------------- rule: F10


class EnvironmentTests(unittest.TestCase):
    def test_an_unusable_boundary_is_observed(self):
        document = attribute(evidence(events=[
            event(2, "boundary_applied", available=True, workspace_writable=True),
            event(3, "boundary_applied", available=False, workspace_writable=True)]), now=BASE)
        finding = next(item for item in document["findings"] if item["label"] == "F10")
        self.assertEqual(len(finding["evidence"]), 1)

    def test_a_healthy_boundary_is_checked_not_cleared(self):
        document = attribute(evidence(events=[
            event(2, "boundary_applied", available=True, workspace_writable=True)]), now=BASE)
        self.assertNotIn("F10", labels_of(document))
        self.assertEqual(checked(document, "F10.boundary-unusable")["triggered"], 0)
        self.assertNotIn("F10", [item["label"] for item in document["not_evaluable"]])

    def test_an_absent_boundary_leaves_the_label_unevaluable(self):
        document = attribute(evidence(), now=BASE)
        self.assertIn("F10", [item["label"] for item in document["not_evaluable"]])


# -------------------------------------------------------------------------- clues


class ClueTests(unittest.TestCase):
    def test_compaction_is_a_clue_and_never_an_F2_finding(self):
        document = attribute(evidence(
            run=run_row(status="completed", end_reason="model_stop"),
            compactions=[{"id": "c1", "status": "completed", "duration_ms": 6}],
            events=[event(5, "compaction_completed", tokens_before=6657, tokens_after=2387)]),
            now=BASE)
        self.assertNotIn("F2", labels_of(document))
        self.assertEqual(document["clues"][0]["kind"], "context_replaced")
        self.assertIn("F2", [item["label"] for item in document["not_evaluable"]])
        sizes = document["clues"][0]["observations"]["token_sizes"][0]
        self.assertEqual(sizes, {"before": 6657, "after": 2387})

    def test_recovery_from_an_earlier_run_is_a_clue(self):
        document = attribute(evidence(events=[
            event(1, "recovery_projected", fact_count=9, source_runs=[RUN])]), now=BASE)
        self.assertEqual(document["clues"][0]["kind"], "recovered_from_earlier_run")


# ------------------------------------------------------------------ confirmations


class ConfirmationTests(unittest.TestCase):
    def confirmation(self, **overrides) -> dict:
        record = {"sample_id": sample_id(RUN), "label": "F5", "verdict": "root_cause",
                  "reviewer": "alice", "review_version": 1, "note": "storage layer bug",
                  "evidence": ["tool_calls:t1"]}
        record.update(overrides)
        return record

    def test_a_review_with_evidence_confirms_the_label(self):
        document = attribute(evidence(tools=[tool("t1", status="failed")]),
                             confirmations=[self.confirmation()], now=BASE)
        finding = document["findings"][0]
        self.assertEqual(finding["certainty"], "confirmed")
        self.assertEqual(document["root_cause"]["label"], "F5")
        self.assertEqual(document["root_cause"]["confirmed_by"]["reviewer"], "alice")

    def test_a_confirmation_without_evidence_is_refused(self):
        document = attribute(evidence(tools=[tool("t1", status="failed")]),
                             confirmations=[self.confirmation(evidence=[])], now=BASE)
        self.assertEqual(document["findings"][0]["certainty"], "observed")
        self.assertIsNone(document["root_cause"])
        self.assertIn("must name evidence", document["escalation"]["rejected"][0]["reason"])

    def test_a_confirmation_for_a_quiet_label_is_reported_as_unmatched(self):
        document = attribute(evidence(run=run_row(status="completed", end_reason="model_stop")),
                             confirmations=[self.confirmation(label="F8", evidence=[f"runs:{RUN}"])], now=BASE)
        self.assertEqual(document["escalation"]["unmatched"], ["F8"])

    def test_a_clue_verdict_does_not_confirm_anything(self):
        document = attribute(evidence(tools=[tool("t1", status="failed")]),
                             confirmations=[self.confirmation(verdict="clue")], now=BASE)
        self.assertEqual(document["findings"][0]["certainty"], "observed")

    def test_pending_escalation_names_what_is_missing(self):
        document = attribute(evidence(tools=[tool("t1", status="failed")]), now=BASE)
        pending = document["escalation"]["pending"][0]
        self.assertEqual(pending["label"], "F5")
        self.assertTrue(pending["needs"])


# ----------------------------------------------------------------- deterministic


class OutputTests(unittest.TestCase):
    def test_the_same_records_produce_the_same_document(self):
        bundle = evidence(tools=[tool("t1", status="failed")],
                          events=[event(3, "run_failed", end_reason="storage_error")])
        first = json.dumps(attribute(bundle, now=BASE), sort_keys=True)
        second = json.dumps(attribute(bundle, now=BASE), sort_keys=True)
        self.assertEqual(first, second)

    def test_tool_arguments_do_not_leave_the_process(self):
        document = attribute(evidence(tools=[
            tool("t1", status="failed", arguments={"path": "a.txt", "secret": "TOP-SECRET-VALUE"})]),
            now=BASE)
        self.assertNotIn("TOP-SECRET-VALUE", json.dumps(document))

    def test_a_workspace_path_in_a_recorded_note_is_projected(self):
        document = attribute(evidence(
            run=run_row(status="completed", end_reason="model_stop", root_path="/tmp/ws"),
            approvals=[{"id": "a1", "tool_name": "exec_command", "status": "denied"}],
            events=[event(8, "approval_decided", approval_id="a1", decision="denied",
                          note="printf x > /tmp/ws/secret.txt")]), now=BASE)
        text = json.dumps(document)
        self.assertNotIn("/tmp/ws", text)
        self.assertIn("/workspace/secret.txt", text)

    def test_text_rendering_states_that_no_root_cause_was_established(self):
        text = format_diagnosis(attribute(evidence(tools=[tool("t1", status="failed")]), now=BASE))
        self.assertIn("root cause  not established", text)
        self.assertIn("not evaluable", text)

    def test_explain_lists_the_rules_that_were_checked(self):
        document = attribute(evidence(tools=[tool("t1", status="failed")]), now=BASE)
        text = format_diagnosis(document, explain=True)
        self.assertIn("F5.tool-failure", text)
        self.assertIn("F7.repeat", text)


# -------------------------------------------------------------------------- ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        self.ledger = self.directory / "labels.jsonl"

    def record(self, **overrides) -> dict:
        row = {"sample_id": "run:abc", "label": "F5", "verdict": "clue",
               "dataset": "tune", "reviewer": "alice", "certainty": "observed",
               "evidence": ["tool_calls:t1"], "note": "call failed"}
        row.update(overrides)
        return row

    def test_append_and_load_round_trip(self):
        annotations.append(self.record(), self.ledger)
        loaded = annotations.load(self.ledger)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].label, "F5")
        self.assertIsNotNone(loaded[0].recorded_at)

    def test_a_sample_cannot_be_in_both_datasets(self):
        annotations.append(self.record(dataset="tune"), self.ledger)
        with self.assertRaises(annotations.AnnotationInvalid) as caught:
            annotations.append(self.record(dataset="validation"), self.ledger)
        self.assertIn("already in the tune set", str(caught.exception))

    def test_an_unknown_label_is_refused(self):
        with self.assertRaises(annotations.AnnotationInvalid):
            annotations.append(self.record(label="F11"), self.ledger)

    def test_a_root_cause_without_evidence_is_refused(self):
        with self.assertRaises(annotations.AnnotationInvalid) as caught:
            annotations.append(self.record(verdict="root_cause", evidence=[]), self.ledger)
        self.assertIn("requires evidence", str(caught.exception))

    def test_not_a_defect_requires_a_note_saying_what_was_checked(self):
        with self.assertRaises(annotations.AnnotationInvalid):
            annotations.append(self.record(verdict="not_a_defect", evidence=[], note=""),
                              self.ledger)

    def test_a_revision_increments_the_version(self):
        annotations.append(self.record(), self.ledger)
        second = annotations.append(self.record(verdict="root_cause"), self.ledger)
        self.assertEqual(second.review_version, 2)
        self.assertEqual(len(annotations.load(self.ledger)), 2)

    def test_latest_keeps_the_newest_review_per_reviewer(self):
        annotations.append(self.record(), self.ledger)
        annotations.append(self.record(verdict="not_a_defect", evidence=[], note="it was fine"),
                           self.ledger)
        newest = annotations.latest(annotations.load(self.ledger))
        self.assertEqual(len(newest), 1)
        self.assertEqual(newest[0].verdict, "not_a_defect")


# -------------------------------------------------------------------------- report


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.ledger = self.directory / "labels.jsonl"

    def label(self, sample, label="F5", verdict="root_cause", dataset="validation",
              reviewer="alice", evidence=None) -> None:
        annotations.append({
            "sample_id": sample, "label": label, "verdict": verdict, "dataset": dataset,
            "reviewer": reviewer, "certainty": "observed",
            "evidence": ["tool_calls:t1"] if evidence is None else evidence,
            "note": "checked" if verdict == "not_a_defect" else ""}, self.ledger)

    def diagnosis(self, sample, labels=("F5",)) -> dict:
        return {"sample_id": sample, "rule_version": RULE_VERSION,
                "run": {"id": sample.split(":", 1)[1]},
                "findings": [{"label": label} for label in labels]}

    def test_no_validation_sample_yields_null_with_a_reason(self):
        report = annotations.evaluate([], [])
        for label, row in report["labels"].items():
            self.assertIsNone(row["precision"]["value"], label)
            self.assertTrue(row["precision"]["reason"], label)

    def test_precision_uses_validation_only(self):
        self.label("run:a", dataset="tune")
        self.label("run:b", dataset="validation")
        self.label("run:c", dataset="validation")
        records = annotations.load(self.ledger)
        report = annotations.evaluate(records, [
            self.diagnosis("run:a", ("F5",)),
            self.diagnosis("run:b", ("F5",)),
            self.diagnosis("run:c", ("F5",)),
        ])
        self.assertEqual(report["samples"]["tune"], 1)
        self.assertEqual(report["samples"]["validation"], 2)
        self.assertEqual(report["labels"]["F5"]["precision"]["value"], 1.0)
        self.assertEqual(report["labels"]["F5"]["precision"]["n"], 2)

    def test_a_false_positive_lowers_precision(self):
        self.label("run:b")
        self.label("run:c", verdict="not_a_defect", evidence=[])
        records = annotations.load(self.ledger)
        report = annotations.evaluate(records, [
            self.diagnosis("run:b", ("F5",)), self.diagnosis("run:c", ("F5",))])
        self.assertEqual(report["labels"]["F5"]["precision"]["value"], 0.5)

    def test_a_missed_positive_is_counted_as_recall_loss(self):
        self.label("run:b")
        records = annotations.load(self.ledger)
        report = annotations.evaluate(records, [self.diagnosis("run:b", ())])
        self.assertEqual(report["labels"]["F5"]["recall"]["value"], 0.0)
        self.assertEqual(report["labels"]["F5"]["validation"]["tn"], 0)

    def test_disagreement_is_disputed_not_averaged(self):
        self.label("run:b", reviewer="alice")
        self.label("run:b", reviewer="bob", verdict="not_a_defect", evidence=[])
        records = annotations.load(self.ledger)
        report = annotations.evaluate(records, [self.diagnosis("run:b", ("F5",))])
        self.assertEqual(len(report["disputed"]), 1)
        self.assertEqual(report["labels"]["F5"]["validation"]["disputed"], 1)
        self.assertIsNone(report["labels"]["F5"]["precision"]["value"])

    def test_agreement_is_not_estimable_with_one_reviewer(self):
        self.label("run:b")
        block = annotations.agreement(annotations.load(self.ledger))
        self.assertIsNone(block["rate"])
        self.assertIn("two people", block["reason"])

    def test_two_reviewers_can_agree(self):
        self.label("run:b", reviewer="alice")
        self.label("run:b", reviewer="bob")
        block = annotations.agreement(annotations.load(self.ledger))
        self.assertEqual(block["rate"], 1.0)

    def test_the_report_says_when_no_accuracy_is_claimed(self):
        text = annotations.format_report(annotations.evaluate([], []))
        self.assertIn("no accuracy is claimed", text)
        self.assertIn("not estimable", text)


class P2GapRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ledger = Path(self.temporary.name) / 'labels.jsonl'

    def record(self, **changes):
        row = {'sample_id': sample_id(RUN), 'label': 'F5', 'verdict': 'root_cause',
               'dataset': 'validation', 'reviewer': 'alice', 'certainty': 'confirmed',
               'evidence': ['tool_calls:t1'], 'note': 'reviewed', 'review_version': 1}
        row.update(changes)
        return row

    def diagnosis(self, sample=None, labels=('F5',), **changes):
        sample = sample or sample_id(RUN)
        row = {'sample_id': sample, 'rule_version': RULE_VERSION,
               'run': {'id': sample.split(':', 1)[1]},
               'findings': [{'label': label} for label in labels]}
        row.update(changes)
        return row

    def diagnose(self, records):
        return attribute(evidence(tools=[tool('t1', status='failed')]), confirmations=records)

    def test_cli_latest_retraction_removes_old_root_cause(self):
        annotations.append(self.record(), self.ledger)
        annotations.append(self.record(verdict='not_a_defect', evidence=[], note='retracted'), self.ledger)
        records = cli._confirmations(self.ledger, sample_id(RUN))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['verdict'], 'not_a_defect')
        self.assertIsNone(self.diagnose(records)['root_cause'])

    def test_direct_confirmation_input_also_respects_latest_revision(self):
        old = self.record()
        new = self.record(verdict='clue', review_version=2)
        for records in ([old, new], [new, old]):
            self.assertIsNone(self.diagnose(records)['root_cause'])

    def test_reviewer_disagreement_blocks_confirmation(self):
        for verdict in ('not_a_defect', 'clue'):
            with self.subTest(verdict=verdict):
                records = [self.record(), self.record(reviewer='bob', verdict=verdict)]
                document = self.diagnose(records)
                self.assertIsNone(document['root_cause'])
                self.assertTrue(any('conflicting' in entry['reason']
                                    for entry in document['escalation']['rejected']))

    def test_confirmation_rejects_other_sample_or_run(self):
        for changes in ({'sample_id': 'run:another'}, {'run_id': 'another'}):
            self.assertIsNone(self.diagnose([self.record(**changes)])['root_cause'])

    def test_malformed_evidence_is_rejected_at_write_and_confirmation(self):
        for refs in ([' '], ['junk'], ['tool_calls:'], ['other_table:t1'], ['tool_calls:a b'],
                     'tool_calls:t1', [False]):
            with self.subTest(refs=refs):
                record = self.record(evidence=refs)
                with self.assertRaises(annotations.AnnotationInvalid):
                    annotations.append(record, self.ledger)
                self.assertIsNone(self.diagnose([record])['root_cause'])

    def test_existing_reference_must_belong_to_current_input(self):
        document = self.diagnose([self.record(evidence=['tool_calls:foreign'])])
        self.assertIsNone(document['root_cause'])
        self.assertTrue(any('not present' in entry['reason']
                            for entry in document['escalation']['rejected']))

    def test_review_confirms_only_findings_linked_to_its_evidence(self):
        document = self.diagnose([self.record()])
        self.assertEqual([f['certainty'] for f in document['findings']], ['confirmed', 'observed'])
        self.assertEqual(document['findings'][0]['confirmation']['evidence'], ['tool_calls:t1'])

    def test_conflicting_same_version_does_not_depend_on_order(self):
        a = self.record()
        b = self.record(verdict='clue')
        for records in ([a, b], [b, a]):
            with self.assertRaises(ValueError):
                self.diagnose(records)

    def test_concurrent_partition_writes_accept_only_one_dataset(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        gate = Barrier(2)

        def write(dataset):
            gate.wait(timeout=5)
            try:
                return annotations.append(self.record(dataset=dataset), self.ledger).dataset
            except annotations.AnnotationInvalid:
                return 'rejected'

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, dataset) for dataset in ('tune', 'validation')]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results.count('rejected'), 1)
        self.assertEqual(len(annotations.load(self.ledger)), 1)

    def test_concurrent_revisions_remain_unique_and_complete(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(annotations.append, self.record(), self.ledger) for _ in range(16)]
            versions = [future.result(timeout=10).review_version for future in futures]
        self.assertEqual(sorted(versions), list(range(1, 17)))
        self.assertEqual(len(annotations.load(self.ledger)), 16)

    def test_cross_process_append_waits_for_ledger_lock_then_checks_partition(self):
        import time
        annotations.append(self.record(dataset='tune'), self.ledger)
        ready = Path(self.temporary.name) / 'ready'
        script = (
            'import json,sys; from pathlib import Path; from eval import annotations; '
            'Path(sys.argv[2]).touch(); '
            'annotations.append(json.loads(sys.argv[3]),sys.argv[1])')
        process = None
        try:
            with annotations._locked(self.ledger, exclusive=True):
                process = subprocess.Popen([sys.executable, '-c', script, str(self.ledger),
                                            str(ready), json.dumps(self.record(dataset='validation'))],
                                           cwd=REPOSITORY, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, env={**os.environ, 'PYTHONPATH': str(REPOSITORY / 'src')})
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=.2)
            _, error = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn('already in the tune set', error)
            self.assertEqual(len(annotations.load(self.ledger)), 1)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    def test_loading_mixed_partition_history_is_rejected(self):
        self.ledger.write_text('\n'.join(json.dumps(self.record(dataset=d))
                                         for d in ('tune', 'validation')) + '\n')
        with self.assertRaises(annotations.AnnotationInvalid):
            annotations.load(self.ledger)

    def test_report_rejects_mixed_partitions_even_for_direct_records(self):
        records = [annotations.Annotation(**self.record(dataset=d)) for d in ('tune', 'validation')]
        with self.assertRaises(annotations.AnnotationInvalid):
            annotations.evaluate(records, [self.diagnosis()])

    def test_missing_diagnoses_never_become_false_negatives_or_true_negatives(self):
        for verdict in ('root_cause', 'not_a_defect'):
            with self.subTest(verdict=verdict):
                records = [annotations.Annotation(**self.record(verdict=verdict))]
                report = annotations.evaluate(records, [])
                row = report['labels']['F5']
                self.assertEqual(sum(row['validation'][key] for key in ('tp', 'fp', 'fn', 'tn')), 0)
                self.assertEqual(row['validation']['without_diagnosis'], 1)
                self.assertIsNone(row['recall']['value'])
                self.assertEqual(report['samples']['missing_diagnoses'], [sample_id(RUN)])
                self.assertIn('without diagnosis', annotations.format_report(report))

    def test_metrics_use_only_the_evaluated_subset(self):
        records = [annotations.Annotation(**self.record()),
                   annotations.Annotation(**self.record(sample_id='run:missing'))]
        report = annotations.evaluate(records, [self.diagnosis()])
        self.assertEqual(report['labels']['F5']['recall'], {'value': 1., 'n': 1, 'reason': None})
        self.assertEqual(report['samples']['without_diagnosis'], 1)
        self.assertEqual(report['samples']['evaluated'], 1)

    def test_missing_or_old_rule_version_is_rejected(self):
        for version in (None, 'attr-1'):
            with self.assertRaises(annotations.AnnotationInvalid):
                annotations.evaluate([], [self.diagnosis(rule_version=version)])

    def test_duplicate_diagnoses_are_rejected_in_both_orders(self):
        a, b = self.diagnosis(), self.diagnosis(labels=())
        for documents in ([a, b], [b, a], [a, a]):
            with self.assertRaises(annotations.AnnotationInvalid):
                annotations.evaluate([], documents)

    def test_mismatched_diagnosis_identity_is_rejected(self):
        with self.assertRaises(annotations.AnnotationInvalid):
            annotations.evaluate([], [self.diagnosis(run={'id': 'another'})])

    def test_not_evaluable_labels_do_not_become_negative_predictions(self):
        record = annotations.Annotation(**self.record())
        report = annotations.evaluate([record], [self.diagnosis(labels=(), not_evaluable=[{'label': 'F5'}])])
        row = report['labels']['F5']
        self.assertEqual(row['validation']['not_evaluable'], 1)
        self.assertIsNone(row['recall']['value'])

    def test_failed_or_discarded_compaction_never_claims_context_replacement(self):
        for status in ('started', 'failed', 'cancelled', 'discarded'):
            with self.subTest(status=status):
                document = attribute(evidence(events=[event(1, f'compaction_{status}', compaction_id='c')]))
                self.assertNotIn('context_replaced', [c['kind'] for c in document['clues']])
                self.assertIn(f'compaction_{status}', [c['kind'] for c in document['clues']])

    def test_call_rows_without_installation_do_not_prove_replacement(self):
        document = attribute(evidence(compactions=[{'id': 'call', 'status': 'completed'}]))
        self.assertEqual([c['kind'] for c in document['clues']], ['compaction_calls_recorded'])

    def test_completed_event_without_call_rows_proves_one_installation(self):
        document = attribute(evidence(events=[event(1, 'compaction_completed', compaction_id='fresh')]))
        self.assertEqual(document['clues'][0]['kind'], 'context_replaced')
        self.assertEqual(document['clues'][0]['count'], 1)

    def test_model_call_count_does_not_inflate_installation_count(self):
        document = attribute(evidence(compactions=[{'id': 'c1'}, {'id': 'c2'}], events=[
            event(1, 'compaction_completed', compaction_id='one-summary')]))
        self.assertEqual(document['clues'][0]['count'], 1)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class AttributionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """The loader and its SQL, against real Postgres.

    The fakes above prove the rules; these prove that what the rules read is
    what the database actually returns, including the result digest the repeat
    rule depends on.
    """

    async def asyncSetUp(self):
        from psycopg import AsyncConnection, sql
        from psycopg.conninfo import make_conninfo

        from chagent.config import Settings
        from chagent.storage.database import Database
        from chagent.storage.migrate import migrate

        self.schema = "chagent_attr_" + uuid4().hex
        self.admin = await AsyncConnection.connect(
            os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self._drop)
        settings = Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}"))
        self.database = Database(settings)
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)

        from psycopg.types.json import Jsonb

        self.run_id, session_id, workspace_id = uuid4(), uuid4(), uuid4()
        model_id, compaction_id = uuid4(), uuid4()
        repeat_ids = [uuid4(), uuid4(), uuid4()]
        async with self.database.transaction() as connection:
            await connection.execute("INSERT INTO workspaces (id, root_path) VALUES (%s, %s)",
                                     (workspace_id, "/tmp/chagent-attr"))
            await connection.execute(
                "INSERT INTO sessions (id, workspace_id, title) VALUES (%s, %s, 'attr')",
                (session_id, workspace_id))
            await connection.execute(
                "INSERT INTO runs (id, session_id, status, end_reason, started_at, ended_at) "
                "VALUES (%s, %s, 'failed', 'context_budget_exhausted', "
                "now() - interval '20 seconds', now())", (self.run_id, session_id))
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, status, request, "
                "context_snapshot, response, created_at, ended_at) VALUES "
                "(%s, %s, 0, 'completed', %s, %s, %s, now() - interval '18 seconds', "
                "now() - interval '15 seconds')",
                (model_id, self.run_id, Jsonb({"model": {"id": "test"}}), Jsonb({}),
                 Jsonb({"usage": {"total_tokens": 10}})))

            # Three identical calls whose stored results are byte-identical: the
            # only shape the repeat rule calls a loop.
            for index, call_id in enumerate(repeat_ids):
                await connection.execute(
                    "INSERT INTO tool_calls (id, run_id, model_call_id, call_id, name, "
                    "arguments, status, result, call_index, attempt, created_at, ended_at) "
                    "VALUES (%s, %s, %s, %s, 'run_tests', %s, 'completed', %s, %s, 1, "
                    "now() - interval '10 seconds', now() - interval '9 seconds')",
                    (call_id, self.run_id, model_id, f"call-{index}",
                     Jsonb({"suite": "unit"}), Jsonb({"content": [{"text": "0 failed"}]}), index))

            failed_id = uuid4()
            await connection.execute(
                "INSERT INTO tool_calls (id, run_id, model_call_id, call_id, name, arguments, "
                "status, result, call_index, attempt, created_at, ended_at) VALUES "
                "(%s, %s, %s, 'call-fail', 'apply_patch', %s, 'failed', %s, 3, 1, "
                "now() - interval '5 seconds', now() - interval '4 seconds')",
                (failed_id, self.run_id, model_id, Jsonb({"path": "a.txt"}),
                 Jsonb({"is_error": True, "details": None, "content": []})))
            self.failed_id = failed_id

            approval_id = uuid4()
            await connection.execute(
                "INSERT INTO approval_requests (id, run_id, tool_record_id, fingerprint, "
                "tool_name, cwd, subject, status, owner_token, expires_at, decided_at, "
                "created_at) VALUES (%s, %s, %s, 'f', 'exec_command', '/tmp', 'write', "
                "'denied', 'owner', now() + interval '1 minute', now() - interval '19 seconds', "
                "now() - interval '19 seconds')",
                (approval_id, self.run_id, failed_id))

            await connection.execute(
                "INSERT INTO compaction_calls (id, compaction_id, run_id, iteration, status, "
                "request, duration_ms, created_at, ended_at) VALUES "
                "(%s, %s, %s, 0, 'completed', %s, 7, now() - interval '17 seconds', "
                "now() - interval '17 seconds')",
                (uuid4(), compaction_id, self.run_id, Jsonb({})))

            for sequence, kind, payload in (
                    (1, "run_started", {"session_id": str(session_id)}),
                    (2, "context_rejected", {"required_tokens": 8231, "input_budget": 7424}),
                    (3, "approval_decided", {"approval_id": str(approval_id),
                                             "decision": "denied",
                                             "note": "no interactive approver"}),
                    (4, "compaction_completed", {"compaction_id": str(compaction_id),
                                                 "tokens_before": 6657, "tokens_after": 2387}),
                    (5, "run_failed", {"end_reason": "context_budget_exhausted"})):
                await connection.execute(
                    "INSERT INTO run_events (run_id, sequence, type, payload) "
                    "VALUES (%s, %s, %s, %s)", (self.run_id, sequence, kind, Jsonb(payload)))

    async def _drop(self):
        from psycopg import sql

        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
            sql.Identifier(self.schema)))

    async def test_the_loader_returns_records_the_rules_turn_into_labels(self):
        from eval.attribute import AttributeProjection

        document = await AttributeProjection(self.database).diagnose(self.run_id)
        self.assertEqual(sorted(set(labels_of(document))), ["F5", "F7", "F8", "F9"])
        self.assertIsNone(document["root_cause"])
        self.assertEqual(document["sample_id"], f"run:{self.run_id}")
        self.assertEqual(document["clues"][0]["kind"], "context_replaced")

    async def test_identical_stored_results_are_the_repeat_signal(self):
        from eval.attribute import AttributeProjection

        document = await AttributeProjection(self.database).diagnose(self.run_id)
        finding = next(item for item in document["findings"] if item["rule"] == "F7.repeat")
        self.assertEqual(finding["certainty"], "suspected")
        self.assertEqual(len(finding["evidence"]), 3)

    async def test_the_budget_finding_carries_the_stored_numbers(self):
        from eval.attribute import AttributeProjection

        document = await AttributeProjection(self.database).diagnose(self.run_id)
        finding = next(item for item in document["findings"] if item["label"] == "F8")
        self.assertIn("8231", " ".join(finding["possible_causes"]))

    async def test_a_case_expectation_reaches_the_approval_reading(self):
        from eval.attribute import AttributeProjection

        case = {"id": "execution-boundary/approval-gated-write", "version": 2,
                "authority": {"approvals": ["denied"]}, "sha256": "a" * 64}
        document = await AttributeProjection(self.database).diagnose(
            self.run_id, case=case)
        finding = next(item for item in document["findings"] if item["label"] == "F9")
        self.assertEqual(finding["observations"]["case_expectation"], "met")
        self.assertEqual(document["case"]["sha256"], "a" * 64)

    async def test_the_exported_document_has_no_workspace_path(self):
        from eval.attribute import AttributeProjection

        document = await AttributeProjection(self.database).diagnose(self.run_id)
        self.assertNotIn("/tmp/chagent-attr", json.dumps(document))

    async def test_cli_attribute_prints_json_and_text(self):
        url = (os.environ["CHAGENT_TEST_DATABASE_URL"]
               + f"?options=-csearch_path%3D{self.schema}")
        base = [sys.executable, "-m", "eval.cli", "attribute", str(self.run_id), "--db", url]
        environment = {**os.environ, "PYTHONPATH": "src", "NO_PROXY": "127.0.0.1,localhost"}

        done = subprocess.run(base + ["--json"], cwd=REPOSITORY, capture_output=True,
                              text=True, env=environment)
        self.assertEqual(done.returncode, 0, done.stderr)
        document = json.loads(done.stdout)
        self.assertEqual(document["run"]["id"], str(self.run_id))
        self.assertEqual(document["rule_version"], RULE_VERSION)

        done = subprocess.run(base + ["--explain"], cwd=REPOSITORY, capture_output=True,
                              text=True, env=environment)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("root cause  not established", done.stdout)
        self.assertIn("F7.repeat", done.stdout)

        missing = subprocess.run(
            [sys.executable, "-m", "eval.cli", "attribute", str(uuid4()), "--db", url],
            cwd=REPOSITORY, capture_output=True, text=True, env=environment)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("run not found", missing.stderr)


if __name__ == "__main__":
    unittest.main()
