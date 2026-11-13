"""P3 pure tier: hand-authored vectors, and a loader that refuses to guess.

The point of these tests is that the vector file is data, so it can be wrong. An
unknown target, a duplicate id, or a vector that declares both a value and an
error must be rejected before anything runs — otherwise a typo silently becomes
a skipped check.
"""

import json
from pathlib import Path
import tempfile
import unittest

from eval.replay import pure


class BundledVectorTests(unittest.TestCase):
    def test_every_bundled_vector_matches(self):
        results = pure.run(pure.load_vectors())
        self.assertEqual(pure.format_results(results), f"{len(results)}/{len(results)} "
                                                       "pure vector(s) match\n")
        self.assertTrue(all(result.ok for result in results))

    def test_the_file_covers_each_target(self):
        targets = {vector.target for vector in pure.load_vectors()}
        self.assertEqual(targets, set(pure.TARGETS))

    def test_report_counts_passes(self):
        results = pure.run(pure.load_vectors())
        document = pure.report(results)
        self.assertEqual(document["vectors"], len(results))
        self.assertEqual(document["passed"], len(results))


class LoaderTests(unittest.TestCase):
    def write(self, document) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(document, handle)
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def test_rejects_an_unknown_target(self):
        path = self.write({"vectors": [{"id": "a", "target": "nope", "input": {},
                                        "expected": 1}]})
        with self.assertRaises(pure.ReplayInvalid) as caught:
            pure.load_vectors(path)
        self.assertIn("unknown target", str(caught.exception))

    def test_rejects_a_duplicate_id(self):
        path = self.write({"vectors": [
            {"id": "a", "target": "retry.table", "input": {}, "expected": {}},
            {"id": "a", "target": "retry.table", "input": {}, "expected": {}}]})
        with self.assertRaises(pure.ReplayInvalid):
            pure.load_vectors(path)

    def test_rejects_a_vector_that_declares_both_a_value_and_an_error(self):
        path = self.write({"vectors": [{"id": "a", "target": "retry.table", "input": {},
                                        "expected": {}, "expect_error": "ValueError"}]})
        with self.assertRaises(pure.ReplayInvalid):
            pure.load_vectors(path)

    def test_rejects_a_vector_that_declares_neither(self):
        path = self.write({"vectors": [{"id": "a", "target": "retry.table", "input": {}}]})
        with self.assertRaises(pure.ReplayInvalid):
            pure.load_vectors(path)

    def test_rejects_an_empty_file(self):
        path = self.write({"vectors": []})
        with self.assertRaises(pure.ReplayInvalid):
            pure.load_vectors(path)

    def test_reports_a_missing_file(self):
        with self.assertRaises(pure.ReplayInvalid):
            pure.load_vectors(Path("/nonexistent/vectors.json"))


class RunTests(unittest.TestCase):
    def test_nested_booleans_are_not_equal_to_integers(self):
        for want, have in (({'allowed': 1}, {'allowed': True}),
                           ({'values': [0]}, {'values': [False]})):
            vector = pure.Vector('typed', 'outbound.protect_payload', {'payload': have}, want)
            self.assertFalse(pure.run([vector])[0].ok)


    def test_a_wrong_expectation_is_reported_with_the_actual_value(self):
        results = pure.run([pure.Vector("wrong", "retry.table", {}, {"denied": True})])
        self.assertFalse(results[0].ok)
        self.assertIn("denied", results[0].actual)
        self.assertIn("wrong", pure.format_results(results))

    def test_a_expected_error_matches_by_type_name(self):
        results = pure.run([pure.Vector(
            "escape", "outbound.protect_payload",
            {"payload": {"content": "/srv/chagent-eval/wsx/secret"},
             "workspace": pure.VECTOR_ROOT}, expect_error="ProtectedPathError")])
        self.assertTrue(results[0].ok)

    def test_an_error_that_did_not_happen_is_a_failure(self):
        results = pure.run([pure.Vector(
            "no-error", "outbound.to_model",
            {"text": "x", "workspace": pure.VECTOR_ROOT}, expect_error="ValueError")])
        self.assertFalse(results[0].ok)
        self.assertIn("expected an error", results[0].error)

    def test_a_malformed_payload_is_a_failure_not_a_crash(self):
        results = pure.run([pure.Vector("bad", "outbound.to_model", {}, "x")])
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].error, "ReplayInvalid")

    def test_results_json_is_bounded(self):
        long_value = "x" * 500
        results = pure.run([pure.Vector("long", "outbound.to_model",
                                        {"text": long_value, "workspace": pure.VECTOR_ROOT},
                                        long_value + "different")])
        self.assertTrue(results[0].to_json()["expected"].endswith("…"))


if __name__ == "__main__":
    unittest.main()
