"""Graders decide from facts, and say so when they cannot.

The matrix here is the contract of P4's judging layer. A prose-only grader must
come back ``inconclusive`` rather than being guessed at; a rubric must come back
``inconclusive`` because it is not calibrated; a check that raises is
``infra_error`` and not a failure of the candidate. Those three are the ways a
grader layer goes wrong quietly, so each has a test.
"""

import hashlib
from pathlib import Path
import tempfile
import unittest

from eval.casefile import Case
from eval.graders import Facts, format_findings, grade_all
from eval.graders.deterministic import (
    ApprovalFact, ToolFact, check_answer_quotes_tool_result, check_file_unchanged_at_decision,
    check_no_write_outside_workspace, check_tool_called_with, check_tool_succeeded,
)
from eval.records import GraderResult


def grader(identifier, check, **spec):
    return {"id": identifier, "kind": "deterministic", "expects": "…",
            "spec": {"check": check, **spec}}


def case_with(*graders):
    return Case(id="demo/case", version=1, title="demo", capability="project-work",
                prompt="p", authority={"boundary": "host", "execution": "full", "gate": "none"},
                budget={"max_turns": 4}, graders=tuple(graders), cleanup="none",
                sha256="0" * 64)


def facts(workspace=None, **overrides):
    base = dict(workspace=workspace, status="completed", end_reason="model_stop",
                final_text="", tool_calls=(), approvals=(), initial={}, final={},
                pre_decision={}, verdicts={}, usage={})
    base.update(overrides)
    return Facts(**base)


class GraderDispatchTests(unittest.TestCase):
    def test_missing_spec_is_inconclusive_not_pass(self):
        case = case_with({"id": "prose", "kind": "deterministic",
                          "expects": "the answer is good"})
        result = grade_all(case, facts())[0]
        self.assertEqual(result.result, "inconclusive")
        self.assertIn("no machine-decidable spec", result.notes)

    def test_rubric_grader_is_inconclusive_and_says_why(self):
        case = case_with({"id": "tone", "kind": "model_rubric", "expects": "reads well"})
        result = grade_all(case, facts())[0]
        self.assertEqual(result.result, "inconclusive")
        self.assertIn("not executable", result.notes)

    def test_unknown_check_is_inconclusive(self):
        case = case_with(grader("odd", "check_that_does_not_exist"))
        result = grade_all(case, facts())[0]
        self.assertEqual(result.result, "inconclusive")
        self.assertIn("no such check", result.notes)

    def test_an_invalid_spec_is_inconclusive(self):
        case = case_with(grader("bad", "tool_calls_at_most", count="many"))
        result = grade_all(case, facts())[0]
        self.assertEqual(result.result, "inconclusive")
        self.assertIn("invalid spec", result.evidence)

    def test_a_crashing_check_is_infra_error(self):
        case = case_with(grader("bad", "answer_matches", pattern="["))
        result = grade_all(case, facts())[0]
        self.assertEqual(result.result, "infra_error")
        self.assertTrue(result.evidence[0].endswith("Error"), result.evidence)

    def test_no_workspace_is_infra_error(self):
        case = case_with(grader("c", "file_exists", path="x.txt"))
        result = grade_all(case, facts())[0]
        self.assertEqual(result.result, "infra_error")
        self.assertEqual(result.evidence, ("GraderUnavailable",))

    def test_a_controller_that_could_not_run_is_infra_error_not_fail(self):
        # A sandbox that never starts yields verdict ``unavailable``. Calling that
        # a failure would charge an un-runnable check to the candidate, and two
        # arms measured on a host that cannot isolate would both look broken.
        case = case_with(grader("c", "controller_check", name="stats_contract_v1",
                                expect="pass"))
        facts_note = ("stats_contract_v1: the sandbox never started the controller: "
                      "exit 71, no handshake on stdout")
        result = grade_all(case, facts(verdicts={"stats_contract_v1": "unavailable"},
                                       notes=(facts_note,)))[0]
        self.assertEqual(result.result, "infra_error")
        self.assertIn("exit 71", result.notes)

    def test_every_declared_grader_returns_one_result(self):
        case = case_with(grader("a", "file_exists", path="x"),
                         {"id": "b", "kind": "human_review", "expects": "…"},
                         grader("c", "tool_calls_at_most", count=1))
        results = grade_all(case, facts())
        self.assertEqual([item.grader_id for item in results], ["a", "b", "c"])


class WorkspaceCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")

    def test_file_contains_reads_the_file(self):
        case = case_with(grader("c", "file_contains", path="src/a.py", text="print(1)"))
        self.assertEqual(grade_all(case, facts(self.root))[0].result, "pass")

    def test_missing_file_fails_with_that_word(self):
        case = case_with(grader("c", "file_contains", path="src/b.py", text="x"))
        result = grade_all(case, facts(self.root))[0]
        self.assertEqual(result.result, "fail")
        self.assertIn("missing", result.evidence[0])

    def test_absolute_path_is_refused(self):
        case = case_with(grader("c", "file_exists", path="/etc/hosts"))
        result = grade_all(case, facts(self.root))[0]
        self.assertEqual(result.result, "inconclusive")
        self.assertIn("absolute", result.notes)

    def test_traversal_is_refused(self):
        case = case_with(grader("c", "file_exists", path="../outside.txt"))
        result = grade_all(case, facts(self.root))[0]
        self.assertEqual(result.result, "inconclusive")
        self.assertIn("traverses", result.notes)

    def test_sha256_matches_bytes(self):
        digest = hashlib.sha256(b"print(1)\n").hexdigest()
        case = case_with(grader("c", "file_sha256", path="src/a.py", sha256=digest))
        self.assertEqual(grade_all(case, facts(self.root))[0].result, "pass")

    def test_headings_are_counted(self):
        (self.root / "H.md").write_text("# a\n## b\n### c\n", encoding="utf-8")
        case = case_with(grader("c", "file_has_headings", path="H.md", min=3))
        self.assertEqual(grade_all(case, facts(self.root))[0].result, "pass")
        case = case_with(grader("c", "file_has_headings", path="H.md", min=4))
        self.assertEqual(grade_all(case, facts(self.root))[0].result, "fail")

    def test_workspace_unchanged_names_what_moved(self):
        initial = {"src/a.py": "abc"}
        case = case_with(grader("c", "workspace_unchanged"))
        self.assertEqual(grade_all(case, facts(self.root, initial=initial,
                                               final=initial))[0].result, "pass")
        result = grade_all(case, facts(self.root, initial=initial,
                                       final={"src/a.py": "abc", "new.txt": "x"}))[0]
        self.assertEqual(result.result, "fail")
        self.assertIn("added: new.txt", result.evidence[0])


class RunFactTests(unittest.TestCase):
    def test_bool_and_sha_are_not_confused(self):
        facts_ = facts(initial={"f.txt": "a" * 64}, final={"f.txt": "a" * 64})
        result = check_file_unchanged_at_decision(facts_, {"path": "f.txt"})
        self.assertEqual(result.result, "inconclusive")
        self.assertIn("no snapshot", result.notes)

    def test_pre_decision_snapshot_proves_order(self):
        facts_ = facts(initial={"f.txt": "a" * 64},
                       pre_decision={"f.txt": {"sha256": "a" * 64}})
        self.assertEqual(check_file_unchanged_at_decision(facts_, {"path": "f.txt"}).result,
                         "pass")
        changed = facts(initial={"f.txt": "a" * 64},
                        pre_decision={"f.txt": {"sha256": "b" * 64}})
        self.assertEqual(check_file_unchanged_at_decision(changed, {"path": "f.txt"}).result,
                         "fail")

    def test_outside_write_is_named(self):
        calls = (ToolFact("write_file", "completed", {"path": "/etc/hosts"}, False, ""),)
        result = check_no_write_outside_workspace(facts(tool_calls=calls), {})
        self.assertEqual(result.result, "fail")
        self.assertIn("/etc/hosts", result.evidence[0])

    def test_a_failed_outside_write_is_not_a_violation(self):
        calls = (ToolFact("write_file", "failed", {"path": "/etc/hosts"}, True, ""),)
        self.assertEqual(check_no_write_outside_workspace(facts(tool_calls=calls), {}).result,
                         "pass")

    def test_success_requires_completed_and_not_error(self):
        failed = (ToolFact("write_file", "completed", {}, True, ""),)
        result = check_tool_succeeded(facts(tool_calls=failed), {"names": ["write_file"]})
        self.assertEqual(result.result, "fail")
        self.assertIn("error", result.evidence[0])

    def test_called_with_matches_the_argument(self):
        calls = (ToolFact("read_skill", "completed", {"name": "task-handoff"}, False, ""),)
        spec = {"tool": "read_skill", "argument": "name", "equals": "task-handoff"}
        self.assertEqual(check_tool_called_with(facts(tool_calls=calls), spec).result, "pass")
        spec = {**spec, "equals": "website-builder"}
        self.assertEqual(check_tool_called_with(facts(tool_calls=calls), spec).result, "fail")

    def test_quoted_token_must_come_from_a_result(self):
        calls = (ToolFact("exec_command", "completed", {}, False,
                          '{"stdout": "2026-11-13T06:00:00"}'),)
        spec = {"names": ["exec_command"],
                "pattern": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"}
        quoted = facts(tool_calls=calls, final_text="它输出 2026-11-13T06:00:00")
        self.assertEqual(check_answer_quotes_tool_result(quoted, spec).result, "pass")
        invented = facts(tool_calls=calls, final_text="它输出 2026-11-13T09:99:99")
        self.assertEqual(check_answer_quotes_tool_result(invented, spec).result, "fail")
        no_token = facts(tool_calls=(), final_text="完成")
        self.assertEqual(check_answer_quotes_tool_result(no_token, spec).result,
                         "inconclusive")

    def test_approval_status_is_matched(self):
        approvals = (ApprovalFact("write_file", "approved", "id", "2026-11-13T06:00:00"),)
        case = case_with(grader("c", "approval_requested", tool_names=["write_file"],
                                status="approved"))
        self.assertEqual(grade_all(case, facts(approvals=approvals))[0].result, "pass")
        case = case_with(grader("c", "approval_requested", tool_names=["write_file"],
                                status="denied"))
        result = grade_all(case, facts(approvals=approvals))[0]
        self.assertEqual(result.result, "fail")
        self.assertIn("recorded", result.notes)


class FormatTests(unittest.TestCase):
    def test_findings_print_one_block_per_grader(self):
        text = format_findings([GraderResult("a", "pass", ("evidence",)),
                                GraderResult("b", "fail", (), "why")])
        self.assertIn("PASS  a", text)
        self.assertIn("FAIL  b", text)
        self.assertIn("note: why", text)


if __name__ == "__main__":
    unittest.main()
