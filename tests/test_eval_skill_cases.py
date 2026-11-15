"""Skill cases must decide something, and decide it in both directions.

A negative case is easy to write and easy to write uselessly. Two failures are
possible and each gets a test here:

* the case asserts only "no skill was read", so a run that does nothing at all
  passes it — the task's own output has to be graded too;
* the case claims to be negative but no grader actually looks at ``read_skill``,
  so the claim is prose in a file that nobody executes.

The third test is the one that matters: the same case must pass when the skill is
left alone and fail when it is loaded. Everything else is bookkeeping.

The last class does the same job for the positive case: ``explicit-name`` claims
the named skill was read *before* the work started, and a tool list cannot show
that. Its tests check the claim is decided by ordering, in both directions.
"""

import hashlib
import shutil
from pathlib import Path
import tempfile
import unittest

from eval import casefile
from eval.graders import grade_all
from eval.graders.deterministic import CHECKS, Facts, ToolFact

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "eval" / "cases"
NEGATIVE_CASES = ("skill-selection-line-count-negative-host.json",
                  "skill-selection-single-fact-negative-host.json")
#: Checks that show the task itself was done. A negative case needs at least one:
#: without it, silence and success are indistinguishable.
TASK_OUTPUT_CHECKS = frozenset({"file_exists", "file_contains", "file_matches", "file_has_headings",
                                "file_sha256", "answer_contains", "answer_matches"})


