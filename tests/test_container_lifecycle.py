"""Focused checks for per-run scratch and persistent site identity."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.tools.site import SiteTool


class ContainerLifecycleTests(TestCase):
    def test_command_uses_private_scratch_and_release_removes_it(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
            owner = uuid4()
            with TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.object(sandbox, "ensure_container", AsyncMock(return_value="chagent-ws-test")), \
                     patch.object(sandbox, "_docker", AsyncMock(return_value=(0, ""))) as docker:
                    argv, _ = await sandbox.wrap(["true"], root, owner=owner)
                    scratch = f"/tmp/chagent-run-{owner}"
                    self.assertIn("HOME=" + scratch, argv)
                    self.assertIn("TMPDIR=" + scratch, argv)
                    self.assertEqual(argv[-3:], ["chagent-run", scratch, "true"])
                    await sandbox.release(owner)
                    docker.assert_awaited_once_with("exec", "chagent-ws-test", "rm", "-rf", "--", scratch)
        asyncio.run(check())

    def test_site_pid_reuse_is_stopped(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
            tool = SiteTool(sandbox, None)
            payload = '{"state":"running","worker_pid":42,"worker_start":"123"}'
            stat = "42 (python) S " + "0 " * 18 + "999 "
            with patch.object(sandbox, "_docker", AsyncMock(side_effect=[(0, payload), (0, stat)])):
                state = await tool._state("chagent-ws-test")
            self.assertEqual(state["state"], "stopped")
        asyncio.run(check())
