"""P3 replay eligibility: the refusals, in the order that makes them meaningful.

The matrix here is the whole contract of the plan step. A denied approval must be
refused before its tool name is considered, a host command must be refused even
when a fixture exists, and a file call without a fixture must be refused instead
of replayed into an empty directory. The database test then proves the loader
reads real rows — the approval join and the unknown status come from SQL, not from
a dict built by the test.
"""

import os
import unittest
from uuid import uuid4

from eval.replay import plan as planning
from eval.replay.sandbox import Fixture


def call(name, arguments=None, *, status="completed", record=None, unknown=None):
    return {"id": record or str(uuid4()), "name": name, "status": status,
            "arguments": arguments or {}, "outcome_unknown": unknown}


class PlanCallTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture({"data.txt": "x", "src/app.py": "y"})

    def test_file_call_with_fixture_is_replayable(self):
        result = planning.plan_call(call("read_file", {"path": "data.txt"}), self.fixture)
        self.assertTrue(result.eligible)
        self.assertEqual(result.tier, "stateful")

    def test_no_fixture_refuses_every_file_call(self):
        result = planning.plan_call(call("read_file", {"path": "data.txt"}), None)
        self.assertFalse(result.eligible)
        self.assertEqual([item.code for item in result.refusals], ["missing_fixture"])

    def test_missing_initial_state_is_named(self):
        result = planning.plan_call(call("edit_file", {"path": "absent.txt", "edits": []}),
                                    self.fixture)
        self.assertFalse(result.eligible)
        self.assertEqual(result.refusals[0].code, "missing_initial_state")
        self.assertIn("absent.txt", result.refusals[0].detail)

    def test_host_commands_are_external(self):
        for name in ("exec_command", "run_tests", "host_exec", "process"):
            with self.subTest(name=name):
                result = planning.plan_call(call(name, {"cmd": "ls"}), self.fixture)
                self.assertEqual(result.tier, "external")
                self.assertEqual(result.refusals[0].code, "external_side_effect")

    def test_network_and_service_tools_are_external(self):
        for name in ("web_fetch", "web_search", "site", "collect_file"):
            with self.subTest(name=name):
                result = planning.plan_call(call(name, {"url": "https://example.com"}), self.fixture)
                self.assertEqual(result.refusals[0].code, "external_side_effect")

    def test_database_and_registry_tools_are_refused(self):
        expectations = {"read_artifact": "requires_database",
                        "create_file": "requires_database",
                        "tool_search": "requires_run_context"}
        for name, code in expectations.items():
            with self.subTest(name=name):
                result = planning.plan_call(call(name, {}), self.fixture)
                self.assertEqual(result.refusals[0].code, code)

    def test_an_undeclared_tool_has_no_tier(self):
        result = planning.plan_call(call("invented_tool", {}), self.fixture)
        self.assertEqual(result.tier, "unknown")
        self.assertEqual(result.refusals[0].code, "undeclared_tier")

    def test_echo_is_pure_and_needs_no_fixture(self):
        result = planning.plan_call(call("echo", {"text": "hi"}), None)
        self.assertEqual(result.tier, "pure")
        self.assertTrue(result.eligible)

    def test_unknown_status_is_refused(self):
        result = planning.plan_call(call("read_file", {"path": "data.txt"}, status="unknown"),
                                    self.fixture)
        self.assertEqual(result.refusals[0].code, "outcome_unknown")

    def test_unknown_outcome_flag_is_refused(self):
        result = planning.plan_call(
            call("read_file", {"path": "data.txt"}, unknown="true"), self.fixture)
        self.assertEqual(result.refusals[0].code, "outcome_unknown")

    def test_a_denied_approval_is_refused_and_not_treated_as_permission(self):
        record = str(uuid4())
        result = planning.plan_call(call("write_file", {"path": "data.txt", "content": "x"},
                                         record=record),
                                    self.fixture, decisions={record: "denied"})
        self.assertFalse(result.eligible)
        self.assertEqual(result.refusals[0].code, "approval_decision")
        self.assertIn("never executed", result.refusals[0].detail)

    def test_an_approved_decision_is_not_itself_a_refusal(self):
        record = str(uuid4())
        result = planning.plan_call(call("write_file", {"path": "data.txt", "content": "x"},
                                         record=record),
                                    self.fixture, decisions={record: "approved"})
        self.assertTrue(result.eligible)

    def test_a_denied_host_command_reports_both_reasons(self):
        record = str(uuid4())
        result = planning.plan_call(call("exec_command", {"cmd": "ls"}, record=record),
                                    self.fixture, decisions={record: "expired"})
        codes = [item.code for item in result.refusals]
        self.assertEqual(codes, ["approval_decision", "external_side_effect"])

    def test_unsafe_arguments_are_refused(self):
        for path in ("../x", "/etc/passwd", "a/../../b", "C:/x"):
            with self.subTest(path=path):
                result = planning.plan_call(call("write_file", {"path": path, "content": "x"}),
                                            self.fixture)
                self.assertFalse(result.eligible)
                self.assertIn("unsafe_path", [item.code for item in result.refusals])

    def test_virtual_root_paths_are_accepted(self):
        result = planning.plan_call(call("read_file", {"path": "/workspace/data.txt"}),
                                    self.fixture)
        self.assertTrue(result.eligible, result.refusals)

    def test_a_plain_write_needs_no_starting_file(self):
        result = planning.plan_call(call("write_file", {"path": "new.txt", "content": "x"}),
                                    self.fixture)
        self.assertTrue(result.eligible)

    def test_a_versioned_write_needs_the_previous_content(self):
        result = planning.plan_call(call("write_file", {"path": "new.txt", "content": "x",
                                                        "expected_sha256": "a" * 64}),
                                    self.fixture)
        self.assertEqual(result.refusals[0].code, "missing_initial_state")


