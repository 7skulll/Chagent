"""The per-run network observer: packet parsing, sidecar lifecycle, persistence.

WHY THIS EXISTS
    ``hostexec/network_monitor.py`` reads packet headers inside the workspace
    container's network namespace and ``network_audit.py`` runs it as a sidecar.
    Both are hard to exercise for real in a unit test, so what is checked here is
    the part that can silently be wrong: which packets are counted, what happens
    when the observer cannot start or cannot finish, and whether the observation
    still reaches the run record when the run is cancelled or fails.

WHAT IT IS NOT
    A packet-capture test. Nothing here reads a real interface; the parsing cases
    are hand-built frames, and the sidecar is a stub docker client that answers
    the same call shapes.
"""

import asyncio
import socket
import struct
import unittest
from contextlib import suppress
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from chagent.agent.runner import RunFailedError
from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.hostexec.network_audit import NetworkAudit
from chagent.hostexec.network_monitor import packet_target
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import RunRepository
from chagent.tools.echo import EchoTool
from chagent.tools.executor import ToolExecutor
from chagent.tools.registry import ToolRegistry


def ethernet(kind: int = 0x0800, vlan: int | None = None) -> bytes:
    frame = b"\x00" * 12
    if vlan is not None:
        return frame + b"\x81\x00" + struct.pack("!H", vlan) + struct.pack("!H", kind)
    return frame + struct.pack("!H", kind)


def ipv4(destination: str, protocol: int, payload: int, total: int | None = None) -> bytes:
    header = bytes([0x45, 0]) + struct.pack("!H", total if total is not None else 20 + payload)
    header += b"\x00" * 5 + bytes([protocol]) + b"\x00\x00"
    return header + socket.inet_aton("172.20.0.2") + socket.inet_aton(destination)


def ipv6(destination: str, next_header: int, payload: int) -> bytes:
    header = struct.pack("!IH", 6 << 28, payload) + bytes([next_header, 64])
    return header + socket.inet_pton(socket.AF_INET6, "::1") + socket.inet_pton(socket.AF_INET6, destination)


class PacketParsingTests(unittest.TestCase):
    def test_ipv4_tcp_destination_and_size(self):
        tcp = struct.pack("!HH", 51000, 443) + b"\x00" * 16
        frame = ethernet() + ipv4("93.184.215.14", 6, len(tcp)) + tcp + b"abc"
        # The IP header's total length is the authority: the 3 trailing bytes
        # after it are Ethernet padding and are not counted as payload.
        self.assertEqual(packet_target(frame), ("93.184.215.14", 443, 40))

    def test_vlan_tagged_frame_is_read_after_the_tag(self):
        tcp = struct.pack("!HH", 51000, 8443) + b"\x00" * 16
        frame = ethernet(vlan=7) + ipv4("93.184.215.14", 6, len(tcp)) + tcp
        self.assertEqual(packet_target(frame), ("93.184.215.14", 8443, 40))

    def test_udp_destination_port_is_reported(self):
        udp = struct.pack("!HH", 5353, 53) + b"\x00" * 4
        frame = ethernet() + ipv4("8.8.8.8", 17, len(udp)) + udp
        destination, port, _ = packet_target(frame)
        self.assertEqual((destination, port), ("8.8.8.8", 53))

    def test_a_protocol_without_ports_reports_port_zero(self):
        icmp = b"\x08\x00" + b"\x00" * 6
        frame = ethernet() + ipv4("1.1.1.1", 1, len(icmp)) + icmp
        self.assertEqual(packet_target(frame)[1], 0)

    def test_ipv6_destination_is_parsed(self):
        tcp = struct.pack("!HH", 51000, 443) + b"\x00" * 16
        frame = ethernet(kind=0x86DD) + ipv6("2606:4700::1", 6, len(tcp)) + tcp
        destination, port, _ = packet_target(frame)
        self.assertEqual((destination, port), ("2606:4700::1", 443))

    def test_padding_is_not_counted_but_a_short_capture_under_reports(self):
        # Ethernet padding must not inflate the byte count, so the declared
        # length wins when it is smaller. The reverse case under-reports: a
        # capture buffer that cut the frame short cannot be detected per packet,
        # which is why whole-observation completeness is a separate flag.
        tcp = struct.pack("!HH", 51000, 443) + b"\x00" * 4
        padded = ethernet() + ipv4("93.184.215.14", 6, len(tcp), total=32) + tcp + b"\x00" * 11
        self.assertEqual(packet_target(padded)[2], 32)
        declared_bigger = ethernet() + ipv4("93.184.215.14", 6, len(tcp), total=1500) + tcp
        self.assertEqual(packet_target(declared_bigger)[2], 28)

    def test_non_ip_and_short_packets_are_ignored(self):
        self.assertIsNone(packet_target(b"short"))
        self.assertIsNone(packet_target(b"\x00" * 12 + b"\x08\x06" + b"\x00" * 40))
        self.assertIsNone(packet_target(ethernet() + b"\x45"))


