"""What the numbers support, and what they do not.

The tests fix the arithmetic against hand-computed values, then fix the rules
that matter more than the arithmetic: a missing slot outranks any comparison, a
regression is named before an improvement, and a difference inside the interval
is not a difference. Each of those is a way a report could be made to say
something the data does not.
"""

import itertools
import unittest

from eval.prereg import Arm, build
from eval.records import GraderResult, TrialRecord
from eval.report import build as build_report
from eval.report import format_report, newcombe, wilson


def protocol(repeats=3, **overrides):
    from eval.casefile import Case

    case = Case(id="demo/case", version=1, title="t", capability="project-work", prompt="p",
                authority={"boundary": "host", "execution": "full", "gate": "none"},
                budget={"max_turns": 8}, graders=({"id": "g", "kind": "deterministic",
                                                   "expects": "e"},),
                cleanup="none", sha256="a" * 64)
    arguments = dict(experiment="demo-2026-11-13", question="q", hypothesis="h", cases=[case],
                     arms=[Arm("baseline", "deepseek", "one"), Arm("candidate", "deepseek", "two")],
                     repeats=repeats, min_repeats_for_stability=3, order="interleaved",
                     budget={"max_turns": 8, "wall_seconds": 240, "max_tool_calls": 20,
                             "total_cost_usd": 1.0},
                     thresholds={"improve_points": 0.2, "regress_points": 0.2},
                     analysis="a", exclusions="e", stopping_rule="s",
                     created_at="2026-11-13T00:00:00+00:00")
    arguments.update(overrides)
    return build(**arguments)


_COUNTER = itertools.count()


def trial(verdict, *, arm, case=("demo/case", 1), experiment="demo-2026-11-13",
          turns=3, tools=4, wall=10.0, cost=0.0):
    results = {
        "pass": (GraderResult("g", "pass"),),
        "fail": (GraderResult("g", "fail"),),
        "inconclusive": (GraderResult("g", "inconclusive"),),
        "infra_error": (GraderResult("g", "pass"),),
    }[verdict]
    from eval.integrity import execution

    p = protocol()
    identity = execution({'repo': {'commit': 'a' * 40, 'dirty': False, 'patch_sha256': None},
                          'dependencies': {'lock_sha256': 'b' * 64, 'python': '3.13'},
                          'runtime': {'platform': 'test'}, 'skills': {'s': 'c' * 64},
                          'prompt_source_sha256': 'd' * 64, 'tool_source_sha256': 'e' * 64,
                          'config': {}})
    identity['end_identity_sha256'] = identity['identity_sha256']
    number = next(_COUNTER)
    return TrialRecord(
        trial_id=f"{arm}-{number}", experiment=experiment,
        case_id=case[0], case_version=case[1], case_sha256="a" * 64,
        status="infra_error" if verdict == "infra_error" else "completed",
        manifest={"arm": arm, "protocol_sha256": p.sha256, "repeat": number % 3,
                  "case": {"id": case[0], "version": case[1], "sha256": "a" * 64},
                  "model": {"provider": "deepseek", "requested_id": "one" if arm == "baseline" else "two"},
                  "execution": identity, "harness": {"cleanup_verified": True, "client_kind": "live"}}, graders_declared=1, grader_ids=("g",), graders=results,
        usage={"model_calls": turns, "tool_calls": tools, "wall_seconds": wall,
               "cost": {"usd": cost, "source": "estimated"} if cost is not None else {"usd": None}})


class IntervalTests(unittest.TestCase):
    def test_wilson_bounds_are_hand_checked(self):
        self.assertEqual(wilson(0, 3), (0.0, 0.5615))
        self.assertEqual(wilson(3, 3), (0.4385, 1.0))
        self.assertIsNone(wilson(0, 0))

    def test_identical_arms_put_zero_inside(self):
        interval = newcombe(wilson(2, 3), wilson(2, 3), 0.6667, 0.6667)
        self.assertLess(interval[0], 0)
        self.assertGreater(interval[1], 0)

    def test_separated_arms_exclude_zero(self):
        interval = newcombe(wilson(3, 3), wilson(0, 3), 1.0, 0.0)
        self.assertLess(interval[1], 0)


