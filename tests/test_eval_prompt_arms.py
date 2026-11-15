"""Frozen prompt bytes bind the plan, dispatch, and comparison independently."""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

from eval import prereg, prompts, report
from eval.budgets import BudgetClient
from eval.runner import TrialPlan, run_trial
from chagent.models.base import Model, ModelRequest
from chagent.domain.messages import SystemMessage, UserMessage
from chagent.models.fake import FakeModelClient
from test_eval_prereg import protocol, case
import test_eval_runner as runner_fixtures
import test_eval_report as report_fixtures


def arms():
    return [
        prereg.Arm(
            "baseline",
            "deepseek",
            "deepseek-flash",
            prompt=prompts.variant("", "baseline-v1"),
        ),
        prereg.Arm(
            "candidate",
            "deepseek",
            "deepseek-flash",
            prompt=prompts.variant("Read the relevant workflow first.", "relevant-v1"),
        ),
    ]


class PromptProtocolTests(unittest.TestCase):
    def test_legacy_protocol_round_trip_has_no_added_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p = protocol()
            prereg.freeze(p, Path(d))
            loaded = prereg.load(Path(d))
            self.assertEqual(loaded.to_json(), p.to_json())
            self.assertTrue(all("prompt" not in a.to_json() for a in loaded.arms))

    def test_same_model_prompt_arms_round_trip_and_hash_changes(self):
        with tempfile.TemporaryDirectory() as d:
            p = protocol(arms=arms())
            prereg.freeze(p, Path(d))
            self.assertEqual(prereg.load(Path(d)).to_json(), p.to_json())
            changed = arms()
            changed[1] = replace(
                changed[1], prompt=prompts.variant("Different text", "relevant-v2")
            )
            self.assertNotEqual(p.sha256, protocol(arms=changed).sha256)
            self.assertTrue(prereg.check(p, [case()], changed))

    def test_bad_prompt_hash_is_refused_before_freeze(self):
        a = arms()
        a[1] = replace(a[1], prompt={**a[1].prompt, "sha256": "0" * 64})
        with self.assertRaises(prereg.ProtocolInvalid):
            protocol(arms=a)

    def test_edited_frozen_prompt_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = protocol(arms=arms())
            path = prereg.freeze(p, Path(d))
            data = json.loads(path.read_text())
            data["arms"][1]["prompt"] = prompts.variant("edited", "edited-v1")
            path.write_text(json.dumps(data))
            with self.assertRaises(prereg.ProtocolInvalid):
                prereg.load(Path(d))

    def test_all_archived_protocols_still_load(self):
        root = Path(__file__).resolve().parent.parent / "eval/protocols"
        for path in root.rglob("protocol.json"):
            with self.subTest(path=path):
                prereg.load(path.parent)

    def test_archived_spec_agrees_with_the_protocol_it_froze(self):
        """A spec that can be edited after the fact is a protocol that can be.

        The frozen protocol is the evidence; the spec is the intent.  When the two
        disagree, the hash still verifies and the mismatch is invisible, so the
        number a reader checks (the frozen file) would no longer be the number the
        experiment was pre-registered under.
        """
        root = Path(__file__).resolve().parent.parent / "eval/protocols"
        checked = 0
        for spec_path in sorted(root.glob("*/**/spec.json")):
            protocol_path = spec_path.with_name("protocol.json")
            if not protocol_path.exists():
                continue
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            frozen = json.loads(protocol_path.read_text(encoding="utf-8"))
            with self.subTest(spec=spec_path.parent.name):
                for key in spec:
                    if key in frozen:
                        self.assertEqual(spec[key], frozen[key], f"{spec_path.parent.name}: {key}")
                self.assertEqual(
                    [(a["name"], a["provider"], a["model"],
                      (a.get("prompt") or {}).get("sha256")) for a in spec["arms"]],
                    [(a["name"], a["provider"], a["model"],
                      (a.get("prompt") or {}).get("sha256")) for a in frozen["arms"]],
                    f"{spec_path.parent.name}: arms")
            checked += 1
        self.assertGreaterEqual(checked, 5, "archived specs with a frozen protocol")