class PlanDocumentTests(unittest.TestCase):
    def test_counts_and_warnings(self):
        calls = [call("echo", {"text": "hi"}),
                 call("exec_command", {"cmd": "ls"}),
                 call("read_file", {"path": "data.txt"})]
        document = planning.plan(calls, Fixture({"data.txt": "x"}), run_id="run-1")
        self.assertEqual(document["counts"]["calls"], 3)
        self.assertEqual(document["counts"]["eligible"], 2)
        self.assertEqual(document["counts"]["refused"], 1)
        self.assertEqual(document["counts"]["by_tier"],
                         {"external": 1, "pure": 1, "stateful": 1})
        self.assertTrue(any("more than one refusal" in note for note in document["warnings"]))

    def test_no_fixture_warning_is_explicit(self):
        document = planning.plan([call("read_file", {"path": "a.txt"})], None)
        self.assertTrue(any("missing initial state" in note for note in document["warnings"]))
        self.assertIsNone(document["fixture"])

    def test_format_prints_the_reason(self):
        text = planning.format_plan(planning.plan([call("exec_command", {"cmd": "ls"})], None))
        self.assertIn("refused", text)
        self.assertIn("external_side_effect", text)

    def test_a_refused_call_cannot_be_planned_into_execution(self):
        # The document is the only output: planning has no execution hook at all.
        document = planning.plan([call("write_file", {"path": "a.txt", "content": "x"})], None)
        self.assertNotIn("executed", document)
        self.assertFalse(document["calls"][0]["eligible"])


