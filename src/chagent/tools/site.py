"""A website service owned by one workspace container across conversation runs."""

import asyncio
import json
from time import monotonic

import httpx

from chagent.domain.messages import TextContent
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.tools.context import ToolContext
from chagent.tools.sandbox import Sandbox
from chagent.tools.workspace import WorkspaceFS


class SiteTool(AgentTool):
    def __init__(self, sandbox: Sandbox, database):
        if not sandbox.settings.host_controller:
            raise ValueError("SiteTool requires the host controller")
        self.sandbox, self.database = sandbox, database
        super().__init__(name="site", label="Manage local website",
            description="Start, inspect, stop or HTTP-check a website in this workspace's Docker container. "
                        "Start persists across agent runs; supply the command and its actual listen port. "
                        "The returned 127.0.0.1 URL works in the user's browser. Inspect status and HTTP response before claiming success.",
            parameters={"type": "object", "properties": {
                "action": {"type": "string", "enum": ["start", "status", "stop", "probe", "logs"]},
                "command": {"type": "string", "minLength": 1, "maxLength": 32768},
                "workdir": {"type": "string", "minLength": 1},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "path": {"type": "string", "maxLength": 1000}},
                "required": ["action"], "additionalProperties": False},
            requires_workspace=True, requires_host_execution=True)

    def execution_timeout(self, params):
        return 60.0

    async def _state(self, name: str):
        code, output = await self.sandbox._docker("exec", name, "cat", "/cache/site/chagent-site.json")
        if code:
            return None
        try:
            state = json.loads(output)
        except ValueError:
            return None
        pid = str(state.get("worker_pid", ""))
        if state.get("state") in {"running", "starting"} and pid.isdigit():
            alive, observed = await self.sandbox._docker("exec", name, "/bin/sh", "-c",
                'cat "/proc/$1/stat"', "sh", pid)
            try:
                start = observed.rsplit(") ", 1)[1].split()[19] if alive == 0 else None
            except (IndexError, ValueError):
                start = None
            if start != state.get("worker_start"):
                state["state"] = "stopped"
        return state

    async def _port(self, name: str) -> int:
        code, output = await self.sandbox._docker("port", name, "18080/tcp")
        if code:
            raise RuntimeError("Workspace container has no host loopback port")
        return int(output.strip().rsplit(":", 1)[1])

    async def _save(self, workspace, name, command, internal_port, host_port, status):
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT id FROM workspaces WHERE root_path=%s", (str(workspace),))
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("Workspace identity is missing")
            await connection.execute(
                "INSERT INTO workspace_services (workspace_id, container_name, command, internal_port, host_port, status) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (workspace_id) DO UPDATE SET "
                "container_name=EXCLUDED.container_name, command=EXCLUDED.command, "
                "internal_port=EXCLUDED.internal_port, host_port=EXCLUDED.host_port, "
                "status=EXCLUDED.status, updated_at=now()",
                (row["id"], name, command, internal_port, host_port, status))

    async def _reconcile(self, workspace, name, host_port, current):
        if current is not None:
            await self._save(workspace, name, current.get("command", ""),
                             current.get("actual_port", 1), host_port, current.get("state", "unknown"))
            return
        async with self.database.connection() as connection:
            await connection.execute(
                "UPDATE workspace_services SET status='unknown', updated_at=now() "
                "WHERE workspace_id=(SELECT id FROM workspaces WHERE root_path=%s) AND status <> 'stopped'",
                (str(workspace),))

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context: ToolContext | None = None):
        if context is None:
            raise PermissionError("Website operation needs a workspace")
        fs = WorkspaceFS(context.workspace, expected_identity=context.workspace_identity)
        action = params["action"]
        if action == "status":
            # A read-only status request must not restart a stopped container.
            candidate = self.sandbox.container_name(fs.root)
            code, output = await self.sandbox._docker(
                "inspect", candidate, "--format", "{{.State.Running}}")
            if code or output.strip() != "true":
                async with self.database.connection() as connection:
                    await connection.execute(
                        "UPDATE workspace_services SET status='stopped', updated_at=now() "
                        "WHERE workspace_id=(SELECT id FROM workspaces WHERE root_path=%s)",
                        (str(fs.root),))
                state = {"state": "stopped", "reason": "container_missing" if code else "container_stopped"}
                return AgentToolResult(content=[TextContent(json.dumps({"url": None, "service": state}))],
                                       details={"url": None, "service": state})
        name = await self.sandbox.ensure_container(fs.root)
        current = await self._state(name)
        host_port = await self._port(name)
        url = f"http://127.0.0.1:{host_port}"
        if action == "status":
            await self._reconcile(fs.root, name, host_port, current)
            return AgentToolResult(content=[TextContent(json.dumps({"url": url, "service": current}, ensure_ascii=False))],
                                   details={"url": url, "service": current,
                                            "log": f"docker:{name}:/cache/site/chagent-site.log"})
        if action == "logs":
            code, output = await self.sandbox._docker("exec", name, "tail", "-c", "4000", "/cache/site/chagent-site.log")
            return AgentToolResult(content=[TextContent(output[-4000:] if code == 0 else "No site log available")],
                                   details={"log": f"docker:{name}:/cache/site/chagent-site.log", "output": output[-4000:]},
                                   is_error=code != 0)
        if action == "stop":
            if current and current.get("state") == "running":
                await self.sandbox._docker("exec", name, "/bin/sh", "-c", 'kill -TERM "$1"', "sh", str(current["worker_pid"]))
            await self._save(fs.root, name, (current or {}).get("command", ""),
                             (current or {}).get("actual_port", 1), host_port, "stopped")
            return AgentToolResult(content=[TextContent(f"Website stop requested. {url}")],
                                   details={"url": url, "status": "stopped"})
        if action == "start":
            command = params.get("command")
            port = params.get("port")
            if not command or not isinstance(port, int) or port == 18080:
                raise ValueError("site(start) requires command and a listen port other than 18080")
            if current and current.get("state") in {"running", "starting"}:
                return AgentToolResult(content=[TextContent("A website is already running; inspect or stop it first.")],
                                       is_error=True, details={"url": url, "service": current})
            parts = fs.parts(params.get("workdir", "."))
            with fs.directory(parts):
                pass
            cwd = fs.root.joinpath(*parts)
            await self.sandbox._docker("exec", name, "rm", "-f", "/cache/site/chagent-site.json")
            code, output = await self.sandbox._docker("exec", "--detach", "--workdir", str(cwd),
                "--env", "PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
                "--env", "HOME=/tmp", "--env", "PYTHONPATH=" + str(fs.root),
                name, "/app/.venv/bin/python", "-m", "chagent.hostexec.site_worker",
                "--port", str(port), "--command", command)
            if code:
                raise RuntimeError("Could not start website: " + output[-400:])
            await self._save(fs.root, name, command, port, host_port, "starting")
            deadline = monotonic() + 30
            while monotonic() < deadline:
                await asyncio.sleep(0.5)
                current = await self._state(name)
                if current and current.get("state") == "running":
                    await self._save(fs.root, name, command, port, host_port, "running")
                    return AgentToolResult(content=[TextContent(f"Website process started. URL: {url}. Call site(probe) to verify HTTP content.")],
                        details={"url": url, "service": current, "container": name,
                                 "log": f"docker:{name}:/cache/site/chagent-site.log"})
                if current and current.get("state") in {"stopped", "failed", "unknown"}:
                    break
            await self._save(fs.root, name, command, port, host_port, "failed")
            return AgentToolResult(content=[TextContent(f"Website did not reach running state. {current}")],
                                   is_error=True, details={"url": url, "service": current})
        if action == "probe":
            await self._reconcile(fs.root, name, host_port, current)
            path = params.get("path", "/")
            if not path.startswith("/") or path.startswith("//"):
                raise ValueError("HTTP path must start with one slash")
            try:
                async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                    response = await client.get(url + path)
                body = response.text[:2000]
                result = {"url": url + path, "status_code": response.status_code,
                          "body": body, "service": current}
                return AgentToolResult(content=[TextContent(json.dumps(result, ensure_ascii=False))],
                    details=result, is_error=response.status_code >= 400)
            except httpx.HTTPError as error:
                return AgentToolResult(content=[TextContent(f"HTTP probe failed: {type(error).__name__}")],
                                       details={"url": url + path, "service": current}, is_error=True)
        raise ValueError("Unknown website action")
