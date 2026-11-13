"""P0 evaluation contracts: version manifests, cases, trial records, redaction.

These tests are deterministic: no model, no network, no database except in the
opt-in class at the bottom that follows the project's ``CHAGENT_TEST_DATABASE_URL``
convention. The prompt-fingerprint test builds its input from the real domain
dataclasses and compares against ``transcript.normalize``, so a change in the
stored request shape fails here instead of silently hashing the wrong bytes.
"""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from uuid import uuid4

from chagent.domain.messages import SystemMessage, TextContent, UserMessage
from chagent.domain.tools import Tool, ToolReference
from chagent.models.base import Model, ModelRequest
from chagent.models.transcript import normalize

from eval import casefile, cli
from eval.evidence import _payload
from eval.manifests import (
    CaseInvalid, VersionManifest, prompt_material, sha256_json, sha256_text,
)
from eval.records import GraderResult, TrialInvalid, TrialRecord, TrialStore, new_trial

REPOSITORY = Path(__file__).resolve().parent.parent
MODEL = Model(id="test-model", provider="local-test", api="openai-chat",
              context_window=8192, max_tokens=256)


def stored_request(*, extra_user: bool = False, drop_tool: bool = False):
    """The request as ``start_model_call`` stores it: ``asdict`` through jsonb."""
    tool = Tool(name="read_file", description="Read a file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}},
                            "required": ["path"]})
    messages = [SystemMessage(content="You are Chagent.", timestamp=0,
                              sections={"style": "terse"}, tools_added=[tool])]
    if drop_tool:
        messages.append(SystemMessage(content="", timestamp=1,
                                      tools_removed=[ToolReference(name="read_file")]))
    messages.append(UserMessage(content="hello", timestamp=2))
    if extra_user:
        messages.append(UserMessage(content="again", timestamp=3))
    request = ModelRequest(model=MODEL, messages=messages, max_tokens=128)
    return json.loads(json.dumps(asdict(request), default=str)), request


def case_document(**overrides) -> dict:
    document = {
        "id": "skill-selection/explicit-name",
        "version": 1,
        "title": "An explicitly named skill is read, not guessed",
        "capability": "skill-selection",
        "prompt": "使用 task-handoff 整理当前任务的交接说明。",
        "fixture": {"kind": "empty"},
        "authority": {"boundary": "container", "execution": "full", "gate": "approval"},
        "budget": {"max_turns": 6, "wall_seconds": 180, "max_cost_usd": None},
        "graders": [
            {"id": "reads_named_skill", "kind": "deterministic",
             "expects": "read_skill called with name=task-handoff", "evidence": "tool_calls"},
            {"id": "no_write_before_answer", "kind": "deterministic",
             "expects": "no workspace write before the first assistant text", "evidence": "run_events"},
        ],
        "cleanup": "Delete the trial workspace; no service is started.",
    }
    document.update(overrides)
    return document


class PromptMaterialTests(unittest.TestCase):
    def test_multi_block_prompt_matches_adapter_and_does_not_collide_with_flat_text(self):
        request = ModelRequest(MODEL, [SystemMessage([TextContent("A"), TextContent("B")], 0)])
        prompt, _ = prompt_material(asdict(request))
        self.assertEqual(prompt, normalize(request)[0])
        flat = ModelRequest(MODEL, [SystemMessage("AB", 0)])
        self.assertNotEqual(sha256_text(prompt), sha256_text(prompt_material(asdict(flat))[0]))

    def test_matches_transcript_normalize(self):
        stored, request = stored_request()
        prompt, declarations = prompt_material(stored)
        instructions, tools, _ = normalize(request)
        self.assertEqual(prompt, instructions)
        self.assertEqual([tool["name"] for tool in declarations], sorted(tool.name for tool in tools))
        self.assertEqual(declarations[0]["parameters"], tools[0].parameters)

    def test_sections_override_by_name_and_removed_tools_disappear(self):
        stored, _ = stored_request(drop_tool=True)
        prompt, declarations = prompt_material(stored)
        self.assertEqual(declarations, [])
        self.assertIn("You are Chagent.", prompt)

    def test_history_growth_is_not_a_version_change(self):
        short, _ = stored_request()
        longer, _ = stored_request(extra_user=True)
        self.assertEqual(prompt_material(short)[0], prompt_material(longer)[0])
        self.assertEqual(sha256_json(prompt_material(short)[1]), sha256_json(prompt_material(longer)[1]))