class CaseFixtureTests(unittest.TestCase):
    def test_a_directory_fixture_is_read_from_the_checkout(self):
        case = {"fixture": {"kind": "directory", "path": "eval/fixtures/approval-gated-write"}}
        fixture, note = planning.fixture_for_case(case)
        self.assertIsNotNone(fixture)
        self.assertIn("data.txt", fixture.files)
        self.assertIn("rebuilt", note)

    def test_an_empty_fixture_is_allowed(self):
        fixture, _ = planning.fixture_for_case({"fixture": {"kind": "empty"}})
        self.assertEqual(fixture.files, {})

    def test_a_tarball_is_refused_rather_than_unpacked(self):
        fixture, note = planning.fixture_for_case(
            {"fixture": {"kind": "tarball", "path": "x.tar", "sha256": "a" * 64}})
        self.assertIsNone(fixture)
        self.assertIn("not unpacked", note)

    def test_a_path_outside_the_checkout_is_refused(self):
        fixture, note = planning.fixture_for_case(
            {"fixture": {"kind": "directory", "path": "../outside"}})
        self.assertIsNone(fixture)
        self.assertIn("escapes", note)

    def test_a_missing_directory_is_reported(self):
        fixture, note = planning.fixture_for_case(
            {"fixture": {"kind": "directory", "path": "eval/does-not-exist"}})
        self.assertIsNone(fixture)
        self.assertIn("cannot be rebuilt", note)

    def test_a_case_without_a_fixture_says_so(self):
        fixture, note = planning.fixture_for_case({})
        self.assertIsNone(fixture)
        self.assertIn("no fixture", note)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class PlanningDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """The approval join and the unknown status, against real SQL."""

    async def asyncSetUp(self):
        from psycopg import AsyncConnection, sql
        from psycopg.conninfo import make_conninfo
        from psycopg.types.json import Jsonb

        from chagent.config import Settings
        from chagent.storage.database import Database
        from chagent.storage.migrate import migrate

        self.schema = "chagent_replay_" + uuid4().hex
        self.admin = await AsyncConnection.connect(
            os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self._drop)
        settings = Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}"))
        self.database = Database(settings)
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)

        self.run_id, session_id, workspace_id = uuid4(), uuid4(), uuid4()
        model_id = uuid4()
        denied_id, unknown_id, read_id = uuid4(), uuid4(), uuid4()
        self.denied_id, self.unknown_id, self.read_id = denied_id, unknown_id, read_id
        async with self.database.transaction() as connection:
            await connection.execute("INSERT INTO workspaces (id, root_path) VALUES (%s, %s)",
                                     (workspace_id, "/tmp/chagent-replay-plan"))
            await connection.execute(
                "INSERT INTO sessions (id, workspace_id, title) VALUES (%s, %s, 'replay')",
                (session_id, workspace_id))
            await connection.execute(
                "INSERT INTO runs (id, session_id, status, end_reason, started_at, ended_at) "
                "VALUES (%s, %s, 'completed', 'model_stop', now() - interval '9 seconds', now())",
                (self.run_id, session_id))
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, status, request, context_snapshot) "
                "VALUES (%s, %s, 0, 'completed', %s, %s)",
                (model_id, self.run_id, Jsonb({}), Jsonb({})))
            await connection.execute(
                "INSERT INTO tool_calls (id, run_id, model_call_id, call_id, name, arguments, "
                "status, call_index, attempt, created_at) VALUES "
                "(%s, %s, %s, 'c1', 'write_file', %s, 'failed', 0, 1, now() - interval '8 seconds'), "
                "(%s, %s, %s, 'c2', 'exec_command', %s, 'unknown', 1, 1, now() - interval '7 seconds'), "
                "(%s, %s, %s, 'c3', 'read_file', %s, 'completed', 2, 1, now() - interval '6 seconds')",
                (denied_id, self.run_id, model_id, Jsonb({"path": "data.txt", "content": "x"}),
                 unknown_id, self.run_id, model_id, Jsonb({"cmd": "ls"}),
                 read_id, self.run_id, model_id, Jsonb({"path": "data.txt"})))
            await connection.execute(
                "INSERT INTO approval_requests (id, run_id, tool_record_id, fingerprint, tool_name, "
                "cwd, subject, status, owner_token, expires_at, decided_at, created_at) VALUES "
                "(%s, %s, %s, 'f', 'write_file', '/tmp', 'write data.txt', 'denied', 'o', "
                "now() + interval '1 minute', now() - interval '7 seconds', "
                "now() - interval '8 seconds')", (uuid4(), self.run_id, denied_id))

    async def _drop(self):
        from psycopg import sql

        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
            sql.Identifier(self.schema)))

    async def test_plan_reads_the_denial_and_the_unknown_status(self):
        document = await planning.ReplayPlanning(self.database).plan(self.run_id)
        by_call = {item["call_id"]: item for item in document["calls"]}
        denied = by_call[str(self.denied_id)]
        unknown = by_call[str(self.unknown_id)]
        self.assertEqual(denied["refusals"][0]["code"], "approval_decision")
        self.assertEqual(unknown["refusals"][0]["code"], "outcome_unknown")
        self.assertIn("missing_fixture",
                      [item["code"] for item in by_call[str(self.read_id)]["refusals"]])
        self.assertEqual(document["counts"]["calls"], 3)

    async def test_plan_with_a_case_fixture_marks_the_read_replayable(self):
        case = {"fixture": {"kind": "directory", "path": "eval/fixtures/approval-gated-write"}}
        document = await planning.ReplayPlanning(self.database).plan(self.run_id, case=case)
        reads = [item for item in document["calls"] if item["tool"] == "read_file"]
        self.assertEqual(len(reads), 1)
        self.assertTrue(reads[0]["eligible"], reads[0]["refusals"])
        self.assertEqual(document["fixture"]["files"], 1)


if __name__ == "__main__":
    unittest.main()
