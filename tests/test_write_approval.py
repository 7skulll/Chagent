"""The half of the boundary a command gate cannot reach: what a write replaces."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from chagent.domain.messages import ToolCall
from chagent.tools.approvals import ApprovalOutcome
from chagent.tools.builtin import WRITE_TOOLS
from chagent.tools.context import ToolContext
from chagent.tools.gate import WriteGate, grade_write
from chagent.tools.policy import FILE_WRITE_TOOLS, ApprovalPolicy


class WriteGradingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        (self.workspace / "existing.py").write_text("x" * 1536, encoding="utf-8")
        (self.workspace / ".git").mkdir()

    def grade(self, name, **arguments):
        return grade_write(name, arguments, self.workspace)

    def test_a_new_file_is_not_a_decision(self):
        for path in ("notes.md", "src/new/module.py", "output/report.txt"):
            with self.subTest(path=path):
                self.assertEqual(self.grade("write_file", path=path).reason, "")

    def test_an_overwrite_names_the_file_and_its_current_size(self):
        intent = self.grade("write_file", path="existing.py")
        self.assertEqual(intent.reason, "overwrite")
        self.assertIn("existing.py", intent.subject)
        self.assertIn("1.5 KB", intent.subject)

    def test_a_new_hook_or_config_file_is_still_a_decision(self):
        for path in (".git/hooks/pre-commit", ".github/workflows/ci.yml",
                     ".husky/pre-commit", ".env.local", ".npmrc", ".bashrc"):
            with self.subTest(path=path):
                self.assertEqual(self.grade("write_file", path=path).reason, "sensitive")

    def test_a_path_outside_the_workspace_is_a_decision(self):
        for path in ("../escape.py", "/etc/passwd", "~/notes.md"):
            with self.subTest(path=path):
                self.assertEqual(self.grade("write_file", path=path).reason, "outside")

    def test_a_symlinked_component_is_a_decision(self):
        (self.workspace / "link").symlink_to(tempfile.gettempdir())
        intent = self.grade("write_file", path="link/payload.py")
        self.assertEqual(intent.reason, "outside")

    def test_making_a_directory_is_reversible_and_not_a_decision(self):
        self.assertEqual(self.grade("create_directory", path="build").reason, "")

    def test_a_patch_asks_and_names_its_files(self):
        patch = ("--- a/existing.py\n+++ b/existing.py\n@@ -1 +1 @@\n-x\n+y\n"
                 "--- a/fresh.py\n+++ b/fresh.py\n@@ -0,0 +1 @@\n+y\n")
        intent = self.grade("apply_patch", patch=patch)
        self.assertEqual(intent.reason, "overwrite")
        self.assertEqual(intent.paths, ("existing.py", "fresh.py"))
        self.assertIn("2 file(s)", intent.subject)

    def test_an_unreadable_patch_is_left_to_the_executor(self):
        # The call dies in validation anyway; a question about it would only
        # teach the user to approve garbage.
        self.assertEqual(self.grade("apply_patch", patch="not a diff").reason, "")

    def test_the_graded_set_matches_the_registered_writers(self):
        # Two sets answer different questions — what may exist, and what needs a
        # human — but a tool added to one must not be forgotten by the other.
        self.assertEqual(FILE_WRITE_TOOLS, WRITE_TOOLS)

    def test_reads_and_commands_are_not_graded_here(self):
        for name in ("read_file", "grep", "exec_command", "process"):
            with self.subTest(name=name):
                self.assertEqual(self.grade(name, path="existing.py").reason, "")


class StubBroker:
    def __init__(self, approved=True, reason="approved"):
        self.approved, self.reason, self.calls = approved, reason, []

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        return ApprovalOutcome(self.approved, uuid4(), "", self.reason)


class WriteApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        (self.workspace / "existing.py").write_text("x" * 1536, encoding="utf-8")
        self.context = ToolContext(workspace=self.workspace, session_id=uuid4(), run_id=uuid4())

    def call(self, name, **arguments):
        return ToolCall(id="call-1", name=name, arguments=arguments)

    async def check(self, gate, name, **arguments):
        return await gate.check(self.call(name, **arguments), context=self.context,
                                tool_record_id=uuid4(), signal=asyncio.Event(), budget=None)

    async def test_approval_carries_the_file_into_the_question(self):
        broker = StubBroker()
        decision = await self.check(WriteGate(ApprovalPolicy(mode="untrusted"), broker),
                                    "write_file", path="existing.py", content="new")
        self.assertTrue(decision.allow)
        self.assertIsNotNone(decision.approval_id)
        self.assertEqual(broker.calls[0]["tool_name"], "write_file")
        self.assertIn("overwrite existing.py", broker.calls[0]["subject"])

    async def test_denial_says_the_file_was_not_modified(self):
        broker = StubBroker(approved=False, reason="denied")
        decision = await self.check(WriteGate(ApprovalPolicy(mode="untrusted"), broker),
                                    "write_file", path="existing.py", content="new")
        self.assertFalse(decision.allow)
        self.assertEqual(decision.code, "denied")
        self.assertIn("file was not modified", decision.reason)
        self.assertIn("Do not resend", decision.reason)

    async def test_a_new_file_never_reaches_the_broker(self):
        broker = StubBroker()
        decision = await self.check(WriteGate(ApprovalPolicy(mode="untrusted"), broker),
                                    "write_file", path="fresh.py", content="new")
        self.assertTrue(decision.allow)
        self.assertEqual(broker.calls, [])

    async def test_approvals_off_means_no_question_at_all(self):
        broker = StubBroker()
        decision = await self.check(WriteGate(ApprovalPolicy(mode="never"), broker),
                                    "write_file", path="existing.py", content="new")
        self.assertTrue(decision.allow)
        self.assertEqual(broker.calls, [])

    async def test_a_missing_broker_refuses_instead_of_writing(self):
        decision = await self.check(WriteGate(ApprovalPolicy(mode="untrusted"), None),
                                    "write_file", path="existing.py", content="new")
        self.assertFalse(decision.allow)
        self.assertEqual(decision.code, "unavailable")
        self.assertIn("file was not modified", decision.reason)


class GateWiringTests(unittest.TestCase):
    def scheduler(self, mode, sandbox_mode="off"):
        from chagent.tools.scheduler import ToolScheduler

        return ToolScheduler(None, policy=ApprovalPolicy(mode=mode), sandbox_mode=sandbox_mode)

    def test_a_run_with_approvals_asks_both_questions(self):
        names = [gate.name for gate in self.scheduler("untrusted").default_gates()]
        self.assertEqual(names, ["human", "write"])

    def test_never_asks_nothing_so_an_upgrade_does_not_block(self):
        self.assertEqual(self.scheduler("never").default_gates(), ())

    def test_the_host_controller_asks_about_writes_too(self):
        # The regression this guards: the profile kept only HumanGate, whose
        # subject in that build is native commands, so every overwrite went
        # through without a card.
        names = [gate.name for gate in
                 self.scheduler("untrusted", "managed-container").default_gates()]
        self.assertEqual(names, ["human", "write"])

    def test_the_host_controller_still_asks_about_native_commands_with_approvals_off(self):
        names = [gate.name for gate in
                 self.scheduler("never", "managed-container").default_gates()]
        self.assertEqual(names, ["human"])


class HostControllerWriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name).resolve()
        (self.workspace / "existing.py").write_text("old", encoding="utf-8")
        self.context = ToolContext(workspace=self.workspace, session_id=uuid4(), run_id=uuid4())

    def write_gate(self, broker):
        from chagent.tools.scheduler import ToolScheduler

        gates = ToolScheduler(None, policy=ApprovalPolicy(mode="untrusted"), approvals=broker,
                              sandbox_mode="managed-container").default_gates()
        return next(gate for gate in gates if gate.name == "write")

    async def check(self, gate, name, **arguments):
        return await gate.check(ToolCall(id="call-1", name=name, arguments=arguments),
                                context=self.context, tool_record_id=uuid4(),
                                signal=asyncio.Event(), budget=None)

    async def test_an_overwrite_in_this_profile_reaches_the_broker(self):
        broker = StubBroker()
        decision = await self.check(self.write_gate(broker), "write_file",
                                    path="existing.py", content="new")
        self.assertTrue(decision.allow)
        self.assertEqual(broker.calls[0]["tool_name"], "write_file")
        self.assertIn("overwrite existing.py", broker.calls[0]["subject"])

    async def test_a_new_file_in_this_profile_is_still_quiet(self):
        broker = StubBroker()
        decision = await self.check(self.write_gate(broker), "write_file",
                                    path="fresh.py", content="new")
        self.assertTrue(decision.allow)
        self.assertEqual(broker.calls, [])

    async def test_a_denied_overwrite_does_not_write(self):
        broker = StubBroker(approved=False, reason="denied")
        decision = await self.check(self.write_gate(broker), "write_file",
                                    path="existing.py", content="new")
        self.assertFalse(decision.allow)
        self.assertEqual(decision.code, "denied")
        self.assertIn("file was not modified", decision.reason)


if __name__ == "__main__":
    unittest.main()
