"""Sandbox profiles and the probe that decides whether they may be used.

The pure cases pin the profile text, because a profile is a string that either
means what we think or silently means something else. The platform cases are
skipped where seatbelt does not exist; a skip is not a pass, so the capability
table in ``docs/执行边界设计.md`` §10.4 is the only claim of support.
"""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from chagent.domain.messages import ToolCall
from chagent.tools.context import ToolContext
from chagent.tools.sandbox import (
    DEFAULT_DENY_READ, DisabledBackend, Sandbox, SandboxPolicy, SandboxSettings,
    SandboxSupport, SeatbeltBackend, _sbpl, profile,
)

DARWIN = sys.platform == "darwin" and SeatbeltBackend().available
# The real policy grants the user's TMPDIR so ordinary tools keep working, so
# "outside" has to mean outside TMPDIR as well. The shared /private/tmp is not
# granted, which makes it the honest target for a containment assertion.
OUTSIDE = Path("/private/tmp") / f"chagent-sandbox-outside-{os.getpid()}"


def policy_for(workspace: Path, **overrides) -> SandboxPolicy:
    settings = dict(workspace=workspace.resolve(), network=False, deny_read=(), writable_tmp=False)
    settings.update(overrides)
    return SandboxPolicy(**settings)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="chagent-sb-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.workspace, ignore_errors=True))

    def test_sbpl_quotes_and_escapes(self):
        self.assertEqual(_sbpl("/tmp/plain"), '"/tmp/plain"')
        self.assertEqual(_sbpl(Path('/tmp/with "quote"')), '"/tmp/with \\"quote\\""')
        self.assertEqual(_sbpl("/tmp/back\\slash"), '"/tmp/back\\\\slash"')

    def test_profile_keeps_non_ascii_paths_verbatim(self):
        workspace = Path(tempfile.mkdtemp(prefix="智能体-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(workspace, ignore_errors=True))
        self.assertIn(f'(subpath "{workspace}")', profile(policy_for(workspace)))

    def test_deny_read_drops_paths_that_would_break_the_workspace(self):
        outside = Path(tempfile.mkdtemp(prefix="chagent-out-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        # Protecting a parent of the workspace, or a child of it, would make
        # ordinary edits fail; both are dropped rather than silently applied.
        policy = SandboxPolicy(self.workspace, deny_read=(outside, self.workspace.parent, self.workspace / "tests"))
        self.assertEqual(policy.deny_read, (outside,))
        policy = SandboxPolicy(self.workspace, deny_read=(outside,))
        self.assertEqual(policy.deny_read, (outside,))

    def test_deny_read_resolves_and_deduplicates(self):
        settings = SandboxSettings(mode="workspace-write", deny_read=(Path("~/.ssh"), Path("~/.ssh"), Path("~/./.ssh")))
        self.assertEqual(settings.deny_read, (Path.home() / ".ssh",))

    def test_settings_default_to_the_credential_list_but_empty_is_explicit(self):
        self.assertEqual(SandboxSettings(mode="workspace-write").deny_read,
                         tuple(Path(item).expanduser().resolve() for item in DEFAULT_DENY_READ))
        self.assertEqual(SandboxSettings(mode="workspace-write", deny_read=()).deny_read, ())

    def test_network_denial_follows_the_policy(self):
        self.assertIn("(deny network-outbound)", profile(policy_for(self.workspace)))
        self.assertNotIn("(deny network-outbound)", profile(policy_for(self.workspace, network=True)))

    def test_identical_policies_render_identical_profiles(self):
        first, second = policy_for(self.workspace), policy_for(self.workspace)
        self.assertEqual(profile(first), profile(second))
        self.assertEqual(first, second)  # also the probe cache key

    def test_disabled_sandbox_passes_argv_through_and_says_so(self):
        sandbox = Sandbox(SandboxSettings(mode="off"))
        self.assertFalse(sandbox.enabled)
        argv, record = asyncio.run(sandbox.wrap(["/bin/echo", "hi"], self.workspace))
        self.assertEqual(argv, ["/bin/echo", "hi"])
        self.assertEqual(record, {"backend": "none", "network_allowed": True, "denied_reads": [], "unsandboxed": True})


@unittest.skipUnless(DARWIN, "macOS seatbelt sandbox")
class SeatbeltTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chagent-sb-")
        self.base = Path(self.temporary.name).resolve()
        self.workspace = self.base / "ws"
        self.workspace.mkdir()
        self.sandbox = Sandbox(SandboxSettings(mode="workspace-write", deny_read=()))
        self.policy = self.sandbox.policy(self.workspace)
        self.context = ToolContext(self.workspace)

    def tearDown(self):
        self.temporary.cleanup()

    async def run_in_sandbox(self, argv):
        wrapped = self.sandbox.backend.wrap(argv, self.policy)
        from chagent.tools.sandbox import _run
        return await _run(wrapped)

    async def test_probe_reports_every_check(self):
        support = await self.sandbox.support(self.policy)
        self.assertIsInstance(support, SandboxSupport)
        self.assertTrue(support.available, support.detail)
        self.assertTrue(all(support.checks.values()), support.checks)
        self.assertEqual(set(support.checks), {
            "starts", "workspace_writable", "outside_denied", "read_denied", "no_residue", "network_denied",
        })

    async def test_writes_land_in_the_workspace_and_nowhere_else(self):
        code, _ = await self.run_in_sandbox(["/bin/sh", "-c", f"printf ok > {self.workspace}/inside"])
        self.assertEqual(code, 0)
        self.assertTrue((self.workspace / "inside").exists())
        code, _ = await self.run_in_sandbox(["/bin/sh", "-c", f"printf no > {OUTSIDE}"])
        self.assertNotEqual(code, 0)
        self.assertFalse(OUTSIDE.exists())

    async def test_the_private_temp_dir_is_the_only_extra_writable_area(self):
        tempdir = Path(tempfile.gettempdir()).resolve()
        code, _ = await self.run_in_sandbox(["/bin/sh", "-c", f"printf ok > {tempdir}/chagent-sb-tmp"])
        self.assertEqual(code, 0)
        (tempdir / "chagent-sb-tmp").unlink()
        contained = SandboxPolicy(self.workspace, writable_tmp=False, deny_read=())
        code, _ = await self.run_in_sandbox_with(["/bin/sh", "-c", f"printf no > {tempdir}/chagent-sb-tmp"], contained)
        self.assertNotEqual(code, 0)
        self.assertFalse((tempdir / "chagent-sb-tmp").exists())

    async def test_network_is_refused_against_a_local_listener(self):
        self.assertTrue(await Sandbox._network_denied(self.sandbox.backend, self.policy))
        allowed = SandboxPolicy(self.workspace, network=True, deny_read=(), writable_tmp=False)
        self.assertFalse(await Sandbox._network_denied(self.sandbox.backend, allowed))

    async def test_configured_paths_are_unreadable(self):
        secret = self.base / "secret"
        secret.write_text("leak\n", encoding="utf-8")
        denied = SandboxPolicy(self.workspace, deny_read=(secret,), writable_tmp=False)
        code, _ = await self.run_in_sandbox_with(["/bin/cat", str(secret)], denied)
        self.assertNotEqual(code, 0)
        code, _ = await self.run_in_sandbox_with(["/bin/cat", str(secret)])
        self.assertEqual(code, 0)  # proves the failure above came from the policy

    async def run_in_sandbox_with(self, argv, policy=None):
        from chagent.tools.sandbox import _run
        return await _run(self.sandbox.backend.wrap(argv, policy or self.policy))

    async def test_descendants_inherit_the_profile(self):
        script = f"sh -c 'sh -c \"printf no > {OUTSIDE}\"'"
        code, _ = await self.run_in_sandbox(["/bin/sh", "-c", script])
        self.assertNotEqual(code, 0)
        self.assertFalse(OUTSIDE.exists())

    async def test_unavailable_backend_refuses_instead_of_degrading(self):
        missing = Sandbox(SandboxSettings(mode="workspace-write"), SeatbeltBackend("/nonexistent/sandbox-exec"))
        with self.assertRaises(PermissionError):
            await missing.wrap(["/bin/echo", "hi"], self.workspace)
        audit = await missing.audit(self.workspace)
        self.assertFalse(audit["available"])
        self.assertFalse(audit["unsandboxed"])  # never claims containment either way


@unittest.skipUnless(DARWIN, "macOS seatbelt sandbox")
class SandboxedToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chagent-sb-")
        self.workspace = Path(self.temporary.name).resolve()
        self.context = ToolContext(self.workspace)
        self.sandbox = Sandbox(SandboxSettings(mode="workspace-write", deny_read=()))

    def tearDown(self):
        self.temporary.cleanup()

    async def test_command_reports_its_sandbox_and_confines_writes(self):
        from chagent.tools.command import ExecCommandTool
        tool = ExecCommandTool(self.sandbox)
        self.assertIn("sandbox", tool.description)
        allowed = await tool.execute("c1", {"cmd": "printf ok > inside"}, context=self.context)
        self.assertFalse(allowed.is_error)
        self.assertTrue((self.workspace / "inside").exists())
        self.assertFalse(allowed.details["sandbox"]["unsandboxed"])
        denied = await tool.execute("c2", {"cmd": f"printf no > {Path.home()}/chagent-should-not-exist"}, context=self.context)
        self.assertTrue(denied.is_error)
        self.assertFalse((Path.home() / "chagent-should-not-exist").exists())

    async def test_run_tests_inside_the_profile_still_works(self):
        (self.workspace / "tests").mkdir()
        (self.workspace / "tests" / "test_ok.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n", encoding="utf-8")
        from chagent.tools.tests_runner import RunTestsTool
        tool = RunTestsTool(self.sandbox)
        result = await tool.execute("c1", {"suite": "unittest", "timeout_ms": 60_000}, context=self.context)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(result.details["counts"]["passed"], 1)
        self.assertFalse(result.details["sandbox"]["unsandboxed"])

    async def test_disabled_sandbox_keeps_the_old_behaviour(self):
        from chagent.tools.command import ExecCommandTool
        tool = ExecCommandTool(Sandbox(SandboxSettings(mode="off")))
        self.assertIn("not sandboxed", tool.description)
        result = await tool.execute("c1", {"cmd": "printf hi"}, context=self.context)
        self.assertFalse(result.is_error)
        self.assertEqual(result.details["sandbox"]["backend"], "none")
        self.assertTrue(result.details["sandbox"]["unsandboxed"])


if __name__ == "__main__":
    unittest.main()
