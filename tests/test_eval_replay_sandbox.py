"""P3 isolated stateful replay: refusals, real tool execution, verified cleanup.

The calls here go through the real tools (``write_file``, ``edit_file``,
``apply_patch``, ``create_directory``) inside a directory built from a fixture.
That is what makes the state comparison mean something: the file on disk was
produced by the product code, not by the test writing bytes and then reading them
back.

Refusal is tested as a result, not as an exception style: an absolute path, a
traversal, a host command, a database tool and a missing starting file each have
to stop before anything is created.
"""

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from eval.replay.sandbox import (
    CleanupError, Fixture, FixtureInvalid, ReplayRefused, Scenario, UnsafePath,
    Workspace, check_relative, load_scenario, patch_targets, required_state, run_call,
    run_scenario,
)


class FixtureTests(unittest.TestCase):
    def test_rejects_traversal_and_absolute_paths(self):
        for name in ("../x", "/etc/passwd", "a/../../b", "C:/x", "a\\b"):
            with self.subTest(name=name), self.assertRaises(UnsafePath):
                Fixture({name: "x"})

    def test_rejects_duplicate_count_above_the_bound(self):
        files = {f"f{index}.txt": "" for index in range(501)}
        with self.assertRaises(FixtureInvalid):
            Fixture(files)

    def test_rejects_content_above_the_byte_bound(self):
        with self.assertRaises(FixtureInvalid):
            Fixture({"big.txt": "x" * (2 * 1024 * 1024 + 1)})

    def test_rejects_non_text_content(self):
        with self.assertRaises(FixtureInvalid):
            Fixture({"a.txt": b"bytes"})

    def test_directory_fixture_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            (root / "a.txt").write_text("x", encoding="utf-8")
            os.symlink(root / "a.txt", root / "link.txt")
            with self.assertRaises(FixtureInvalid):
                Fixture.from_directory(root)

    def test_directory_fixture_refuses_binary_files(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            (root / "blob.bin").write_bytes(b"\xff\xfe\x00")
            with self.assertRaises(FixtureInvalid):
                Fixture.from_directory(root)

    def test_directory_fixture_reads_text_and_skips_git(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            (root / "a.txt").write_text("x", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("y", encoding="utf-8")
            fixture = Fixture.from_directory(root)
            self.assertEqual(fixture.files, {"a.txt": "x"})

    def test_json_round_trip(self):
        fixture = Fixture({"a.txt": "x"}, notes="note")
        self.assertEqual(Fixture.from_json(fixture.to_json()), fixture)

    def test_covers_the_root_and_parent_directories(self):
        fixture = Fixture({"src/app.py": "x"})
        self.assertTrue(fixture.covers("."))
        self.assertTrue(fixture.covers("src"))
        self.assertTrue(fixture.covers("src/app.py"))
        self.assertFalse(fixture.covers("other.py"))

    def test_json_fixture_needs_files(self):
        with self.assertRaises(FixtureInvalid):
            Fixture.from_json({"notes": "nothing"})


class PathTests(unittest.TestCase):
    def test_accepts_plain_relative_paths(self):
        self.assertEqual(check_relative("./a/b.txt"), "a/b.txt")
        self.assertEqual(check_relative("a//b"), "a/b")

    def test_refuses_escapes(self):
        for value in ("", " ", "/x", "~/x", "\\x", "..", "a/../b"):
            with self.subTest(value=value), self.assertRaises(UnsafePath):
                check_relative(value)

    def test_required_state_follows_the_tool(self):
        self.assertEqual(required_state("read_file", {"path": "a.txt"}), ("a.txt",))
        self.assertEqual(required_state("create_directory", {"path": "new"}), ())
        self.assertEqual(required_state("write_file", {"path": "new.txt", "content": ""}), ())
        self.assertEqual(required_state("write_file", {"path": "old.txt", "content": "",
                                                       "expected_sha256": "a" * 64}), ("old.txt",))
        self.assertEqual(required_state("list_dir", {}), (".",))

    def test_patch_targets_say_which_files_are_created(self):
        patch = ("--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hi\n"
                 "--- a/old.txt\n+++ b/old.txt\n@@ -1 +1 @@\n-hi\n+bye\n")
        self.assertEqual(patch_targets(patch), (("new.txt", True), ("old.txt", False)))
        self.assertEqual(required_state("apply_patch", {"patch": patch}), ("old.txt",))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="replay-test-"))
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_build_writes_nested_files_and_removes_them(self):
        workspace = Workspace(Fixture({"src/app.py": "print(1)\n"}), base=self.base)
        root = workspace.build()
        self.assertEqual((root / "src" / "app.py").read_text(encoding="utf-8"), "print(1)\n")
        report = workspace.cleanup()
        self.assertTrue(report.verified)
        self.assertEqual(report.removed_files, 1)
        self.assertFalse(root.exists())
        self.assertEqual(list(self.base.iterdir()), [])

    def test_snapshot_records_files_and_directories(self):
        with Workspace(Fixture({"a.txt": "one"}), base=self.base) as workspace:
            facts = workspace.snapshot()
        self.assertEqual(facts["a.txt"]["kind"], "file")
        self.assertEqual(facts["a.txt"]["size"], 3)

    def test_cleanup_twice_is_harmless(self):
        workspace = Workspace(Fixture({}), base=self.base)
        workspace.build()
        self.assertTrue(workspace.cleanup().verified)
        second = workspace.cleanup()
        self.assertTrue(second.verified)
        self.assertEqual(second.note, "nothing was built")

    def test_unverified_cleanup_raises_through_the_context_manager(self):
        workspace = Workspace(Fixture({"a.txt": "one"}), base=self.base)
        root = workspace.build()
        with mock.patch("eval.replay.sandbox.shutil.rmtree", lambda *a, **k: None):
            report = workspace.cleanup()
            self.assertFalse(report.verified)
            shutil.rmtree(root, ignore_errors=True)
        workspace = Workspace(Fixture({"a.txt": "one"}), base=self.base)
        workspace.build()
        with mock.patch("eval.replay.sandbox.shutil.rmtree", lambda *a, **k: None):
            with self.assertRaises(CleanupError):
                workspace.__exit__(None, None, None)
            for leftover in self.base.iterdir():
                shutil.rmtree(leftover, ignore_errors=True)


class RunCallTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="replay-call-"))
        self.addCleanup(shutil.rmtree, self.base, True)

    def call(self, filename, text, tool, arguments):
        with Workspace(Fixture({filename: text}), base=self.base) as workspace:
            outcome = run_call(tool, arguments, workspace)
            files = {name: (workspace.path / name).read_text(encoding="utf-8")
                     for name in (workspace.snapshot() or {})
                     if (workspace.path / name).is_file()}
            report = workspace.cleanup()
        return outcome, files, report

    def test_write_file_creates_the_declared_content(self):
        outcome, files, report = self.call(
            "existing.txt", "keep", "write_file",
            {"path": "new.txt", "content": "hello\n"})
        self.assertTrue(outcome.ok)
        self.assertEqual(files["new.txt"], "hello\n")
        self.assertTrue(report.verified)

    def test_edit_file_applies_the_recorded_edit(self):
        outcome, files, _ = self.call(
            "a.txt", "hello world\n", "edit_file",
            {"path": "a.txt", "edits": [{"old_text": "world", "new_text": "there"}]})
        self.assertTrue(outcome.ok)
        self.assertEqual(files["a.txt"], "hello there\n")

    def test_legacy_edit_shape_is_normalised_like_the_executor_does(self):
        outcome, files, _ = self.call(
            "a.txt", "hello world\n", "edit_file",
            {"path": "a.txt", "old_text": "world", "new_text": "there"})
        self.assertTrue(outcome.ok)
        self.assertEqual(files["a.txt"], "hello there\n")

    def test_apply_patch_creates_and_modifies(self):
        patch = ("--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hi\n"
                 "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n")
        outcome, files, _ = self.call("a.txt", "old\n", "apply_patch", {"patch": patch})
        self.assertTrue(outcome.ok)
        self.assertEqual(files["a.txt"], "new\n")
        self.assertEqual(files["new.txt"], "hi\n")

    def test_create_directory_is_recorded_as_state(self):
        with Workspace(Fixture({}), base=self.base) as workspace:
            self.assertTrue(run_call("create_directory", {"path": "sub"}, workspace).ok)
            self.assertEqual(workspace.snapshot(), {"sub": {"kind": "directory"}})

    def test_host_command_is_refused(self):
        with Workspace(Fixture({}), base=self.base) as workspace:
            with self.assertRaises(ReplayRefused) as caught:
                run_call("exec_command", {"cmd": "ls"}, workspace)
        self.assertIn("host", str(caught.exception))

    def test_network_and_service_tools_are_refused(self):
        for name in ("web_fetch", "web_search", "site", "collect_file"):
            with self.subTest(name=name), Workspace(Fixture({}), base=self.base) as workspace:
                with self.assertRaises(ReplayRefused):
                    run_call(name, {"path": "x"}, workspace)

    def test_database_tools_are_refused(self):
        with Workspace(Fixture({}), base=self.base) as workspace:
            with self.assertRaises(ReplayRefused) as caught:
                run_call("read_artifact", {"path": "x"}, workspace)
        self.assertIn("database", str(caught.exception))

    def test_unknown_tool_is_refused(self):
        with Workspace(Fixture({}), base=self.base) as workspace:
            with self.assertRaises(ReplayRefused) as caught:
                run_call("invent_tool", {}, workspace)
        self.assertIn("no declaration", str(caught.exception))

    def test_missing_initial_state_is_refused_before_execution(self):
        with Workspace(Fixture({}), base=self.base) as workspace:
            with self.assertRaises(ReplayRefused) as caught:
                run_call("edit_file", {"path": "absent.txt", "old_text": "a", "new_text": "b"},
                         workspace)
            self.assertEqual(workspace.snapshot(), {})
        self.assertIn("missing initial state", str(caught.exception))

    def test_a_failure_scenario_may_declare_absent_state(self):
        with Workspace(Fixture({}), base=self.base) as workspace:
            outcome = run_call("edit_file", {"path": "absent.txt", "old_text": "a",
                                             "new_text": "b"}, workspace,
                               require_initial_state=False)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "FileNotFoundError")

    def test_unsafe_argument_is_refused_before_any_write(self):
        with Workspace(Fixture({}), base=self.base) as workspace:
            with self.assertRaises(UnsafePath):
                run_call("write_file", {"path": "../escape.txt", "content": "x"}, workspace)
            with self.assertRaises(UnsafePath):
                run_call("write_file", {"path": "/workspace/../../escape.txt", "content": "x"},
                         workspace)
            self.assertFalse((self.base / "escape.txt").exists())
            self.assertEqual(workspace.snapshot(), {})

    def test_schema_violation_is_an_error_contract_not_a_crash(self):
        with Workspace(Fixture({}), base=self.base) as workspace:
            outcome = run_call("write_file", {"path": "a.txt"}, workspace)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "ValidationError")


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="replay-scenario-"))
        self.addCleanup(shutil.rmtree, self.base, True)

    def run_document(self, scenario):
        document = run_scenario(scenario, base=self.base)
        self.assertEqual(list(self.base.iterdir()), [], "the replay directory was not removed")
        return document

    def test_matching_scenario_is_ok(self):
        scenario = Scenario(
            "write-then-read", Fixture({"a.txt": "before\n"}),
            ({"tool": "write_file",
              "arguments": {"path": "a.txt", "content": "42", "overwrite": True}},),
            {"state": {"a.txt": {"content": "42"}}})
        document = self.run_document(scenario)
        self.assertTrue(document["ok"], document)
        self.assertTrue(document["state"]["equal"])
        self.assertTrue(document["cleanup"]["verified"])

    def test_wrong_expectation_is_not_ok(self):
        scenario = Scenario("wrong", Fixture({"a.txt": "before\n"}),
                            ({"tool": "write_file",
                              "arguments": {"path": "a.txt", "content": "42",
                                            "overwrite": True}},),
                            {"state": {"a.txt": {"content": "43"}}})
        document = self.run_document(scenario)
        self.assertFalse(document["ok"])
        self.assertFalse(document["state"]["equal"])
        self.assertEqual(document["state"]["differences"][0]["path"], 'files["a.txt"].sha256')

    def test_error_contract_can_be_declared(self):
        scenario = Scenario("missing-file", Fixture({}),
                            ({"tool": "edit_file", "expects_failure": True,
                              "arguments": {"path": "absent.txt", "old_text": "a",
                                            "new_text": "b"}},),
                            {"errors": [{"failed": True, "code": "FileNotFoundError"}]})
        document = self.run_document(scenario)
        self.assertTrue(document["ok"], document)
        self.assertTrue(document["errors"][0]["equal"])

    def test_a_wrong_error_contract_is_not_ok(self):
        scenario = Scenario("wrong-error", Fixture({}),
                            ({"tool": "edit_file", "expects_failure": True,
                              "arguments": {"path": "absent.txt", "old_text": "a",
                                            "new_text": "b"}},),
                            {"errors": [{"failed": False}]})
        document = self.run_document(scenario)
        self.assertFalse(document["ok"])
        self.assertFalse(document["errors"][0]["equal"])

    def test_refusal_still_removes_the_directory(self):
        scenario = Scenario("refused", Fixture({}),
                            ({"tool": "exec_command", "arguments": {"cmd": "ls"}},), {})
        with self.assertRaises(ReplayRefused):
            run_scenario(scenario, base=self.base)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_scenario_document_validation(self):
        for document in ({"id": "x", "calls": []},
                         {"id": "x", "fixture": {"files": {}}, "calls": []},
                         {"fixture": {"files": {}}, "calls": [{"tool": "echo"}]}):
            with self.subTest(document=document), self.assertRaises(FixtureInvalid):
                Scenario.from_json(document)

    def test_json_scenario_keeps_the_failure_declaration(self):
        scenario = Scenario.from_json({
            "id": "json", "fixture": {"files": {}},
            "calls": [{"tool": "edit_file", "expects_failure": True,
                       "arguments": {"path": "absent.txt", "old_text": "a", "new_text": "b"}}],
            "expect": {"errors": [{"failed": True, "code": "FileNotFoundError"}]}})
        self.assertTrue(scenario.calls[0]["expects_failure"])
        self.assertTrue(self.run_document(scenario)["ok"])

    def test_load_scenario_reports_the_file(self):
        path = self.base / "scenario.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(FixtureInvalid) as caught:
            load_scenario(path)
        self.assertIn("scenario.json", str(caught.exception))


