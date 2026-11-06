"""Focused checks for per-run scratch and persistent site identity."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.tools.site import SiteTool
from chagent.tools.context import ToolContext
from chagent.web.workspace_api import workspace_site
from chagent.web.runtime import WebRuntime
from chagent.config import Settings


class ContainerLifecycleTests(TestCase):
    def test_idle_timeout_configuration(self):
        self.assertEqual(Settings.from_env({"CHAGENT_CONTAINER_IDLE_SECONDS": "0"}).container_idle_seconds, 0)
        with self.assertRaisesRegex(ValueError, "CHAGENT_CONTAINER_IDLE_SECONDS"):
            Settings(container_idle_seconds=-1)

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

    def test_site_state_falls_back_to_pre_upgrade_location(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
            tool = SiteTool(sandbox, None)
            payload = '{"state":"running","worker_pid":39,"actual_port":8000}'
            stat = "39 (python) S " + "0 " * 18 + "999 "
            with patch.object(sandbox, "_docker", AsyncMock(side_effect=[
                    (1, "No such file or directory"),   # /cache/site/... is absent
                    (0, payload),                        # the old /tmp file is not
                    (0, stat)])):
                state = await tool._state("chagent-ws-test")
            self.assertEqual(state["state"], "running")
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

    def test_recovery_only_removes_terminal_runs(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
            completed, active, missing = uuid4(), uuid4(), uuid4()

            class Cursor:
                async def fetchall(self):
                    return [{"id": completed}]

            class Connection:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return None

                async def execute(self, statement, values):
                    self_candidates.extend(values[0])
                    return Cursor()

            class Database:
                def connection(self):
                    return Connection()

            self_candidates = []
            listing = "\n".join(f"chagent-run-{owner}" for owner in (completed, active, missing))
            with patch.object(sandbox, "_docker", AsyncMock(side_effect=[
                (0, "chagent-ws-" + "a" * 20), (0, listing), (0, "")])) as docker:
                removed = await sandbox.reap_terminal_scratch(Database())
            self.assertEqual(removed, 1)
            self.assertEqual(set(self_candidates), {completed, active, missing})
            self.assertEqual(docker.await_args_list[-1].args[-1], f"/tmp/chagent-run-{completed}")
        asyncio.run(check())

    def test_site_status_does_not_start_stopped_container(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))

            class Connection:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return None

                async def execute(self, *args):
                    return None

            class Database:
                def connection(self):
                    return Connection()

            with TemporaryDirectory() as directory:
                tool = SiteTool(sandbox, Database())
                with patch.object(sandbox, "_docker", AsyncMock(return_value=(0, "false"))), \
                     patch.object(sandbox, "container_detail", AsyncMock(return_value={"image_stale": False})), \
                     patch.object(sandbox, "ensure_container", AsyncMock()) as ensure:
                    result = await tool.execute("status", {"action": "status"},
                                                context=ToolContext(workspace=Path(directory)))
                self.assertEqual(result.details["service"]["state"], "stopped")
                ensure.assert_not_awaited()
        asyncio.run(check())

    def test_web_site_status_skips_execution_preflight(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))

            class Connection:
                async def __aenter__(self): return self
                async def __aexit__(self, *args): return None
                async def execute(self, *args): return None

            class Database:
                def connection(self): return Connection()

            with TemporaryDirectory() as directory:
                root, workspace_id = Path(directory), uuid4()
                tool = SiteTool(sandbox, Database())
                current = SimpleNamespace(
                    roots={root}, _workspace_ids={root: workspace_id},
                    settings=SimpleNamespace(execution_profile="host-controller"),
                    workspace_id=AsyncMock(), require_programming=AsyncMock(),
                    runner=SimpleNamespace(tools=SimpleNamespace(registry=SimpleNamespace(get=lambda _: tool))))
                with patch.object(sandbox, "_docker", AsyncMock(return_value=(0, "false"))), \
                     patch.object(sandbox, "container_detail", AsyncMock(return_value={"image_stale": False})), \
                     patch.object(sandbox, "ensure_container", AsyncMock()) as ensure:
                    result = await workspace_site(workspace_id, current)
                self.assertEqual(result["service"]["state"], "stopped")
                current.workspace_id.assert_awaited_once_with(root)
                current.require_programming.assert_not_awaited()
                ensure.assert_not_awaited()
        asyncio.run(check())

    def test_active_run_lease_prevents_idle_reaping(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
            with TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                name, workspace_id = sandbox.container_name(root), uuid4()
                stat = root.stat()

                class Cursor:
                    async def fetchall(self):
                        return [{"workspace_id": workspace_id, "container_name": name,
                                 "host_path": str(root), "device": stat.st_dev, "inode": stat.st_ino}]

                class Connection:
                    async def __aenter__(self): return self
                    async def __aexit__(self, *args): return None
                    async def execute(self, *args): return Cursor()

                class Database:
                    def connection(self): return Connection()

                sandbox._run_containers[str(uuid4())] = name
                with patch.object(sandbox, "_docker", AsyncMock()) as docker:
                    self.assertEqual(await sandbox.reap_idle_containers(Database(), 1), 0)
                    sandbox._run_containers.clear()
                    async with sandbox.use(root):
                        self.assertEqual(await sandbox.reap_idle_containers(Database(), 1), 0)
                docker.assert_not_awaited()
        asyncio.run(check())

    def test_command_startup_lease_blocks_gc_during_ensure(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
            with TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                name, workspace_id, owner = sandbox.container_name(root), uuid4(), uuid4()
                stat = root.stat()
                entered, proceed = asyncio.Event(), asyncio.Event()

                async def slow_ensure(_root):
                    entered.set()
                    await proceed.wait()
                    return name

                class Cursor:
                    async def fetchall(self):
                        return [{"workspace_id": workspace_id, "container_name": name,
                                 "host_path": str(root), "device": stat.st_dev, "inode": stat.st_ino}]

                class Connection:
                    async def __aenter__(self): return self
                    async def __aexit__(self, *args): return None
                    async def execute(self, *args): return Cursor()

                class Database:
                    def connection(self): return Connection()

                with patch.object(sandbox, "ensure_container", side_effect=slow_ensure), \
                     patch.object(sandbox, "_docker", AsyncMock(return_value=(0, ""))) as docker:
                    startup = asyncio.create_task(sandbox.wrap(["true"], root, owner=owner))
                    await entered.wait()
                    self.assertEqual(await sandbox.reap_idle_containers(Database(), 1), 0)
                    docker.assert_not_awaited()
                    proceed.set()
                    await startup
                    await sandbox.release(owner)
        asyncio.run(check())

    def test_container_deleted_by_another_controller_is_not_an_error(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
            name = "chagent-ws-" + "a" * 20
            failure = "Could not reap orphan container: "
            with patch.object(sandbox, "_docker", AsyncMock(side_effect=[
                    (1, f"Error: No such container: {name}"), (1, "no such object")])):
                await sandbox._remove_container(name, failure)
            # The other controller is mid-removal: the container survives one look
            # and is gone on the next, which is not a failure of this cycle.
            with patch.object(sandbox, "_docker", AsyncMock(side_effect=[
                    (1, "removal of container is already in progress"),
                    (0, "sha256:abc"), (1, "no such object")])), \
                 patch("chagent.tools.sandbox.asyncio.sleep", AsyncMock()) as sleep:
                await sandbox._remove_container(name, failure)
            sleep.assert_awaited_once()
            # A container that is still there after the wait is a real failure.
            with patch.object(sandbox, "_docker", AsyncMock(side_effect=[
                    (1, "device or resource busy")] + [(0, "sha256:abc")] * 10)), \
                 patch("chagent.tools.sandbox.asyncio.sleep", AsyncMock()):
                with self.assertRaisesRegex(RuntimeError, "Could not reap orphan container"):
                    await sandbox._remove_container(name, failure)
        asyncio.run(check())

    def test_orphan_cycle_continues_after_losing_the_delete_race(self):
        async def check():
            sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))

            def details(path):
                h = hashlib.sha256(os.fsencode(path)).hexdigest()
                return h, json.dumps({
                    "Mounts": [{"Type": "bind", "Source": path, "Destination": path, "RW": True},
                               {"Type": "volume", "Destination": "/cache"}],
                    "Config": {"Labels": {"chagent.workspace": h}}, "State": {"Running": True}})

            first_hash, first = details("/private/tmp/orphan-first")
            second_hash, second = details("/private/tmp/orphan-second")
            names = ["chagent-ws-" + first_hash[:20], "chagent-ws-" + second_hash[:20]]
            inspections = {names[0]: first, names[1]: second}
            gone = set()

            async def docker(*args):
                if args[:2] == ("ps", "-a"):
                    return 0, "\n".join(names)
                if args[0] == "inspect":
                    return (1, "no such object") if args[1] in gone else (0, inspections[args[1]])
                if args[0] == "exec" and args[-2:] == ("-1", "/tmp"):
                    return 0, ""
                if args[0] == "exec":
                    return 1, ""
                if args[0] == "top":
                    return 0, "PID CMD\n1 sleep infinity"
                if args[0] == "rm":
                    gone.add(args[-1])
                    # The first delete loses to a faster controller.
                    return (1, "Error: No such container") if args[-1] == names[0] else (0, "")
                raise AssertionError(args)

            class Cursor:
                async def fetchone(self): return None

            class Connection:
                async def __aenter__(self): return self
                async def __aexit__(self, *args): return None
                async def execute(self, *args): return Cursor()

            class Database:
                def connection(self): return Connection()

            with patch.object(sandbox, "_docker", AsyncMock(side_effect=docker)):
                self.assertEqual(await sandbox.reap_orphan_containers(Database()), 2)
        asyncio.run(check())

    def test_orphans_are_previewed_for_one_gc_interval(self):
        async def check():
            sandbox = SimpleNamespace(
                reap_terminal_scratch=AsyncMock(), reap_idle_containers=AsyncMock(),
                reap_orphan_containers=AsyncMock())
            runtime = object.__new__(WebRuntime)
            runtime.runner = SimpleNamespace(sandbox=sandbox, runs=SimpleNamespace(database=object()))
            runtime.settings = SimpleNamespace(container_idle_seconds=1)
            sleeps = []

            async def advance(seconds):
                sleeps.append(seconds)
                if len(sleeps) == 3:
                    raise asyncio.CancelledError()

            with patch("chagent.web.runtime.asyncio.sleep", side_effect=advance):
                with self.assertRaises(asyncio.CancelledError):
                    await runtime._reap_scratch()
            self.assertEqual(sleeps, [5, 300, 300])
            self.assertEqual(sandbox.reap_terminal_scratch.await_count, 2)
            self.assertEqual(sandbox.reap_orphan_containers.await_count, 1)
        asyncio.run(check())
