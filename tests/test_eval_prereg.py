"""A protocol is a promise, and the code has to refuse to edit it later.

The tests here are about refusal: an edited protocol, a changed case, a slot
that already has its trial, a stability claim below three observations. Each one
is a way an experiment quietly becomes unfalsifiable, so each one has to raise
rather than warn.
"""

import json
from pathlib import Path
import tempfile
import unittest

from eval.casefile import Case
from eval.prereg import (
    Arm, MIN_STABILITY_REPEATS, ProtocolInvalid, assert_slot, build, check, freeze, load,
    slots_used,
)


def case(identifier="demo/case", version=1, sha=None):
    return Case(id=identifier, version=version, title="t", capability="project-work",
                prompt="p", authority={"boundary": "host", "execution": "full", "gate": "none"},
                budget={"max_turns": 4}, graders=({"id": "g", "kind": "deterministic",
                                                  "expects": "e"},),
                cleanup="none", sha256=sha or "a" * 64)


ARMS = ({"name": "baseline", "provider": "deepseek", "model": "deepseek-flash"},
        {"name": "candidate", "provider": "deepseek", "model": "deepseek-v4-flash"})

BUDGET = {"max_turns": 8, "wall_seconds": 240, "max_tool_calls": 20, "total_cost_usd": 1.0}


def protocol(**overrides):
    arguments = dict(experiment="demo-2026-11-13", question="does it help?",
                     hypothesis="it does", cases=[case()], arms=ARMS, repeats=3,
                     min_repeats_for_stability=3, order="interleaved", budget=BUDGET,
                     thresholds={"improve_points": 0.2, "regress_points": 0.2},
                     analysis="wilson", exclusions="infra out of the denominator",
                     stopping_rule="every slot once", created_at="2026-11-13T00:00:00+00:00")
    arguments.update(overrides)
    return build(**arguments)


class BuildingTests(unittest.TestCase):
    def test_hash_is_stable(self):
        self.assertEqual(protocol().sha256, protocol().sha256)

    def test_hash_changes_with_the_promise(self):
        self.assertNotEqual(protocol().sha256, protocol(repeats=4, min_repeats_for_stability=4).sha256)

    def test_slots_multiply(self):
        self.assertEqual(protocol(cases=[case(), case("demo/other")]).slots, 12)

    def test_two_repeats_cannot_claim_stability(self):
        with self.assertRaises(ProtocolInvalid):
            protocol(repeats=2, min_repeats_for_stability=2)

    def test_minimum_is_three(self):
        with self.assertRaises(ProtocolInvalid):
            protocol(repeats=4, min_repeats_for_stability=MIN_STABILITY_REPEATS - 1)

    def test_stability_cannot_exceed_repeats(self):
        with self.assertRaises(ProtocolInvalid):
            protocol(repeats=3, min_repeats_for_stability=4)

    def test_duplicate_arms_are_refused(self):
        with self.assertRaises(ProtocolInvalid):
            protocol(arms=[ARMS[0], ARMS[0]])

    def test_duplicate_cases_are_refused(self):
        with self.assertRaises(ProtocolInvalid):
            protocol(cases=[case(), case()])

    def test_baseline_is_the_first_arm(self):
        built = protocol()
        self.assertEqual(built.baseline.model, "deepseek-flash")
        self.assertEqual(built.candidate.model, "deepseek-v4-flash")


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_round_trip_keeps_the_digest(self):
        built = protocol()
        freeze(built, self.directory)
        loaded = load(self.directory)
        self.assertEqual(loaded.sha256, built.sha256)
        self.assertEqual(loaded.repeats, built.repeats)

    def test_editing_the_file_is_refused(self):
        built = protocol()
        path = freeze(built, self.directory)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["repeats"] = 10
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ProtocolInvalid) as caught:
            load(self.directory)
        self.assertIn("edited after it was frozen", str(caught.exception))

    def test_missing_file_is_reported(self):
        with self.assertRaises(ProtocolInvalid):
            load(self.directory / "absent")

    def test_pricing_survives(self):
        built = protocol(pricing={"input_usd_per_mtok": 0.1, "output_usd_per_mtok": 0.4,
                                  "source": "provider list price 2026-11-13"})
        freeze(built, self.directory)
        self.assertEqual(load(self.directory).pricing["output_usd_per_mtok"], 0.4)