class PromptDispatchTests(unittest.IsolatedAsyncioTestCase):
    def request(self, preamble):
        return ModelRequest(
            Model("one", "test", "test", 100, 20),
            [
                SystemMessage("", 0, sections={"preamble": preamble}),
                UserMessage("hi", 0),
            ],
        )

    async def test_mismatch_blocks_before_any_call_or_cost_reservation(self):
        client = FakeModelClient("read_file", {"path": "x"})
        guard = BudgetClient(
            client,
            protocol(arms=arms()),
            runner_fixtures.case(),
            synthetic=True,
            expected_preamble=prompts.preamble(arms()[1]),
        )
        with self.assertRaisesRegex(ValueError, "no call was made"):
            [e async for e in guard.stream(self.request("wrong"))]
        self.assertEqual(guard.model_calls, 0)
        self.assertEqual(guard.account.reserved, 0)
        self.assertFalse(guard.unknown_cost)

    async def test_later_section_override_is_also_verified(self):
        expected = prompts.preamble(arms()[1])
        request = self.request(expected)
        request.messages.append(SystemMessage("", 0, sections={"preamble": "wrong"}))
        with self.assertRaises(ValueError):
            prompts.verify(request, expected)

    async def test_matching_request_is_forwarded_without_mutation(self):
        expected = prompts.preamble(arms()[1])
        request = self.request(expected)
        guard = BudgetClient(
            FakeModelClient("read_file", {"path": "x"}),
            protocol(arms=arms()),
            runner_fixtures.case(),
            synthetic=True,
            expected_preamble=expected,
        )
        [e async for e in guard.stream(request)]
        self.assertEqual(guard.verified_requests, 1)
        self.assertEqual(request.messages[0].sections["preamble"], expected)


@unittest.skipUnless(
    os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL"
)
class PromptRoundTripTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = runner_fixtures.TrialRoundTripTests.asyncSetUp
    drop_schema = runner_fixtures.TrialRoundTripTests.drop_schema

    async def test_runtime_applies_and_records_each_prompt_arm(self):
        c = runner_fixtures.case()
        p = protocol(cases=[c], arms=arms())
        for arm in p.arms:
            trial = await run_trial(
                TrialPlan(p, c, arm, 0),
                database=self.database,
                client=FakeModelClient(
                    "write_file", {"path": "note.txt", "content": "hi"}
                ),
                env={"DEEPSEEK_API_KEY": "test-key", "CHAGENT_PROVIDER": "deepseek"},
            )
            self.assertEqual(trial.verdict, "pass", trial.notes)
            self.assertEqual(
                trial.manifest["prompt_arm"],
                prompts.binding(arm, trial.usage["model_calls"]),
            )
            self.assertEqual(trial.usage["skill_reads"], [])


class PromptReportTests(unittest.TestCase):
    def test_missing_or_wrong_prompt_binding_makes_report_incomparable(self):
        p = report_fixtures.protocol()
        a = replace(p.baseline, prompt=prompts.variant("", "base-v1"))
        p = replace(p, arms=(a, p.candidate))
        trial = report_fixtures.trial("pass", arm="baseline")
        for binding in (None, prompts.binding(a, 99)):
            changed = replace(trial, manifest={**trial.manifest, "prompt_arm": binding})
            self.assertTrue(
                any(
                    "prompt" in e["reason"]
                    for e in report._invalid_trials(p, [changed])
                )
            )

    def test_legacy_read_count_is_unknown_not_zero(self):
        p = report_fixtures.protocol()
        trial = report_fixtures.trial("pass", arm="baseline")
        observations = report._observations(p, [trial])
        self.assertIsNone(observations[0]["trials_with_skill_read"])


if __name__ == "__main__":
    unittest.main()