class ClaimTests(unittest.TestCase):
    def test_all_pass_both_arms_is_no_difference(self):
        trials = [trial("pass", arm="baseline") for _ in range(3)]
        trials += [trial("pass", arm="candidate") for _ in range(3)]
        document = build_report(protocol(), trials)
        self.assertEqual(document["claim"], "no_difference_observed")
        self.assertEqual(document["cases"][0]["delta"]["direction"], "no_difference_observed")

    def test_candidate_worse_is_a_regression(self):
        trials = [trial("pass", arm="baseline") for _ in range(3)]
        trials += [trial("fail", arm="candidate") for _ in range(3)]
        document = build_report(protocol(), trials)
        self.assertEqual(document["cases"][0]["delta"]["direction"], "regressed")
        self.assertEqual(document["claim"], "regression_detected")

    def test_candidate_better_is_named(self):
        trials = [trial("fail", arm="baseline") for _ in range(3)]
        trials += [trial("pass", arm="candidate") for _ in range(3)]
        document = build_report(protocol(), trials)
        self.assertEqual(document["cases"][0]["delta"]["direction"], "improved")
        self.assertEqual(document["claim"], "improvement_supported")

    def test_a_missing_slot_outranks_every_comparison(self):
        trials = [trial("pass", arm="baseline") for _ in range(3)]
        trials += [trial("fail", arm="candidate") for _ in range(2)]
        document = build_report(protocol(), trials)
        self.assertEqual(document["claim"], "incomplete")
        self.assertEqual(document["missing"], [{"case": "demo/case@1", "arm": "candidate",
                                                "missing": 1}])

    def test_no_denominator_is_inconclusive_not_a_pass(self):
        trials = [trial("inconclusive", arm="baseline") for _ in range(3)]
        trials += [trial("inconclusive", arm="candidate") for _ in range(3)]
        document = build_report(protocol(), trials)
        self.assertEqual(document["claim"], "inconclusive")
        self.assertIsNone(document["cases"][0]["arms"]["baseline"]["rate"])
        self.assertEqual(len(document["excluded"]), 6)

    def test_infrastructure_errors_leave_the_denominator_but_stay_reported(self):
        trials = [trial("pass", arm="baseline"), trial("pass", arm="baseline"),
                  trial("infra_error", arm="baseline")]
        trials += [trial("pass", arm="candidate") for _ in range(3)]
        document = build_report(protocol(), trials)
        cell = document["cases"][0]["arms"]["baseline"]
        self.assertEqual(cell["denominator"], 2)
        self.assertEqual(cell["infra_error"], 1)
        self.assertEqual(len(document["excluded"]), 1)

    def test_same_arm_disagreeing_is_reported_as_unstable(self):
        trials = [trial("pass", arm="baseline"), trial("fail", arm="baseline"),
                  trial("pass", arm="baseline")]
        trials += [trial("pass", arm="candidate") for _ in range(3)]
        document = build_report(protocol(), trials)
        self.assertEqual(document["unstable"][0]["arm"], "baseline")

    def test_an_empty_schedule_is_incomplete_not_green(self):
        document = build_report(protocol(), [])
        self.assertEqual(document["claim"], "incomplete")
        self.assertEqual(document["schedule"]["observed"], 0)

    def test_stray_trials_are_named_not_counted(self):
        trials = [trial("pass", arm="baseline", case=("other/case", 1))]
        document = build_report(protocol(), trials)
        self.assertEqual(document["stray"][0]["case"], "other/case@1")
        self.assertEqual(document["schedule"]["observed"], 0)


class BudgetTests(unittest.TestCase):
    def test_observed_maxima_come_from_the_trials(self):
        trials = [trial("pass", arm="baseline", turns=2, tools=1, wall=5.0),
                  trial("pass", arm="baseline", turns=6, tools=9, wall=40.0)]
        document = build_report(protocol(), trials)
        self.assertEqual(document["budget"]["observed"]["max_turns"], 6)
        self.assertEqual(document["budget"]["observed"]["max_tool_calls"], 9)
        self.assertEqual(document["budget"]["observed"]["max_wall_seconds"], 40.0)

    def test_unknown_cost_stays_unknown(self):
        trials = [trial("pass", arm="baseline", cost=None)]
        document = build_report(protocol(), trials)
        self.assertIsNone(document["budget"]["cost"]["estimated_usd"])

    def test_estimates_are_summed(self):
        trials = [trial("pass", arm="baseline", cost=0.25), trial("pass", arm="baseline", cost=0.5)]
        document = build_report(protocol(), trials)
        self.assertAlmostEqual(document["budget"]["cost"]["estimated_usd"], 0.75)

    def test_no_pricing_is_stated_as_a_limit(self):
        document = build_report(protocol(), [trial("pass", arm="baseline")])
        self.assertTrue(any("cost is unknown" in item for item in document["limitations"]))


class FormatTests(unittest.TestCase):
    def test_the_table_names_the_claim_and_the_interval(self):
        trials = [trial("pass", arm="baseline") for _ in range(3)]
        trials += [trial("fail", arm="candidate") for _ in range(3)]
        text = format_report(build_report(protocol(), trials))
        self.assertIn("claim: regression_detected", text)
        self.assertIn("baseline", text)
        self.assertIn("delta [95%]", text)

    def test_limits_are_printed(self):
        text = format_report(build_report(protocol(), [trial("pass", arm="baseline")]))
        self.assertIn("limit:", text)

    def test_a_column_widens_instead_of_running_into_the_next(self):
        # "no_difference_observed" is wider than any comfortable fixed column.  With a
        # constant width it printed as "...1.00]+0.00 [-0.56,+0.56]", which reads as one
        # merged cell.  Each row must keep four separable cells whatever the content.
        trials = [trial("pass", arm="baseline") for _ in range(3)]
        trials += [trial("pass", arm="candidate") for _ in range(3)]
        lines = format_report(build_report(protocol(), trials)).splitlines()
        start = next(index for index, line in enumerate(lines) if "delta [95%]" in line)
        for line in [lines[start], lines[start + 1]]:
            cells = [cell for cell in line.split("  ") if cell]
            self.assertEqual(4, len(cells), line)
        self.assertTrue(lines[start + 1].endswith("no_difference_observed"))


if __name__ == "__main__":
    unittest.main()