class StubDocker:
    """Answers the sidecar's docker calls; anything unexpected is a failure."""

    def __init__(self, *, run: int = 0, logs: list[str] | None = None, stop: int = 0):
        self.run_code = run
        self.logs = logs if logs is not None else ["READY"]
        self.stop_code = stop
        self.calls: list[tuple] = []

    async def ensure_container(self, workspace):
        self.calls.append(("ensure", str(workspace)))
        return "chagent-ws-stub"

    async def _docker(self, *arguments, timeout=None):
        self.calls.append(arguments)
        if arguments[0] == "run":
            return self.run_code, ""
        if arguments[0] == "logs":
            return 0, "\n".join(self.logs)
        if arguments[0] == "stop":
            return self.stop_code, ""
        if arguments[0] == "rm":
            return 0, ""
        raise AssertionError(f"unexpected docker call {arguments}")

    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]


class NetworkAuditTests(unittest.IsolatedAsyncioTestCase):
    def audit(self, docker) -> NetworkAudit:
        sandbox = SimpleNamespace(ensure_container=docker.ensure_container, _docker=docker._docker)
        return NetworkAudit(sandbox, Path("/workspace"), uuid4())

    async def test_start_waits_for_the_capture_socket(self):
        docker = StubDocker(logs=["booting", "READY"])
        audit = self.audit(docker)
        with mock.patch("chagent.hostexec.network_audit.asyncio.sleep", mock.AsyncMock()):
            self.assertTrue(await audit.start())
        self.assertTrue(audit.ready)
        self.assertIn(("logs", audit.name), docker.calls)
        run = next(call for call in docker.calls if call[0] == "run")
        # Shares the workspace network namespace and carries no workspace mount.
        self.assertIn("container:chagent-ws-stub", run)
        self.assertIn("NET_RAW", run)
        self.assertNotIn("--network=host", run)

    async def test_start_fails_and_cleans_up_when_the_sidecar_cannot_run(self):
        docker = StubDocker(run=125)
        audit = self.audit(docker)
        self.assertFalse(await audit.start())
        self.assertFalse(audit.started)
        self.assertIn("rm", docker.kinds())

    async def test_start_fails_when_ready_never_arrives(self):
        docker = StubDocker(logs=["booting"])
        audit = self.audit(docker)
        with mock.patch("chagent.hostexec.network_audit.asyncio.sleep", mock.AsyncMock()):
            self.assertFalse(await audit.start())
        self.assertFalse(audit.ready)

    async def test_stop_reports_an_incomplete_observation_when_never_started(self):
        docker = StubDocker()
        audit = self.audit(docker)
        result = await audit.stop()
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "monitor_unavailable")
        self.assertIsNone(result["sent_bytes"])
        self.assertEqual(docker.calls, [])

    async def test_stop_reads_the_final_line_and_is_idempotent(self):
        payload = '{"targets": [{"ip": "104.20.23.154", "port": 443, "sent_bytes": 2457}], ' \
                  '"sent_bytes": 2457, "packets": 9, "truncated": false}'
        docker = StubDocker(logs=["READY", payload])
        audit = self.audit(docker)
        with mock.patch("chagent.hostexec.network_audit.asyncio.sleep", mock.AsyncMock()):
            await audit.start()
        first = await audit.stop()
        calls = len(docker.calls)
        second = await audit.stop()
        self.assertEqual(first, second)
        self.assertEqual(len(docker.calls), calls, "a second stop must not touch docker")
        self.assertTrue(first["complete"])
        self.assertEqual(first["sent_bytes"], 2457)
        self.assertEqual(docker.kinds().count("rm"), 1)

    async def test_a_truncated_capture_is_not_reported_as_complete(self):
        payload = '{"targets": [], "sent_bytes": 10, "packets": 1, "truncated": true}'
        docker = StubDocker(logs=["READY", payload])
        audit = self.audit(docker)
        with mock.patch("chagent.hostexec.network_audit.asyncio.sleep", mock.AsyncMock()):
            await audit.start()
        result = await audit.stop()
        self.assertFalse(result["complete"])
        self.assertEqual(result["sent_bytes"], 10)

    async def test_a_docker_failure_at_stop_is_reported_not_raised(self):
        class Broken(StubDocker):
            async def _docker(self, *arguments, timeout=None):
                if arguments[0] == "logs":
                    return 1, ""
                return await super()._docker(*arguments, timeout=timeout)

        docker = Broken()
        audit = self.audit(docker)
        with mock.patch("chagent.hostexec.network_audit.asyncio.sleep", mock.AsyncMock()):
            await audit.start()
        result = await audit.stop()
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "RuntimeError")

    async def test_unreadable_monitor_output_is_reported_not_raised(self):
        docker = StubDocker(logs=["READY", "not json"])
        audit = self.audit(docker)
        with mock.patch("chagent.hostexec.network_audit.asyncio.sleep", mock.AsyncMock()):
            await audit.start()
        result = await audit.stop()
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "JSONDecodeError")