class ManifestTests(unittest.TestCase):
    def test_skill_hashes_belong_to_selected_checkout_without_executing_it(self):
        folder = self.root / "src/chagent/skills"
        folder.mkdir(parents=True)
        (folder / "__init__.py").write_text('raise RuntimeError("do not execute candidate")\nSKILL_NAMES = ("task-handoff",)\n')
        skill = folder / "task-handoff/SKILL.md"
        skill.parent.mkdir()
        skill.write_text("candidate skill v1")
        extra = folder / "not-registered/SKILL.md"
        extra.parent.mkdir()
        extra.write_text("not enabled")
        first = VersionManifest.capture(self.root)
        self.assertEqual(first.skills, {"task-handoff": sha256_text("candidate skill v1")})
        skill.write_text("candidate skill v2")
        self.assertNotEqual(first.skills, VersionManifest.capture(self.root).skills)

    def test_missing_skill_catalog_does_not_import_host_skills(self):
        manifest = VersionManifest.capture(self.root)
        self.assertEqual(manifest.skills, {})
        self.assertIn("skills", manifest.missing())

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "-q")
        self.git("config", "user.email", "eval@example.invalid")
        self.git("config", "user.name", "eval")
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        self.git("add", "a.txt")
        self.git("commit", "-qm", "base")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def test_clean_tree_is_pinned_to_its_commit(self):
        manifest = VersionManifest.capture(self.root)
        self.assertEqual(len(manifest.repo["commit"]), 40)
        self.assertFalse(manifest.repo["dirty"])
        self.assertIsNone(manifest.repo["patch_sha256"])

    def test_untracked_content_changes_the_patch_digest(self):
        """``git diff`` alone misses new files, which are exactly what a change adds."""
        (self.root / "a.txt").write_text("two", encoding="utf-8")
        (self.root / "new.txt").write_text("fresh", encoding="utf-8")
        first = VersionManifest.capture(self.root)
        self.assertTrue(first.repo["dirty"])
        self.assertIsNotNone(first.repo["patch_sha256"])

        (self.root / "new.txt").write_text("changed", encoding="utf-8")
        second = VersionManifest.capture(self.root)
        self.assertNotEqual(first.repo["patch_sha256"], second.repo["patch_sha256"])

        (self.root / "new.txt").write_text("fresh", encoding="utf-8")
        third = VersionManifest.capture(self.root)
        self.assertEqual(first.repo["patch_sha256"], third.repo["patch_sha256"])

    def test_skill_fingerprint_matches_the_bundled_text(self):
        manifest = VersionManifest.capture(REPOSITORY)
        self.assertIn("task-handoff", manifest.skills)
        self.assertGreaterEqual(len(manifest.skills), 8)
        body = (REPOSITORY / "src/chagent/skills/task-handoff/SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(manifest.skills["task-handoff"], sha256_text(body))

    def test_missing_names_what_cannot_be_compared(self):
        manifest = VersionManifest.capture(self.root)
        self.assertIn("model", manifest.missing())
        self.assertIn("tools.declarations_sha256", manifest.missing())
        attached = manifest.with_request(stored_request()[0])
        self.assertNotIn("tools.declarations_sha256", attached.missing())
        self.assertEqual(attached.tools["names"], ["read_file"])
        self.assertNotIn("no model request", " ".join(attached.warnings))

    def test_manifest_reports_its_own_gaps(self):
        manifest = VersionManifest.capture(self.root)
        self.assertTrue(any("image digest" in warning for warning in manifest.warnings))


class CaseTests(unittest.TestCase):
    def test_tarball_requires_path_and_hash_but_empty_fixture_does_not(self):
        for fixture in ({"kind": "tarball"}, {"kind": "tarball", "path": "fixture.tar"},
                        {"kind": "tarball", "sha256": "a" * 64},
                        {"kind": "directory"}, {"kind": "repository"}):
            with self.subTest(fixture=fixture), self.assertRaises(CaseInvalid):
                self.load(case_document(fixture=fixture))
        self.load(case_document(fixture={"kind": "tarball", "path": "fixtures/project.tar", "sha256": "a" * 64}))
        self.load(case_document(fixture={"kind": "empty"}))

    def test_fixture_path_cannot_be_absolute_or_traverse_parent(self):
        for path in ("/tmp/project.tar", "../project.tar", "fixtures/../../project.tar", "C:\\project.tar", "fixtures\\..\\project.tar"):
            with self.subTest(path=path), self.assertRaises(CaseInvalid):
                self.load(case_document(fixture={"kind": "tarball", "path": path, "sha256": "a" * 64}))

    def load(self, document: dict):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "case.json"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return casefile.load(path)

    def test_valid_case_loads_with_a_content_hash(self):
        case = self.load(case_document())
        self.assertEqual(case.key, "skill-selection/explicit-name@1")
        self.assertEqual(case.sha256, sha256_json(case_document()))
        self.assertEqual(len(case.graders), 2)

    def test_unknown_capability_is_rejected_by_name(self):
        with self.assertRaises(CaseInvalid) as caught:
            casefile.validate(case_document(capability="vibes"))
        self.assertIn("capability", str(caught.exception))

    def test_a_case_without_graders_is_rejected(self):
        document = case_document()
        document["graders"] = []
        with self.assertRaises(CaseInvalid) as caught:
            casefile.validate(document)
        self.assertIn("graders", str(caught.exception))

    def test_every_violation_is_reported_at_once(self):
        document = case_document()
        document["version"] = 0
        document["graders"][0]["kind"] = "guesswork"
        with self.assertRaises(CaseInvalid) as caught:
            casefile.validate(document)
        message = str(caught.exception)
        self.assertIn("2 schema violation", message)
        self.assertIn("version", message)
        self.assertIn("kind", message)

    def test_hash_ignores_key_order_and_tracks_expectations(self):
        first = self.load(case_document())
        reordered = dict(reversed(list(case_document().items())))
        second = self.load(reordered)
        self.assertEqual(first.sha256, second.sha256)

        changed = case_document()
        changed["graders"][0]["expects"] = "read_skill called with any name"
        self.assertNotEqual(first.sha256, self.load(changed).sha256)


class TrialTests(unittest.TestCase):
    def test_unrelated_extra_and_duplicate_graders_cannot_substitute_required_checks(self):
        trial = new_trial(self.case, {}, "baseline")
        for graders in ((GraderResult("other_one", "pass"), GraderResult("other_two", "pass")),
                        (GraderResult("reads_named_skill", "pass"), GraderResult("other_two", "pass")),
                        (GraderResult("reads_named_skill", "pass"), GraderResult("reads_named_skill", "pass"))):
            with self.subTest(graders=graders), self.assertRaises(TrialInvalid):
                trial.with_graders(graders)

    def test_infra_error_status_cannot_be_overridden_by_grader_passes(self):
        trial = new_trial(self.case, {}, "baseline", status="infra_error")
        self.assertEqual(trial.with_graders(tuple(GraderResult(g["id"], "pass") for g in self.case.graders)).verdict, "infra_error")

    def test_legacy_count_only_record_cannot_become_pass(self):
        trial = new_trial(self.case, {}, "baseline").with_graders(tuple(GraderResult(g["id"], "pass") for g in self.case.graders))
        document = trial.to_json()
        document.pop("grader_ids", None)
        self.assertEqual(TrialRecord.from_json(document).verdict, "inconclusive")

    def test_trial_id_cannot_escape_store_on_create_or_deserialize(self):
        trial = new_trial(self.case, {}, "baseline")
        for identity in ("../escape", str(self.root / "outside"), "nested/trial", "a\\b", "a\n", ""):
            with self.subTest(identity=identity), self.assertRaises(TrialInvalid):
                replace(trial, trial_id=identity)
            with self.assertRaises(TrialInvalid):
                new_trial(self.case, {}, "baseline", trial_id=identity)
            document = trial.to_json()
            document["trial_id"] = identity
            with self.assertRaises(TrialInvalid):
                TrialRecord.from_json(document)
        self.assertFalse((self.root / "outside.json").exists())

    def test_symlink_experiment_cannot_escape_store(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.store.root.mkdir()
        (self.store.root / "baseline").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(TrialInvalid):
            self.store.write(new_trial(self.case, {}, "baseline"))
        self.assertEqual(list(outside.iterdir()), [])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        path = self.root / "case.json"
        path.write_text(json.dumps(case_document(), ensure_ascii=False), encoding="utf-8")
        self.case = casefile.load(path)
        self.store = TrialStore(self.root / "results")

    def test_a_missing_grader_is_never_a_pass(self):
        trial = new_trial(self.case, {}, "baseline")
        self.assertEqual(trial.verdict, "inconclusive")
        self.assertEqual(trial.with_graders((GraderResult("reads_named_skill", "pass"),)).verdict,
                         "inconclusive")
        self.assertEqual(trial.with_graders((
            GraderResult("reads_named_skill", "pass"),
            GraderResult("no_write_before_answer", "pass"))).verdict, "pass")

    def test_failure_outranks_pass_and_infrastructure_outranks_failure(self):
        trial = new_trial(self.case, {}, "baseline")
        failed = trial.with_graders((GraderResult("reads_named_skill", "fail"),
                                     GraderResult("no_write_before_answer", "pass")))
        self.assertEqual(failed.verdict, "fail")
        broken = trial.with_graders((GraderResult("reads_named_skill", "infra_error"),
                                     GraderResult("no_write_before_answer", "fail")))
        self.assertEqual(broken.verdict, "infra_error")

    def test_store_round_trip_keeps_the_verdict(self):
        trial = new_trial(self.case, {"repo": {"commit": "a" * 40}}, "baseline")
        trial = trial.with_graders((GraderResult("reads_named_skill", "pass", evidence=("tool_calls#3",)),
                                    GraderResult("no_write_before_answer", "pass")))
        path = self.store.write(trial)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix(".tmp").exists())
        self.assertEqual(len((path.parent / "index.jsonl").read_text().splitlines()), 1)
        restored = self.store.read("baseline")
        self.assertEqual([record.verdict for record in restored], ["pass"])
        self.assertEqual(restored[0].graders[0].evidence, ("tool_calls#3",))

    def test_experiment_name_cannot_escape_the_results_directory(self):
        with self.assertRaises(TrialInvalid):
            self.store.directory("../escape")
        with self.assertRaises(TrialInvalid):
            GraderResult("reads_named_skill", "maybe")


class PayloadTests(unittest.TestCase):
    def test_paths_are_projected_and_credentials_redacted(self):
        # The resolved root, because projection matches the resolved form: on
        # macOS /tmp is a symlink and a payload written with the short path would
        # legitimately not be projected.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            payload = {"name": "read_file", "cmd": f"cat {root}/secret.txt",
                       "api_key": "sk-abcdefgh12345678", "nested": {"deep": 1}}
            projected = _payload(payload, root, True)
            self.assertEqual(projected["cmd"], "cat /workspace/secret.txt")
            self.assertEqual(projected["api_key"], "[redacted]")
            self.assertEqual(projected["_omitted_non_scalar"], 1)

    def test_default_projection_omits_unknown_keys_and_bounds_text(self):
        payload = {"name": "run_tests", "extra": "not-a-scalar-we-record", "error": "x" * 4000}
        projected = _payload(payload, None, False)
        self.assertNotIn("extra", projected)
        self.assertEqual(projected["_omitted_keys"], 1)
        self.assertLess(len(projected["error"]), 600)