class FixtureAndScenarioRegressionTests(unittest.TestCase):
    def test_directory_fixture_preserves_crlf_cr_and_bom_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'source'
            root.mkdir()
            original = b'\xef\xbb\xbffirst\r\nsecond\rthird\n'
            (root / 'a.txt').write_bytes(original)
            fixture = Fixture.from_directory(root)
            with Workspace(fixture, base=Path(tmp)) as workspace:
                self.assertEqual((workspace.path / 'a.txt').read_bytes(), original)

    def test_directory_links_including_ignored_names_are_refused(self):
        for name in ('linked', '.git'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'source'
                root.mkdir()
                (root / name).symlink_to(Path(tmp), target_is_directory=True)
                with self.assertRaises(FixtureInvalid):
                    Fixture.from_directory(root)

    def test_symlink_fixture_root_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'source'
            root.mkdir()
            link = Path(tmp) / 'link'
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(FixtureInvalid):
                Fixture.from_directory(link)

    def test_empty_directories_survive_fixture_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'source'
            (root / 'empty' / 'nested').mkdir(parents=True)
            fixture = Fixture.from_directory(root)
            rebuilt = Fixture.from_json(fixture.to_json())
            self.assertTrue(rebuilt.covers('empty/nested'))
            with Workspace(rebuilt, base=Path(tmp)) as workspace:
                self.assertTrue((workspace.path / 'empty' / 'nested').is_dir())

    def test_json_does_not_coerce_non_text_into_fixture_content(self):
        for value in (None, 1, False, ['text']):
            with self.subTest(value=value), self.assertRaises(FixtureInvalid):
                Fixture.from_json({'files': {'x': value}})

    def test_unexpected_failure_cannot_pass_on_unchanged_state(self):
        scenario = Scenario('unexpected', Fixture({'a.txt': 'before'}),
                            ({'tool': 'write_file', 'arguments': {'path': 'a.txt', 'content': 'after'}},),
                            {'state': {'a.txt': {'content': 'before'}}})
        result = run_scenario(scenario)
        self.assertTrue(result['state']['equal'])
        self.assertTrue(result['calls'][0]['contract']['failed'])
        self.assertFalse(result['calls_ok'])
        self.assertFalse(result['ok'])
        self.assertTrue(result['cleanup']['verified'])

    def test_empty_expected_state_is_checked_instead_of_skipped(self):
        scenario = Scenario('empty', Fixture({}),
                            ({'tool': 'write_file', 'arguments': {'path': 'x', 'content': 'new'}},),
                            {'state': {}})
        result = run_scenario(scenario)
        self.assertFalse(result['ok'])
        self.assertFalse(result['state']['equal'])

    def test_default_success_checks_every_call(self):
        scenario = Scenario('two', Fixture({'a': 'before'}),
                            ({'tool': 'write_file', 'arguments': {'path': 'new', 'content': 'ok'}},
                             {'tool': 'write_file', 'arguments': {'path': 'a', 'content': 'bad'}}), {})
        result = run_scenario(scenario)
        self.assertFalse(result['ok'])

    def test_failure_flag_without_error_contract_does_not_pass(self):
        scenario = Scenario('ungraded-failure', Fixture({}),
                            ({'tool': 'read_file', 'arguments': {'path': 'missing'},
                              'expects_failure': True},), {})
        self.assertFalse(run_scenario(scenario)['ok'])

    def test_partial_error_contract_list_is_rejected_before_execution(self):
        scenario = Scenario('partial', Fixture({}),
                            ({'tool': 'write_file', 'arguments': {'path': 'new', 'content': 'ok'}},),
                            {'errors': []})
        with mock.patch('eval.replay.sandbox.run_call') as execute:
            with self.assertRaises(FixtureInvalid):
                run_scenario(scenario)
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