class StubAudit:
    """Stands in for the sidecar so the runner path is testable without docker."""

    instances: list["StubAudit"] = []
    available = True
    observation = {"complete": True, "targets": [{"ip": "104.20.23.154", "port": 443, "sent_bytes": 2457}],
                   "sent_bytes": 2457, "packets": 9}

    def __init__(self, sandbox, workspace, run_id):
        self.finished = None
        self.started = False
        self.stops = 0
        self.sandbox, self.workspace, self.run_id = sandbox, workspace, run_id
        StubAudit.instances.append(self)

    async def start(self) -> bool:
        self.started = self.available
        return self.available

    async def stop(self) -> dict:
        self.stops += 1
        self.finished = (dict(self.observation) if self.started else
                         {"complete": False, "reason": "monitor_unavailable", "targets": [],
                          "sent_bytes": None, "packets": None})
        return self.finished


class HostSandbox:
    """The two things the runner asks a host-controller sandbox for."""

    def __init__(self):
        self.settings = SimpleNamespace(host_controller=True, containerized=True)

    async def audit(self, workspace, owner=None):
        return {"capability": "full", "network": "observed"}

    def release(self, run_id):
        """The runner releases the run's workspace container after cleanup."""


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class NetworkRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "chagent_test_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}",
        )))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.runner = create_demo_runner(self.database)
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.session = await self.runner.conversations.create_session(Path(self.workspace.name))
        StubAudit.instances = []
        StubAudit.available = True
        patcher = mock.patch("chagent.agent.runner.NetworkAudit", StubAudit)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def events(self, run_id):
        return await RunRepository(self.database).events(run_id)

    async def test_a_completed_run_persists_one_observation(self):
        self.runner.sandbox = HostSandbox()
        result = await self.runner.run(self.session, "回显")
        events = await self.events(result.run_id)
        self.assertEqual(sum(event["type"] == "network_observed" for event in events), 1)
        observed = next(event["payload"] for event in events if event["type"] == "network_observed")
        self.assertEqual(observed["sent_bytes"], 2457)
        self.assertEqual(await RunRepository(self.database).network_observation(result.run_id), observed)
        # The observation is written before the terminal row, so a reader of the
        # finished run never sees a half-observed state. It follows the file
        # diff because the diff scans the workspace inside the run's cleanup.
        types = [event["type"] for event in events]
        self.assertLess(types.index("network_observed"), types.index("network_manifest_after")
                        if "network_manifest_after" in types else types.index("workspace_manifest_after"))
        self.assertEqual(types[-1], "run_completed")

    async def test_cancelling_a_run_still_persists_the_observation(self):
        class BlockingEcho(EchoTool):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()

            async def execute(self, *args, **kwargs):
                self.started.set()
                await asyncio.Event().wait()

        tool = BlockingEcho()
        registry = ToolRegistry()
        registry.register(tool)
        self.runner.tools = ToolExecutor(registry)
        self.runner.sandbox = HostSandbox()
        task = asyncio.create_task(self.runner.run(self.session, "等待取消"))

        async def cleanup():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self.addAsyncCleanup(cleanup)
        await asyncio.wait_for(tool.started.wait(), timeout=5)
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT id FROM runs WHERE session_id = %s", (self.session,))
            run_id = (await cursor.fetchone())["id"]
        self.assertTrue(self.runner.cancel(run_id))
        with self.assertRaises(asyncio.CancelledError):
            await task
        observed = [event for event in await self.events(run_id) if event["type"] == "network_observed"]
        self.assertEqual(len(observed), 1, "a cancelled run must still record what left the container")
        self.assertEqual(observed[0]["payload"]["sent_bytes"], 2457)
        self.assertEqual(StubAudit.instances[0].stops, 1)

    async def test_a_missing_observer_refuses_the_run_instead_of_claiming_it_is_observed(self):
        StubAudit.available = False
        self.runner.sandbox = HostSandbox()
        with self.assertRaises(RunFailedError) as failure:
            await self.runner.run(self.session, "回显")
        self.assertEqual(failure.exception.end_reason, "execution_error")
        events = await self.events(failure.exception.run_id)
        started = [event for event in events if event["type"] == "network_monitor_started"]
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0]["payload"]["available"])
        observed = [event for event in events if event["type"] == "network_observed"]
        self.assertEqual(len(observed), 1, "a run refused for lack of an observer still records why")
        self.assertFalse(observed[0]["payload"]["complete"])
        self.assertEqual(observed[0]["payload"]["reason"], "monitor_unavailable")

    async def test_a_failing_run_persists_the_observation_once(self):
        self.runner.sandbox = HostSandbox()

        async def explode(*args, **kwargs):
            raise RuntimeError("model exploded")

        self.runner._loop = explode
        with self.assertRaises(RunFailedError) as failure:
            await self.runner.run(self.session, "回显")
        events = await self.events(failure.exception.run_id)
        self.assertEqual(sum(event["type"] == "network_observed" for event in events), 1)
        self.assertEqual(StubAudit.instances[0].stops, 1)

    async def test_a_run_without_a_host_sandbox_does_not_start_an_observer(self):
        result = await self.runner.run(self.session, "回显")
        self.assertEqual(StubAudit.instances, [])
        self.assertIsNone(await RunRepository(self.database).network_observation(result.run_id))


if __name__ == "__main__":
    unittest.main()
