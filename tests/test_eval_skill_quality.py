"""Structured contracts reject keyword-only, contradicted and overreported answers."""

import json
from pathlib import Path
import unittest

from eval.casefile import load
from eval.graders.deterministic import check_answer_json_schema, SpecInvalid
from eval.graders import grade_all
from test_eval_graders import facts

ROOT = Path(__file__).resolve().parent.parent
ANSWERS = {
    "explore-ledger": {
        "entry": "service/main.py",
        "default_port": 8765,
        "call_edges": ["main->create_app", "create_app->Ledger"],
        "routes_source": "service/routes.py",
        "health_route": "GET /health",
        "storage": "memory",
        "launches_http_server": False,
    },
    "explore-worker": {
        "entry": "worker/main.py",
        "default_port": 9093,
        "call_edges": ["main->build_worker", "build_worker->Queue"],
        "storage": "disk",
        "storage_source": "worker/queue.py",
        "launches_http_server": False,
    },
    "synthesize-cache": {
        "obsolete_seconds": 60,
        "policy_seconds": 300,
        "canary_seconds": 120,
        "policy_source": "notes/cache-policy-v2.md",
        "canary_source": "notes/rollout.md",
        "alignment": "pending",
        "conflict": True,
    },
    "synthesize-retries": {
        "obsolete_retries": 2,
        "policy_retries": 5,
        "canary_retries": 1,
        "policy_source": "notes/policy-v2.md",
        "canary_source": "notes/rollout.md",
        "alignment": "pending",
        "conflict": True,
    },
    "review-pages": {
        "findings": [
            {
                "function": "total_pages",
                "source": "page.py",
                "trigger": "non_divisible",
                "counterexample": {"count": 10, "size": 3, "actual": 3, "expected": 4},
            }
        ]
    },
    "review-extra-page": {
        "findings": [
            {
                "function": "total_pages",
                "source": "page.py",
                "trigger": "exact_multiple",
                "counterexample": {"count": 9, "size": 3, "actual": 4, "expected": 3},
            }
        ]
    },
}


def case(name):
    return load(ROOT / "eval/cases" / f"skill-quality-{name}.json")


def results(name, answer):
    return grade_all(case(name), facts(final_text=answer))


class QualityContractTests(unittest.TestCase):
    def test_equivalent_constructor_notation_is_accepted(self):
        for name in ("explore-ledger", "explore-worker"):
            answer = dict(ANSWERS[name])
            answer["call_edges"] = list(answer["call_edges"])
            answer["call_edges"][1] += ".__init__"
            self.assertEqual(results(name, json.dumps(answer))[0].result, "pass")

    def test_call_parentheses_qualifiers_and_spacing_are_not_factual_errors(self):
        for edge in (
            "create_app->Ledger()",
            "service.main.create_app -> service.store.Ledger",
            "create_app() -> Ledger.__init__()",
        ):
            answer = dict(
                ANSWERS["explore-ledger"], call_edges=["main()->create_app()", edge]
            )
            self.assertEqual(
                results("explore-ledger", json.dumps(answer))[0].result, "pass"
            )
        answer = dict(
            ANSWERS["explore-ledger"], call_edges=["main->create_app", "main->Ledger"]
        )
        self.assertEqual(
            results("explore-ledger", json.dumps(answer))[0].result, "fail"
        )
        answer["call_edges"] = ["main->create_app", "create_app->invented.Ledger"]
        self.assertEqual(
            results("explore-ledger", json.dumps(answer))[0].result, "fail"
        )

    def test_affected_wrapper_alongside_root_cause_is_not_a_false_positive(self):
        root = ANSWERS["review-pages"]["findings"][0]
        answer = {"findings": [dict(root, function="page_count_label"), root]}
        self.assertEqual(results("review-pages", json.dumps(answer))[0].result, "pass")

    def test_all_six_correct_answers_pass_without_any_skill_read(self):
        for name, answer in ANSWERS.items():
            with self.subTest(name=name):
                self.assertTrue(
                    all(g.result == "pass" for g in results(name, json.dumps(answer)))
                )

    def test_keywords_and_self_claims_do_not_pass(self):
        for name in ANSWERS:
            with self.subTest(name=name):
                self.assertEqual(
                    results(
                        name,
                        "main.py 8765 /health 300 120 total_pages; everything is correct",
                    )[0].result,
                    "fail",
                )

    def test_wrong_source_links_and_resolved_conflict_are_rejected(self):
        for field, value in [
            ("policy_source", "notes/rollout.md"),
            ("alignment", "confirmed"),
            ("policy_seconds", 120),
            ("conflict", False),
        ]:
            answer = dict(ANSWERS["synthesize-cache"], **{field: value})
            self.assertEqual(
                results("synthesize-cache", json.dumps(answer))[0].result, "fail"
            )

    def test_invented_server_and_missing_call_edge_fail(self):
        for change in (
            {"launches_http_server": True},
            {"call_edges": ["main->create_app"]},
            {"storage": "disk"},
        ):
            self.assertEqual(
                results(
                    "explore-ledger",
                    json.dumps(dict(ANSWERS["explore-ledger"], **change)),
                )[0].result,
                "fail",
            )

    def test_review_requires_trigger_arithmetic_and_no_extra_findings(self):
        answer = ANSWERS["review-pages"]
        variants = [
            {"findings": []},
            {"findings": [dict(answer["findings"][0], trigger="exact_multiple")]},
            {
                "findings": [
                    dict(
                        answer["findings"][0],
                        counterexample={
                            "count": 10,
                            "size": 3,
                            "actual": 4,
                            "expected": 4,
                        },
                    )
                ]
            },
            {
                "findings": answer["findings"]
                + [
                    {
                        "function": "page",
                        "source": "page.py",
                        "trigger": "non_divisible",
                        "counterexample": {
                            "count": 10,
                            "size": 3,
                            "actual": 3,
                            "expected": 4,
                        },
                    }
                ]
            },
        ]
        for value in variants:
            self.assertEqual(
                results("review-pages", json.dumps(value))[0].result, "fail"
            )

    def test_order_and_json_fences_are_allowed(self):
        answer = dict(ANSWERS["explore-ledger"])
        answer["call_edges"] = list(reversed(answer["call_edges"]))
        self.assertEqual(
            results("explore-ledger", "```json\n" + json.dumps(answer) + "\n```")[
                0
            ].result,
            "pass",
        )

    def test_duplicate_keys_and_nonfinite_json_fail(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}'):
            self.assertEqual(
                check_answer_json_schema(
                    facts(final_text=text), {"schema": {"type": "object"}}
                ).result,
                "fail",
            )

    def test_schema_errors_and_remote_references_are_case_defects(self):
        for schema in (
            {"type": "bogus"},
            {"$ref": "https://example.com/schema"},
            {"properties": {"x": {"$ref": "#/x"}}},
        ):
            with self.assertRaises(SpecInvalid):
                check_answer_json_schema(facts(final_text="{}"), {"schema": schema})

    def test_cases_do_not_require_skill_reads_or_change_existing_cases(self):
        for name in ANSWERS:
            c = case(name)
            self.assertNotIn("read_skill", c.prompt)
            self.assertEqual(
                [g["spec"]["check"] for g in c.graders],
                ["answer_json_schema", "workspace_unchanged"],
            )


if __name__ == "__main__":
    unittest.main()