class StoreTests(unittest.TestCase):
    def test_the_protocol_beside_trials_is_not_read_as_a_trial(self):
        from eval.records import TrialInvalid, TrialStore

        with tempfile.TemporaryDirectory() as directory:
            built = protocol()
            freeze(built, Path(directory) / built.experiment)
            store = TrialStore(directory)
            self.assertEqual(store.read(built.experiment,
                                        ignore=("protocol.json",)), [])
            # Without the ignore, the file is not skipped silently: a name this
            # store cannot read is a mistake the caller has to resolve.
            with self.assertRaises((TrialInvalid, KeyError)):
                store.read(built.experiment)


class CheckTests(unittest.TestCase):
    def test_matching_inputs_have_no_problems(self):
        built = protocol()
        self.assertEqual(check(built, [case()], list(built.arms)), [])

    def test_a_changed_case_is_named(self):
        built = protocol()
        problems = check(built, [case(sha="b" * 64)], list(built.arms))
        self.assertTrue(any("changed since it was preregistered" in item for item in problems))

    def test_an_unpreregistered_case_is_named(self):
        built = protocol()
        problems = check(built, [case(), case("demo/extra")], list(built.arms))
        self.assertTrue(any("not preregistered" in item for item in problems))

    def test_an_arm_running_another_model_is_refused(self):
        built = protocol()
        arms = [Arm("baseline", "deepseek", "deepseek-flash"),
                Arm("candidate", "deepseek", "deepseek-v4-flash")]
        self.assertEqual(check(built, [case()], arms), [])
        swapped = [arms[0], Arm("candidate", "deepseek", "something-else")]
        problems = check(built, [case()], swapped)
        self.assertTrue(any("which was not preregistered" in item for item in problems))

    def test_all_problems_are_returned_at_once(self):
        built = protocol()
        problems = check(built, [case("demo/extra", sha="c" * 64)], [])
        self.assertGreaterEqual(len(problems), 3)


class SlotTests(unittest.TestCase):
    def test_slots_are_counted_per_case_and_arm(self):
        built = protocol()
        trial = _trial("a", case=("demo/case", 1), arm="baseline")
        used = slots_used(built, [trial])
        self.assertEqual(used[("demo/case@1", "baseline")], 1)

    def test_assert_slot_numbers_the_repeats(self):
        built = protocol()
        self.assertEqual(assert_slot(built, case(), built.baseline, []), 0)
        trials = [_trial(str(index), case=("demo/case", 1), arm="baseline") for index in range(2)]
        self.assertEqual(assert_slot(built, case(), built.baseline, trials), 2)

    def test_the_fourth_trial_is_refused(self):
        built = protocol()
        trials = [_trial(str(index), case=("demo/case", 1), arm="baseline") for index in range(3)]
        with self.assertRaises(ProtocolInvalid) as caught:
            assert_slot(built, case(), built.baseline, trials)
        self.assertIn("not re-run until it turns green", str(caught.exception))

    def test_other_experiments_do_not_fill_these_slots(self):
        built = protocol()
        trial = _trial("x", case=("demo/case", 1), arm="baseline", experiment="other")
        self.assertEqual(slots_used(built, [trial]), {})


def _trial(identifier, *, case, arm, experiment="demo-2026-11-13"):
    from eval.records import new_trial

    loaded = type("C", (), {"id": case[0], "version": case[1], "sha256": "a" * 64,
                            "graders": ()})()
    return new_trial(loaded, {"arm": arm}, experiment, trial_id=identifier)


if __name__ == "__main__":
    unittest.main()