class CliTests(unittest.TestCase):
    def test_manifest_command_emits_a_version_document(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "manifest.json"
            with redirect_stdout(StringIO()):
                code = cli.main(["manifest", "--repo", str(REPOSITORY), "--out", str(out)])
            self.assertEqual(code, 0)
            document = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], 2)
            self.assertEqual(set(document) >= {"repo", "prompts", "tools", "skills"}, True)

    def test_invalid_case_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(case_document(budget={"max_turns": 0})), encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = cli.main(["case", str(path)])
            self.assertEqual(code, 1)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from psycopg import AsyncConnection, sql
        from psycopg.conninfo import make_conninfo

        from chagent.config import Settings
        from chagent.storage.database import Database
        from chagent.storage.migrate import migrate

        self.schema = "chagent_eval_" + uuid4().hex
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

        self.workspace = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        # The host may store the path as it was given (``/var/...``), while tools
        # record the resolved one (``/private/var/...``). The export has to
        # project against the resolved root for the second form to match.
        resolved = self.workspace.resolve()
        from psycopg.types.json import Jsonb

        self.run_id, session_id, workspace_id = uuid4(), uuid4(), uuid4()
        async with self.database.transaction() as connection:
            await connection.execute("INSERT INTO workspaces (id, root_path) VALUES (%s, %s)",
                                     (workspace_id, str(self.workspace)))
            await connection.execute(
                "INSERT INTO sessions (id, workspace_id, title) VALUES (%s, %s, 'eval')",
                (session_id, workspace_id))
            await connection.execute(
                "INSERT INTO runs (id, session_id, status, end_reason, started_at, ended_at) "
                "VALUES (%s, %s, 'completed', 'model_stop', now(), now())",
                (self.run_id, session_id))
            await connection.execute(
                "INSERT INTO run_events (run_id, sequence, type, payload) VALUES (%s, 1, "
                "'tool_completed', %s)", (self.run_id, Jsonb({
                    "name": "read_file", "call_id": "call-1",
                    "path": str(resolved / "secret.txt"),
                    "error": "sk-abcdefgh12345678", "nested": {"deep": True}})))
            await connection.execute(
                "INSERT INTO run_events (run_id, sequence, type, payload) VALUES (%s, 2, "
                "'run_completed', %s)", (self.run_id, Jsonb({"end_reason": "model_stop"})))

    async def _drop(self):
        from psycopg import sql

        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def test_export_projects_paths_and_redacts_credentials(self):
        from eval.evidence import EvalEvidence

        document = await EvalEvidence(self.database).export_run(self.run_id, include_payload=True)
        self.assertEqual(document["export_version"], 1)
        self.assertEqual(document["run"]["status"], "completed")
        self.assertEqual(document["counts"]["events"], 2)
        self.assertFalse(document["events_truncated"])
        first = document["events"][0]
        self.assertEqual(first["source_ref"], {
            "table": "run_events", "run_id": str(self.run_id), "sequence": 1})
        self.assertEqual(first["payload"]["path"], "/workspace/secret.txt")
        self.assertEqual(first["payload"]["error"], "[redacted]")
        self.assertEqual(first["payload"]["_omitted_non_scalar"], 1)

    async def test_export_bounds_the_event_head_and_says_so(self):
        from eval.evidence import EvalEvidence

        document = await EvalEvidence(self.database).export_run(self.run_id, events=1)
        self.assertEqual(len(document["events"]), 1)
        self.assertTrue(document["events_truncated"])

    async def test_unknown_run_is_not_found(self):
        from eval.evidence import EvalEvidence

        with self.assertRaises(LookupError):
            await EvalEvidence(self.database).export_run(uuid4())


if __name__ == "__main__":
    unittest.main()
