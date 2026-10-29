import json
from pathlib import Path
import tempfile
import unittest

from scripts.probe_task import build_fixture, protected_hashes, targets, verify_fixture
from scripts.run_metrics import summarize_tool_calls


class ToolMetricTests(unittest.TestCase):
    def test_exact_repeats_are_counted_after_the_first_occurrence(self):
        calls = [
            {"name": "read_file", "arguments": {"path": "a.py", "limit": 10}, "status": "completed"},
            {"name": "read_file", "arguments": {"limit": 10, "path": "a.py"}, "status": "completed"},
            {"name": "read_file", "arguments": {"path": "a.py", "limit": 20}, "status": "completed"},
            {"name": "run_tests", "arguments": {}, "status": "failed"},
            {"name": "run_tests", "arguments": {}, "status": "failed"},
            {"name": "run_tests", "arguments": {}, "status": "failed"},
        ]
        result = summarize_tool_calls(calls)
        self.assertEqual(result["tool_calls"], 6)
        self.assertEqual(result["repeated_tool_calls"], 3)
        self.assertEqual([call["repeated"] for call in result["tool_sequence"]],
                         [False, True, False, False, True, True])
        self.assertEqual([call["status"] for call in result["tool_sequence"]],
                         ["completed", "completed", "completed", "failed", "failed", "failed"])

    def test_empty_and_non_object_arguments_are_stable_and_serializable(self):
        result = summarize_tool_calls([
            {"name": "glob", "arguments": None},
            {"name": "glob"},
            {"name": "custom", "arguments": {"path": Path("a.py")}},
        ])
        self.assertEqual(result["tool_calls"], 3)
        self.assertEqual(result["repeated_tool_calls"], 1)
        json.dumps(result)


class ProbeFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_code_scenarios_begin_with_failing_tests(self):
        for name in ("fix", "edit", "multifile"):
            with self.subTest(name=name):
                build_fixture(name, self.root)
                protected = protected_hashes(self.root)
                checks = verify_fixture(name, self.root, protected)
                self.assertFalse(checks["tests_pass"])
                self.assertTrue(checks["protected_files_unchanged"])

    def test_fix_verifier_accepts_the_semantic_fix_not_a_particular_patch(self):
        build_fixture("fix", self.root)
        protected = protected_hashes(self.root)
        path = self.root / "app/pricing.py"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "TIERS = ((100, 0.05), (500, 0.10), (1000, 0.15))",
            "TIERS = ((1000, 0.15), (500, 0.10), (100, 0.05))"), encoding="utf-8")
        checks = verify_fixture("fix", self.root, protected)
        self.assertTrue(checks["tests_pass"], checks["test_output_tail"])
        self.assertTrue(checks["protected_files_unchanged"])
        (self.root / "tests/test_invoice.py").write_text("# changed\n", encoding="utf-8")
        self.assertFalse(verify_fixture("fix", self.root, protected)["protected_files_unchanged"])

    def test_edit_verifier_covers_all_three_discontinuous_changes(self):
        build_fixture("edit", self.root)
        protected = protected_hashes(self.root)
        path = self.root / "app/text_utils.py"
        content = path.read_text(encoding="utf-8")
        content = content.replace("“draft”", "“published”").replace(
            "value.lower()", "value.casefold()").replace('suffix = "…"', 'suffix = "!"')
        path.write_text(content, encoding="utf-8")
        checks = verify_fixture("edit", self.root, protected)
        self.assertTrue(checks["tests_pass"], checks["test_output_tail"])

    def test_long_scenario_requires_the_command_to_finish(self):
        build_fixture("long", self.root)
        self.assertFalse(verify_fixture("long", self.root, {})["expected_result_created"])
        (self.root / "long-result.json").write_text('{"marker":"SLOW_JOB_DONE"}', encoding="utf-8")
        self.assertTrue(verify_fixture("long", self.root, {})["expected_result_created"])

    def test_performance_target_is_separate_from_correctness(self):
        record = {"correct": True, "turns": 6, "tool_calls": 8, "elapsed_s": 8.6,
                  "tool_sequence": []}
        self.assertFalse(targets("fix", record)["met"])
        record.update(turns=4, tool_calls=6, elapsed_s=6.5)
        self.assertTrue(targets("fix", record)["met"])


if __name__ == "__main__":
    unittest.main()