def snapshot(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def skill_read(name="project-explorer"):
    return ToolFact(name="read_skill", status="completed", arguments={"name": name},
                    failed=False, text="")


class SkillNegativeCaseTests(unittest.TestCase):
    def load(self, filename):
        return casefile.load(CASES / filename)

    def graded(self, case, *, files=None, tool_calls=(), answer=""):
        """Run the case's graders against a copy of its own fixture."""
        source = ROOT / case.fixture["path"]
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copytree(source, root, dirs_exist_ok=True)
        initial = snapshot(root)
        for name, text in (files or {}).items():
            (root / name).write_text(text, encoding="utf-8")
        final = snapshot(root)
        facts = Facts(workspace=root, status="completed", end_reason="model_stop",
                      final_text=answer, tool_calls=tuple(tool_calls), approvals=(),
                      initial=initial, final=final, pre_decision={}, verdicts={}, usage={},
                      notes=())
        return {result.grader_id: result for result in grade_all(case, facts)}

    def test_every_negative_case_is_runnable(self):
        for filename in NEGATIVE_CASES:
            with self.subTest(case=filename):
                case = self.load(filename)
                self.assertEqual(case.authority["boundary"], "host")
                self.assertEqual(case.capability, "skill-selection")
                for grader in case.graders:
                    self.assertEqual(grader["kind"], "deterministic", grader["id"])
                    check = (grader.get("spec") or {}).get("check")
                    self.assertIn(check, CHECKS, grader["id"])

    def test_every_negative_case_watches_read_skill(self):
        for filename in NEGATIVE_CASES:
            with self.subTest(case=filename):
                specs = [grader["spec"] for grader in self.load(filename).graders]
                watched = [spec for spec in specs
                           if spec["check"] == "tool_not_called"
                           and "read_skill" in (spec.get("names") or ())]
                self.assertEqual(len(watched), 1, "the negative claim has to be executed")

    def test_every_negative_case_also_grades_the_task(self):
        for filename in NEGATIVE_CASES:
            with self.subTest(case=filename):
                checks = {grader["spec"]["check"] for grader in self.load(filename).graders}
                self.assertTrue(checks & TASK_OUTPUT_CHECKS,
                                "a case that only forbids cannot tell silence from success")

    def test_the_line_count_case_fails_when_a_skill_is_read(self):
        case = self.load("skill-selection-line-count-negative-host.json")
        quiet = self.graded(case, files={"count.txt": "5\n"})
        self.assertEqual(quiet["no_skill_loaded"].result, "pass")
        self.assertEqual(quiet["count_written"].result, "pass")
        noisy = self.graded(case, files={"count.txt": "5\n"}, tool_calls=(skill_read(),))
        self.assertEqual(noisy["no_skill_loaded"].result, "fail")
        self.assertIn("read_skill: completed", noisy["no_skill_loaded"].evidence[0])
        # Reading a skill does not excuse the task: the other grader still decides.
        self.assertEqual(noisy["count_written"].result, "pass")

    def test_the_line_count_case_rejects_prose_instead_of_the_number(self):
        case = self.load("skill-selection-line-count-negative-host.json")
        result = self.graded(case, files={"count.txt": "总共 5 行\n"})
        self.assertEqual(result["count_written"].result, "fail")

    def test_the_single_fact_case_fails_when_a_skill_is_read(self):
        case = self.load("skill-selection-single-fact-negative-host.json")
        quiet = self.graded(case, answer="7200")
        self.assertEqual({result.result for result in quiet.values()}, {"pass"})
        noisy = self.graded(case, answer="7200", tool_calls=(skill_read("project-explorer"),))
        self.assertEqual(noisy["no_skill_loaded"].result, "fail")
        self.assertEqual(noisy["answer_states_the_value"].result, "pass")

    def test_doing_nothing_does_not_pass_either_case(self):
        for filename in NEGATIVE_CASES:
            with self.subTest(case=filename):
                results = self.graded(self.load(filename))
                self.assertIn("fail", {result.result for result in results.values()},
                              "a silent run satisfied every grader")

    def test_fixtures_stay_inside_the_checkout(self):
        for filename in NEGATIVE_CASES:
            with self.subTest(case=filename):
                fixture = (ROOT / self.load(filename).fixture["path"]).resolve()
                self.assertTrue(fixture.is_relative_to(ROOT))
                self.assertTrue(fixture.is_dir())


class ExplicitNameCaseTests(unittest.TestCase):
    """The positive case has to decide order, not just presence."""

    FILENAME = "skill-selection-explicit-name.json"
    HANDOFF = "# 交接\n\n## 目标与约束\n\n交付交接说明。\n\n## 已完成\n\n读过技能。\n\n## 继续入口\n\n无。\n"

    def load(self):
        return casefile.load(CASES / self.FILENAME)

    def graded(self, *, tool_calls, handoff=True, extra_skill=None):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        if handoff:
            (root / "HANDOFF.md").write_text(self.HANDOFF, encoding="utf-8")
        calls = tuple(tool_calls)
        if extra_skill:
            calls = calls + (skill_read(extra_skill),)
        facts = Facts(workspace=root, status="completed", end_reason="model_stop",
                      final_text="", tool_calls=calls, approvals=(),
                      initial={}, final=snapshot(root), pre_decision={}, verdicts={},
                      usage={}, notes=())
        return {result.grader_id: result for result in grade_all(self.load(), facts)}

    @staticmethod
    def write():
        return ToolFact(name="create_file", status="completed",
                        arguments={"path": "HANDOFF.md"}, failed=False, text="")

    def test_the_case_is_executable(self):
        case = self.load()
        self.assertEqual(case.version, 2)
        self.assertEqual(case.authority["boundary"], "host")
        for grader in case.graders:
            self.assertEqual(grader["kind"], "deterministic", grader["id"])
            self.assertIn((grader.get("spec") or {}).get("check"), CHECKS, grader["id"])

    def test_the_order_claim_is_executed_not_written_down(self):
        specs = [grader["spec"] for grader in self.load().graders]
        ordering = [spec for spec in specs if spec["check"] == "tool_called_before"]
        self.assertEqual(len(ordering), 1, "the ordering claim has no grader")
        self.assertEqual(ordering[0]["equals"], "task-handoff")
        # A write in any of its recorded spellings has to stop the clock.
        self.assertTrue({"create_file", "write_file"} <= set(ordering[0]["before"]))

    def test_read_then_write_passes_and_write_then_read_fails(self):
        before = self.graded(tool_calls=(skill_read("task-handoff"), self.write()))
        self.assertEqual({result.result for result in before.values()}, {"pass"})
        after = self.graded(tool_calls=(self.write(), skill_read("task-handoff")))
        self.assertEqual(after["read_before_first_write"].result, "fail")
        # Reading late still leaves the file and the structure: only order changed.
        self.assertEqual(after["document_written"].result, "pass")
        self.assertEqual(after["skill_was_read"].result, "pass")

    def test_a_second_skill_read_is_caught(self):
        result = self.graded(tool_calls=(skill_read("task-handoff"), self.write()),
                             extra_skill="project-explorer")
        self.assertEqual(result["no_substitute_skill"].result, "fail")
        self.assertIn("project-explorer", result["no_substitute_skill"].evidence[0])
        self.assertEqual(result["skill_was_read"].result, "pass")


IMPLICIT_CASES = {
    "skill-selection-explore-unfamiliar-project-host.json": "project-explorer",
    "skill-selection-synthesize-conflicting-notes-host.json": "document-synthesizer",
    "skill-selection-review-commit-diff-host.json": "change-reviewer",
    "skill-selection-verify-delivery-against-requirements-host.json": "project-verifier",
    "skill-selection-handoff-implicit-host.json": "task-handoff",
}

#: What a run has to produce for the task graders, per case. Kept next to the
#: case map so the "the task was done too" claim is exercised, not asserted.
TASK_OUTPUT = {
    "skill-selection-explore-unfamiliar-project-host.json": {
        "answer": "入口在 service/main.py，默认端口 8765，路由含 GET /health。"},
    "skill-selection-synthesize-conflicting-notes-host.json": {
        "answer": "v2 写 300，rollout 写 120，两者对不上。"},
    "skill-selection-review-commit-diff-host.json": {
        "answer": "total_pages 用整除，10 条 3 条一页会少一页。"},
    "skill-selection-verify-delivery-against-requirements-host.json": {
        "answer": "第 3 条不满足：没有 run.sh；app.py 只有 /status 与 /version。"},
    "skill-selection-handoff-implicit-host.json": {
        "files": {"HANDOFF.md": "# 交接\n\n## 目标\n\n整理。\n\n## 进展\n\n无。\n\n## 下一步\n\n无。\n"}},
}


class ImplicitSkillCaseTests(unittest.TestCase):
    """Cases that never name a skill, so the read is the model's own decision.

    These cases exist to measure auto-selection. Two ways they could be useless
    are tested here: the prompt secretly names the skill (which would make the
    measurement a copy of ``explicit-name``), and the case grades only the read
    (so a run that loads a workflow and does nothing would pass).
    """

    def load(self, filename):
        return casefile.load(CASES / filename)

    def graded(self, case, *, answer="", files=None, tool_calls=()):
        source = ROOT / case.fixture["path"] if case.fixture.get("path") else None
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        if source is not None:
            shutil.copytree(source, root, dirs_exist_ok=True)
        initial = snapshot(root)
        for name, text in (files or {}).items():
            (root / name).write_text(text, encoding="utf-8")
        facts = Facts(workspace=root, status="completed", end_reason="model_stop",
                      final_text=answer, tool_calls=tuple(tool_calls), approvals=(),
                      initial=initial, final=snapshot(root), pre_decision={}, verdicts={},
                      usage={}, notes=())
        return {result.grader_id: result for result in grade_all(case, facts)}

    def test_every_implicit_case_is_runnable(self):
        for filename in IMPLICIT_CASES:
            with self.subTest(case=filename):
                case = self.load(filename)
                self.assertEqual(case.capability, "skill-selection")
                self.assertEqual(case.authority, {"boundary": "host", "execution": "full",
                                                  "gate": "none"})
                for grader in case.graders:
                    self.assertEqual(grader["kind"], "deterministic", grader["id"])
                    self.assertIn((grader.get("spec") or {}).get("check"), CHECKS, grader["id"])

    def test_no_implicit_prompt_names_the_skill_it_expects(self):
        for filename, skill in IMPLICIT_CASES.items():
            with self.subTest(case=filename):
                prompt = self.load(filename).prompt
                self.assertNotIn(skill, prompt, "a named skill makes this a copy of explicit-name")
                # The Chinese descriptions do not use the English skill name, but the
                # ordering phrase would give the game away just as well.
                self.assertNotIn("技能", prompt)

    def test_every_implicit_case_watches_read_skill_in_both_directions(self):
        for filename, skill in IMPLICIT_CASES.items():
            with self.subTest(case=filename):
                specs = [grader["spec"] for grader in self.load(filename).graders]
                watched = [spec for spec in specs if spec["check"] == "tool_called_with"
                           and spec.get("tool") == "read_skill"]
                exclusive = [spec for spec in specs if spec["check"] == "tool_called_only_with"]
                self.assertEqual([spec.get("equals") for spec in watched], [skill])
                self.assertEqual([spec.get("equals") for spec in exclusive], [skill])

    def test_every_implicit_case_also_grades_the_task(self):
        for filename in IMPLICIT_CASES:
            with self.subTest(case=filename):
                checks = {grader["spec"]["check"] for grader in self.load(filename).graders}
                self.assertTrue(checks & TASK_OUTPUT_CHECKS,
                                "loading a workflow and doing nothing would pass")

    def test_the_task_alone_fails_and_the_right_read_passes(self):
        for filename, skill in IMPLICIT_CASES.items():
            with self.subTest(case=filename):
                case = self.load(filename)
                output = TASK_OUTPUT[filename]
                alone = self.graded(case, **output)
                self.assertEqual(alone["skill_was_read"].result, "fail")
                self.assertEqual(alone["no_substitute_skill"].result, "fail")
                self.assertNotIn("fail", {result.result for key, result in alone.items()
                                          if key not in ("skill_was_read", "no_substitute_skill")})
                both = self.graded(case, tool_calls=(skill_read(skill),), **output)
                self.assertEqual({result.result for result in both.values()}, {"pass"})

    def test_a_sibling_skill_is_named_rather_than_accepted(self):
        for filename, skill in IMPLICIT_CASES.items():
            with self.subTest(case=filename):
                sibling = "change-reviewer" if skill != "change-reviewer" else "project-explorer"
                result = self.graded(self.load(filename), tool_calls=(skill_read(sibling),),
                                     **TASK_OUTPUT[filename])
                self.assertEqual(result["skill_was_read"].result, "fail")
                self.assertEqual(result["no_substitute_skill"].result, "fail")
                self.assertIn(sibling, result["no_substitute_skill"].evidence[0])

    def test_fixtures_stay_inside_the_checkout(self):
        for filename in IMPLICIT_CASES:
            with self.subTest(case=filename):
                path = self.load(filename).fixture.get("path")
                if path is None:
                    continue
                fixture = (ROOT / path).resolve()
                self.assertTrue(fixture.is_relative_to(ROOT))
                self.assertTrue(fixture.is_dir())


if __name__ == "__main__":
    unittest.main()
