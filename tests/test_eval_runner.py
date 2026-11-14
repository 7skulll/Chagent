"""The runner: workspace in, graded trial out, nothing left behind.

The pure tests fix the pieces that are easy to get subtly wrong — which end
reason is infrastructure, what a fixture kind may be, which axis a case is
compared against. The database test then runs the whole path with a
deterministic client: a real workspace, a real session, a real tool call, real
grading, and a cleanup that is verified rather than assumed.
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from eval.casefile import Case
from eval.prereg import Arm, build
from eval.runner import (
    TrialError, TrialPlan, _hosts_unchanged, _unit_tests, assert_axes, declared_axes,
    fixture_for, prime_hosts, run_trial, snapshot_files, status_for,
)


def case(**overrides):
    document = dict(id="demo/write-a-file", version=1, title="t", capability="project-work",
                    prompt="写一个 note.txt，内容为 hi。", authority={"boundary": "host",
                                                                 "execution": "full",
                                                                 "gate": "none"},
                    budget={"max_turns": 3, "wall_seconds": 60, "max_cost_usd": None,
                            "max_tool_calls": 4},
                    graders=({"id": "exists", "kind": "deterministic", "expects": "…",
                              "spec": {"check": "file_exists", "path": "note.txt"}},
                             {"id": "budget", "kind": "deterministic", "expects": "…",
                              "spec": {"check": "tool_calls_at_most", "count": 4}}),
                    cleanup="discard", sha256="a" * 64, fixture={"kind": "empty"})
    document.update(overrides)
    return Case(**document)


class StatusTests(unittest.TestCase):
    def test_provider_and_storage_failures_are_infrastructure(self):
        for reason in ("model_error", "model_protocol_error", "storage_error"):
            self.assertEqual(status_for(reason, None), "infra_error", reason)

    def test_budget_endings_are_failures_of_the_attempt(self):
        for reason in ("turn_limit", "run_timeout", "output_limit",
                       "context_budget_exhausted", "repeated_tool_call"):
            self.assertEqual(status_for(reason, None), "failed", reason)

    def test_cancelled_and_interrupted_are_separate(self):
        self.assertEqual(status_for("user_cancelled", None), "cancelled")
        self.assertEqual(status_for("owner_lost", None), "interrupted")

    def test_an_exception_is_infrastructure(self):
        self.assertEqual(status_for("model_stop", "TrialError: boom"), "infra_error")


class AxisTests(unittest.TestCase):
    def settings(self, **overrides):
        from chagent.config import Settings

        return Settings(**{"execution_profile": "configured", **overrides})

    def test_no_container_is_the_host(self):
        self.assertEqual(declared_axes(self.settings())["boundary"], "host")

    def test_a_container_declaration_matches_a_container_runner(self):
        settings = self.settings(execution_profile="containerized", container_declared=True)
        assert_axes(case(authority={"boundary": "container", "execution": "full",
                                    "gate": "none"}), settings)

    def test_a_container_case_on_a_host_runner_is_refused(self):
        with self.assertRaises(TrialError) as caught:
            assert_axes(case(authority={"boundary": "container", "execution": "full",
                                        "gate": "none"}), self.settings())
        self.assertIn("boundary", str(caught.exception))

    def test_a_read_only_case_on_a_writable_runner_is_refused(self):
        with self.assertRaises(TrialError):
            assert_axes(case(authority={"boundary": "host", "execution": "read-only",
                                        "gate": "none"}), self.settings())


class FixtureTests(unittest.TestCase):
    def test_empty_fixture_builds_nothing(self):
        self.assertEqual(fixture_for(case()).files, {})

    def test_directory_fixture_reads_its_files(self):
        fixture = fixture_for(case(fixture={"kind": "directory", "path": "eval/fixtures/off-by-one"}))
        self.assertIn("stats.py", fixture.files)
        self.assertIn("tests/test_stats.py", fixture.files)

    def test_tarball_is_refused_by_name(self):
        with self.assertRaises(TrialError) as caught:
            fixture_for(case(fixture={"kind": "tarball", "path": "x.tar", "sha256": "0" * 64}))
        self.assertIn("tarball", str(caught.exception))

    def test_fixture_outside_the_checkout_is_refused(self):
        with self.assertRaises(TrialError):
            fixture_for(case(fixture={"kind": "directory", "path": "/etc"}))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_files_are_hashed_by_relative_path(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "a.txt").write_text("x", encoding="utf-8")
        facts = snapshot_files(self.root)
        self.assertEqual(facts["sub/a.txt"],
                         "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881")

    def test_a_symlink_is_marked_not_followed(self):
        (self.root / "target.txt").write_text("x", encoding="utf-8")
        os.symlink(self.root / "target.txt", self.root / "link.txt")
        self.assertEqual(snapshot_files(self.root)["link.txt"], "symlink")

    def test_missing_root_is_empty(self):
        self.assertEqual(snapshot_files(self.root / "absent"), {})


class ControllerCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "tests").mkdir()
        # What these tests classify is unittest output, not confinement: a host
        # whose sandbox cannot start has its own test (IsolationStartupRegressions).
        # Substituting the launcher keeps the classification under test everywhere;
        # the confined path is exercised where a sandbox can actually run.
        launcher = patch('eval.isolation.command',
                         side_effect=lambda workspace, scratch, bootstrap:
                         [sys.executable, '-I', '-S', '-B', '-c', bootstrap])
        launcher.start()
        self.addCleanup(launcher.stop)

    def write(self, name, body):
        (self.root / name).write_text(body, encoding="utf-8")

    def test_a_passing_suite_is_diagnostic_only(self):
        self.write("tests/test_ok.py", "import unittest\n"
                                       "class T(unittest.TestCase):\n"
                                       "    def test_one(self):\n"
                                       "        self.assertTrue(True)\n")
        verdict, detail = _unit_tests(self.root)
        self.assertEqual(verdict, "unverified", detail)

    def test_a_failing_suite_fails(self):
        self.write("tests/test_bad.py", "import unittest\n"
                                        "class T(unittest.TestCase):\n"
                                        "    def test_one(self):\n"
                                        "        self.assertEqual(1, 2)\n")
        self.assertEqual(_unit_tests(self.root)[0], "fail")

    def test_zero_tests_is_not_a_pass(self):
        verdict, detail = _unit_tests(self.root)
        self.assertEqual(verdict, "empty")
        self.assertIn("not a pass", detail)

    def test_a_skip_is_reported_as_a_skip(self):
        self.write("tests/test_skip.py", "import unittest\n"
                                         "class T(unittest.TestCase):\n"
                                         "    @unittest.skip('later')\n"
                                         "    def test_one(self):\n"
                                         "        pass\n")
        verdict, detail = _unit_tests(self.root)
        self.assertEqual(verdict, "skipped", detail)

    def test_hosts_without_a_prior_reading_is_unreadable(self):
        verdict, detail = _hosts_unchanged(self.root / uuid4().hex)
        self.assertEqual(verdict, "unreadable")
        self.assertIn("before the run", detail)

    def test_hosts_matches_its_primed_reading(self):
        marker = self.root / uuid4().hex
        prime_hosts(marker)
        self.assertEqual(_hosts_unchanged(marker)[0], "pass")
        # The reading is consumed: a second check without a new prime cannot pass.
        self.assertEqual(_hosts_unchanged(marker)[0], "unreadable")


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class TrialRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from psycopg import AsyncConnection, sql
        from psycopg.conninfo import make_conninfo

        from chagent.config import Settings
        from chagent.storage.database import Database
        from chagent.storage.migrate import migrate

        self.schema = "chagent_eval_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"],
                                                   autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)

    async def drop_schema(self):
        from psycopg import sql

        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
            sql.Identifier(self.schema)))

    async def test_a_deterministic_client_produces_a_graded_trial(self):
        from chagent.models.fake import FakeModelClient

        plan = TrialPlan(protocol=build(
            experiment="demo-runner", question="q", hypothesis="h", cases=[case()],
            arms=[Arm("baseline", "deepseek", "one"), Arm("candidate", "deepseek", "two")],
            repeats=3, min_repeats_for_stability=3, order="interleaved",
            budget={"max_turns": 4, "wall_seconds": 60, "max_tool_calls": 4,
                    "total_cost_usd": 1.0},
            thresholds={"improve_points": 0.2, "regress_points": 0.2},
            analysis="a", exclusions="e", stopping_rule="s"), case=case(),
            arm=Arm("baseline", "deepseek", "one"), repeat=0)
        client = FakeModelClient("write_file", {"path": "note.txt", "content": "hi"})
        record = await run_trial(
            plan, database=self.database, client=client,
            env={"DEEPSEEK_API_KEY": "test-key", "CHAGENT_PROVIDER": "deepseek",
                 "CHAGENT_MODEL": "deepseek-flash"})
        self.assertEqual(record.status, "completed", record.notes)
        self.assertEqual(record.verdict, "pass", record.graders)
        self.assertEqual(record.manifest["arm"], "baseline")
        self.assertTrue(record.manifest["harness"]["cleanup_verified"])
        self.assertTrue(record.manifest["prompts"]["request_sha256"])
        self.assertIsNotNone(record.run_id)
        self.assertIn("cleanup verified=True", record.notes)

    async def test_a_fixture_case_grades_the_files_it_shipped(self):
        from chagent.models.fake import FakeModelClient

        graded = case(
            id="demo/fix", fixture={"kind": "directory", "path": "eval/fixtures/off-by-one"},
            graders=({"id": "tests_unmodified", "kind": "deterministic", "expects": "…",
                      "spec": {"check": "file_sha256", "path": "tests/test_stats.py",
                               "sha256": "c765e52faa7e33f60119eddc7cf8254d0fa93b622bd3cc3080f65e880ba9fa3b"}},))
        plan = TrialPlan(protocol=build(
            experiment="demo-runner", question="q", hypothesis="h", cases=[graded],
            arms=[Arm("baseline", "deepseek", "one"), Arm("candidate", "deepseek", "two")],
            repeats=3, min_repeats_for_stability=3, order="interleaved",
            budget={"max_turns": 4, "wall_seconds": 60, "max_tool_calls": 4,
                    "total_cost_usd": 1.0},
            thresholds={"improve_points": 0.2, "regress_points": 0.2},
            analysis="a", exclusions="e", stopping_rule="s"), case=graded,
            arm=Arm("baseline", "deepseek", "one"), repeat=0)
        client = FakeModelClient("read_file", {"path": "stats.py"})
        record = await run_trial(
            plan, database=self.database, client=client,
            env={"DEEPSEEK_API_KEY": "test-key", "CHAGENT_PROVIDER": "deepseek",
                 "CHAGENT_MODEL": "deepseek-flash"})
        self.assertEqual(record.verdict, "pass", record.graders)


if __name__ == "__main__":
    unittest.main()
