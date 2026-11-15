"""The reviewer worksheet must hand back a file, not an opinion.

Three things are worth a test here, and they are the three ways this tool could
quietly damage the calibration:

* it could render a packet whose content has drifted from its hashes, so the
  reviewer spends an hour judging text that no longer matches the rubric;
* it could ship a page whose verdicts are already chosen, which would make a
  human signature on machine judgment;
* it could accept a returned file that ``analyze_reviews.py`` will later reject,
  which costs the reviewer the whole pass.

The packet is a synthetic one built in a temporary directory. The real packet
lives under ``eval/results/``, which is not in version control, so a test that
read it would fail on a fresh clone.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest

from eval import review_sheet


def _packet(root: Path, count: int = 3) -> Path:
    rubric = {
        "version": "test-rubric-v1",
        "purpose": "test only",
        "dimensions": {"fidelity": "no invented facts", "continuation": "next action is clear"},
        "verdicts": {"pass": "all dimensions hold", "fail": "one material false claim",
                     "inconclusive": "evidence cannot decide"},
        "calibration_policy": {"accuracy_threshold": 0.9, "pass_recall_threshold": 0.9,
                               "fail_recall_threshold": 1.0, "minimum_consensus_per_class": 3},
    }
    rubric_hash = review_sheet.digest(rubric)
    samples = []
    for index in range(count):
        sample = {
            "sample_id": f"S-{index:04d}",
            "dataset": "development" if index == 0 else "validation",
            "prompt": "整理成交接说明",
            "facts": [f"fact {index}.a", f"fact {index}.b"],
            "document": f"# 交接 {index}\n\n第二行\n\n## 目标\n- 目标 {index}\n",
            "final_answer": f"已写好交接说明 {index}",
        }
        sample["sample_sha256"] = review_sheet.digest(sample)
        samples.append(sample)
    root.mkdir(parents=True, exist_ok=True)
    (root / "rubric.json").write_text(
        json.dumps({"rubric": rubric, "rubric_sha256": rubric_hash}, ensure_ascii=False),
        encoding="utf-8")
    (root / "samples.json").write_text(
        json.dumps({"rubric_sha256": rubric_hash, "samples": samples}, ensure_ascii=False),
        encoding="utf-8")
    return root


def _rows(packet: Path, reviewer: str = "reviewer-A", verdict: str = "pass") -> list[dict]:
    _, samples = review_sheet.load(packet)
    rubric_hash = json.loads((packet / "rubric.json").read_text())["rubric_sha256"]
    return [{"sample_id": sample["sample_id"],
             "sample_sha256": sample["sample_sha256"],
             "rubric_sha256": rubric_hash,
             "reviewer_id": reviewer, "verdict": verdict,
             "evidence_lines": [1], "note": "依据第 1 行"} for sample in samples]


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.packet = _packet(Path(self.temp.name) / "packet")

    def tearDown(self):
        self.temp.cleanup()

    def test_page_carries_every_sample_once_with_its_hashes(self):
        page = review_sheet.build(self.packet, "A").read_text(encoding="utf-8")
        _, samples = review_sheet.load(self.packet)
        for sample in samples:
            self.assertEqual(page.count(f'data-sample="{sample["sample_id"]}"'), 1)
            self.assertIn(sample["sample_sha256"], page)
        self.assertIn(json.loads((self.packet / "rubric.json").read_text())["rubric_sha256"], page)

    def test_no_verdict_is_preselected(self):
        page = review_sheet.build(self.packet, "B").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"<input[^>]*\schecked", page))
        self.assertNotIn('"verdict": "pass"', page)

    def test_document_lines_are_numbered_from_one(self):
        page = review_sheet.build(self.packet, "A").read_text(encoding="utf-8")
        self.assertIn('<span class="n">1</span>', page)
        self.assertIn('<span class="n">6</span>', page)

    def test_a_trailing_newline_is_not_given_a_line_number(self):
        # samples/<id>.md stops at the last visible row; a page that numbered the
        # empty string after it would disagree with the document by one row.
        self.assertEqual(review_sheet._lines("a\nb\n").count('class="ln"'), 2)
        self.assertEqual(review_sheet._lines("a\nb").count('class="ln"'), 2)
        self.assertEqual(review_sheet._lines("a\n\n").count('class="ln"'), 2)

    def test_page_has_no_model_side_vocabulary(self):
        # The packet is blind: a worksheet that named arms or scores would leak
        # the answer to the person being asked for an independent label.
        page = review_sheet.build(self.packet, "A").read_text(encoding="utf-8")
        for word in ("baseline", "candidate", "arm=", "judge", "grader"):
            self.assertNotIn(word, page.lower())

    def test_a_changed_sample_refuses_to_render(self):
        source = json.loads((self.packet / "samples.json").read_text(encoding="utf-8"))
        source["samples"][0]["document"] += "\n偷偷改一行\n"
        (self.packet / "samples.json").write_text(json.dumps(source, ensure_ascii=False),
                                                  encoding="utf-8")
        with self.assertRaises(review_sheet.PacketInvalid):
            review_sheet.build(self.packet, "A")

    def test_a_changed_rubric_refuses_to_render(self):
        rubric_doc = json.loads((self.packet / "rubric.json").read_text(encoding="utf-8"))
        rubric_doc["rubric"]["verdicts"]["pass"] = "任何东西都算通过"
        (self.packet / "rubric.json").write_text(json.dumps(rubric_doc, ensure_ascii=False),
                                                 encoding="utf-8")
        with self.assertRaises(review_sheet.PacketInvalid):
            review_sheet.build(self.packet, "A")

    def test_only_two_slots_exist(self):
        with self.assertRaises(ValueError):
            review_sheet.build(self.packet, "C")


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.packet = _packet(Path(self.temp.name) / "packet")

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, rows: list[dict], name: str = "reviewer-A.jsonl") -> Path:
        path = Path(self.temp.name) / name
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")
        return path

    def test_a_complete_file_is_accepted(self):
        self.assertEqual(review_sheet.check(self._write(_rows(self.packet)), self.packet), [])

    def test_the_empty_template_is_not_a_review(self):
        blank = [{**row, "verdict": None, "evidence_lines": [], "note": "", "reviewer_id": ""}
                 for row in _rows(self.packet)]
        problems = review_sheet.check(self._write(blank), self.packet)
        self.assertTrue(any("no verdict" in problem for problem in problems))

    def test_missing_reviewer_is_named(self):
        rows = _rows(self.packet)
        rows[1]["reviewer_id"] = "  "
        problems = review_sheet.check(self._write(rows), self.packet)
        self.assertTrue(any("no reviewer_id" in problem for problem in problems))

    def test_missing_evidence_and_note_are_named(self):
        rows = _rows(self.packet)
        rows[0]["evidence_lines"] = []
        rows[2]["note"] = ""
        problems = review_sheet.check(self._write(rows), self.packet)
        self.assertTrue(any("no evidence_lines" in problem for problem in problems))
        self.assertTrue(any("no note" in problem for problem in problems))

    def test_evidence_must_be_positive_line_numbers(self):
        rows = _rows(self.packet)
        rows[0]["evidence_lines"] = [0]
        problems = review_sheet.check(self._write(rows), self.packet)
        self.assertTrue(any("positive line numbers" in problem for problem in problems))

    def test_a_tampered_hash_is_refused(self):
        rows = _rows(self.packet)
        rows[0]["sample_sha256"] = "0" * 64
        problems = review_sheet.check(self._write(rows), self.packet)
        self.assertTrue(any("sample hash" in problem for problem in problems))

    def test_two_reviewers_in_one_file_are_refused(self):
        rows = _rows(self.packet)
        rows[-1]["reviewer_id"] = "someone-else"
        problems = review_sheet.check(self._write(rows), self.packet)
        self.assertTrue(any("exactly one reviewer" in problem for problem in problems))

    def test_an_absent_sample_is_named(self):
        problems = review_sheet.check(self._write(_rows(self.packet)[:2]), self.packet)
        self.assertTrue(any("absent" in problem for problem in problems))

    def test_an_unknown_sample_is_refused(self):
        rows = _rows(self.packet)
        rows[0]["sample_id"] = "S-ffff"
        problems = review_sheet.check(self._write(rows), self.packet)
        self.assertTrue(any("unknown sample" in problem for problem in problems))

    def test_a_broken_line_is_reported_with_its_number(self):
        path = Path(self.temp.name) / "broken.jsonl"
        path.write_text('{"sample_id": "S-0000",\n', encoding="utf-8")
        problems = review_sheet.check(path, self.packet)
        self.assertTrue(any(problem.startswith("line 1: not JSON") for problem in problems))

    def test_check_and_analyze_agree_on_a_usable_file(self):
        # The whole point of check is that it predicts what the scoring script
        # will do with the same bytes. That script lives with the packet, which
        # is not in version control, so load it by path and skip when absent.
        script = (Path(__file__).resolve().parent.parent
                  / "eval/results/p4-semantic-calibration-20261114/public/analyze_reviews.py")
        if not script.exists():
            self.skipTest("analyze_reviews.py is not present in this checkout")
        import importlib.util
        spec = importlib.util.spec_from_file_location("analyze_reviews_under_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows = _rows(self.packet)
        a = self._write(rows, "reviewer-A.jsonl")
        b = self._write([{**row, "reviewer_id": "reviewer-B"} for row in rows], "reviewer-B.jsonl")
        self.assertEqual(review_sheet.check(a, self.packet), [])
        result = module.analyze(self.packet, a, b)
        self.assertEqual(result["human_review_status"], "complete")
        self.assertFalse(result["semantic_grader_enabled"])


if __name__ == "__main__":
    unittest.main()
