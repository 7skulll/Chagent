import io
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import httpx

from chagent.hostexec.service import create_service, host_directory
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.tools.host import HostExecTool
from chagent.tools.context import ToolContext


class Terminal(io.StringIO):
    def isatty(self):
        return True


class HostExecTests(unittest.IsolatedAsyncioTestCase):
    async def test_denial_and_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "project").mkdir()
            output = root / "project" / "result.txt"
            token = "x" * 40
            reader, writer = Terminal("no\n"), Terminal()
            app = create_service(host_root=root, container_root=Path("/workspaces"),
                                 token=token, input_stream=reader, output_stream=writer,
                                 audit_path=root / "audit.jsonl")
            payload = {"workspace": "/workspaces/project", "command": "echo ready > result.txt",
                       "purpose": "test", "run_id": "run", "call_id": "call"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                denied = await client.post("/execute", json=payload,
                                           headers={"Authorization": "Bearer " + token})
                self.assertEqual(denied.json()["status"], "denied")
                self.assertFalse(output.exists())
                duplicate = await client.post("/execute", json=payload,
                                              headers={"Authorization": "Bearer " + token})
                self.assertEqual(duplicate.status_code, 409)
                unauthorized = await client.post("/execute", json=payload)
                self.assertEqual(unauthorized.status_code, 401)
                # The request ID is unpredictable; this terminal only says yes
                # after showing that exact ID to its human-facing writer.
                class ApprovingTerminal(Terminal):
                    def readline(self):
                        return writer.getvalue().split("[Chagent host request ")[-1].split("]")[0] + "\n"
                app = create_service(host_root=root, container_root=Path("/workspaces"),
                                     token=token, input_stream=ApprovingTerminal(), output_stream=writer,
                                     audit_path=root / "audit.jsonl")
                payload["call_id"] = "new-call"
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as approved_client:
                    approved = await approved_client.post("/execute", json=payload,
                        headers={"Authorization": "Bearer " + token})
                self.assertEqual(approved.json()["exit_code"], 0)
                self.assertEqual(output.read_text().strip(), "ready")

    async def test_workspace_switch_is_disabled_until_identity_is_fixed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "new-workspace"
            candidate.mkdir()
            token = "x" * 40
            app = create_service(host_root=root, container_root=Path("/workspaces"),
                                 token=token, input_stream=Terminal("no\n"), output_stream=Terminal(),
                                 audit_path=root / "audit.jsonl")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                denied = await client.post("/workspace/switch", json={"path": str(candidate)},
                                           headers={"Authorization": "Bearer " + token})
                self.assertEqual(denied.status_code, 503)

    def test_host_rechecks_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "project").mkdir()
            self.assertEqual(host_directory(root, Path("/workspaces"), "/workspaces", "."), root.resolve())
            self.assertEqual(host_directory(root, Path("/workspaces"), "/workspaces/project", "."), (root / "project").resolve())
            with self.assertRaises(ValueError):
                host_directory(root, Path("/workspaces"), "/etc", ".")

    def test_host_tool_is_explicitly_registered_only_in_container(self):
        endpoint = ("http://host.docker.internal:8766", "x" * 40)
        sandbox = Sandbox(SandboxSettings(containerized=True))
        registry = create_builtin_registry(mode="work", allow_host_commands=True,
            unified=True, containerized=True, sandbox=sandbox, host_exec=endpoint)
        self.assertIsNotNone(registry.get("host_exec"))
        without = create_builtin_registry(mode="work", allow_host_commands=True,
            unified=True, containerized=True, sandbox=sandbox)
        self.assertIsNone(without.get("host_exec"))

    async def test_host_tool_requires_prior_container_verification(self):
        class Cursor:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def execute(self, *args): pass
            async def fetchone(self): return None
        class Connection:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def cursor(self): return Cursor()
        class Database:
            def connection(self): return Connection()
        with tempfile.TemporaryDirectory() as temporary:
            tool = HostExecTool("http://host.docker.internal:8766", "x" * 40, Database())
            result = await tool.execute("call", {"command": "python3 --version", "purpose": "test"},
                context=ToolContext(workspace=Path(temporary), run_id=uuid4()))
            self.assertTrue(result.is_error)
            self.assertIn("container command", result.content[0].text)
