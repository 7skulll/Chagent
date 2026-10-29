"""Cache auditing and kept-schema artifacts stay local and diagnosable."""

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from chagent.diagnostics.cache import audit
from chagent.diagnostics.schema import keep_artifacts
from chagent.domain.messages import AssistantMessage, SystemMessage, TextContent, UserMessage
from chagent.models.base import Model, ModelRequest


def request(*messages, api="openai-chat"):
    return asdict(ModelRequest(model=Model("test-model", "test-provider", api, 8192, 1024),
                               messages=list(messages), max_tokens=256))


def row(iteration, payload, *, input_tokens=0, cache_read=0):
    return {"iteration": iteration, "request": payload,
            "response": {"usage": {"input": input_tokens, "output": 10, "cache_read": cache_read,
                                   "cache_write": 0, "total_tokens": input_tokens + cache_read + 10}}}


def assistant(answer):
    return AssistantMessage([TextContent(answer)], "test-provider", "test-model", "test-model", 0, "stop")


class CacheAuditTests(unittest.TestCase):
    def test_hit_matches_provider_usage(self):
        rows = [row(0, request(SystemMessage("rules", 0), UserMessage("hi", 0)), input_tokens=100),
                row(1, request(SystemMessage("rules", 0), UserMessage("hi", 0), assistant("ok")),
                    input_tokens=40, cache_read=160)]
        report = audit(rows)
        # Provider usage defines the denominator: input excludes cached tokens.
        self.assertEqual(report["prompt_tokens"], 300)
        self.assertEqual(report["cached_tokens"], 160)
        self.assertEqual(report["hit"], round(160 / 300, 4))
        self.assertEqual(report["breaks"], [])

    def test_changing_one_byte_of_the_system_section_names_it(self):
        rows = [row(0, request(SystemMessage("rules A", 0), UserMessage("hi", 0))),
                row(1, request(SystemMessage("rules B", 0), UserMessage("hi", 0)))]
        report = audit(rows)
        self.assertEqual(report["breaks"], [{
            "iteration": 1, "section": "system",
            "lost_bytes": report["turns"][1]["total_bytes"],
        }])
        self.assertEqual(report["turns"][1]["reusable"], 0.0)

    def test_appending_a_turn_keeps_the_prefix(self):
        history = [SystemMessage("rules", 0), UserMessage("hi", 0), assistant("ok")]
        rows = [row(0, request(*history)),
                row(1, request(*history, UserMessage("next", 0)))]
        report = audit(rows)
        self.assertEqual(report["breaks"], [])
        self.assertGreater(report["reusable"], 0.5)

    def test_a_shorter_request_after_compaction_is_not_a_break(self):
        rows = [row(0, request(SystemMessage("rules", 0), UserMessage("old", 0), assistant("gone"))),
                row(1, request(SystemMessage("rules", 0)))]
        report = audit(rows)
        self.assertEqual(report["breaks"], [])
        self.assertEqual(report["reusable"], 1.0)

    def test_anthropic_system_prompt_is_labelled_like_the_chat_adapter(self):
        rows = [row(0, request(SystemMessage("rules A", 0), UserMessage("hi", 0), api="anthropic-messages")),
                row(1, request(SystemMessage("rules B", 0), UserMessage("hi", 0), api="anthropic-messages"))]
        report = audit(rows)
        self.assertEqual([broken["section"] for broken in report["breaks"]], ["system"])

    def test_no_calls_recorded_yields_empty_facts(self):
        report = audit([])
        self.assertIsNone(report["hit"])
        self.assertIsNone(report["reusable"])
        self.assertEqual(report["breaks"], [])


class KeepSchemaTests(unittest.TestCase):
    def test_writes_schema_record_and_restore_script(self):
        with tempfile.TemporaryDirectory() as directory:
            data, script = keep_artifacts(
                Path(directory), schema="chagent_live_abc123",
                report_path=Path("docs/diagnostics/live-chain.json"),
                failures=[{"case": "smoke", "failed_checks": ["ordered_events"], "error_type": None}])
            self.assertEqual(data.name, "keep-chagent_live_abc123.json")
            self.assertEqual(script.name, "restore-chagent_live_abc123.sh")
            record = json.loads(data.read_text(encoding="utf-8"))
            self.assertEqual(record["schema"], "chagent_live_abc123")
            self.assertIn("--schema chagent_live_abc123", record["audit"])
            self.assertEqual(record["failures"][0]["failed_checks"], ["ordered_events"])
            text = script.read_text(encoding="utf-8")
            self.assertIn('DROP SCHEMA IF EXISTS "chagent_live_abc123" CASCADE', text)
            # The script takes the connection from the environment, so no secret lands on disk.
            self.assertIn("CHAGENT_DATABASE_URL", text)
            self.assertNotIn("password", text.lower())


if __name__ == "__main__":
    unittest.main()
