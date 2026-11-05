"""Container-side request to a separately confirmed host executor."""

from urllib.parse import urlsplit

import httpx

from chagent.domain.messages import TextContent
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.tools.context import ToolContext
from chagent.tools.workspace import WorkspaceFS


class HostExecTool(AgentTool):
    def __init__(self, endpoint: str, token: str, database=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"host.docker.internal", "127.0.0.1", "localhost"} \
                or parsed.username or parsed.password or parsed.path not in {"", "/"} \
                or parsed.query or parsed.fragment or len(token) < 32:
            raise ValueError("Host execution requires a local HTTP endpoint and a 32-character token")
        self.endpoint, self.token = endpoint.rstrip("/"), token
        self.database = database
        super().__init__(name="host_exec", label="Run on user's machine",
            description="Request one command on the user's machine. The host displays the exact command and directory and requires a separate human confirmation. Use only after inspecting container test results. A refusal means no execution. Report host and container results separately.",
            parameters={"type": "object", "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 32768},
                "workdir": {"type": "string", "minLength": 1},
                "purpose": {"type": "string", "minLength": 1, "maxLength": 1000},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 120000}},
                "required": ["command", "purpose"], "additionalProperties": False},
            requires_workspace=True, requires_host_execution=True)

    def execution_timeout(self, params):
        # Includes time spent waiting for the person at the host terminal.
        return 260.0

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context: ToolContext | None = None):
        if context is None or context.run_id is None:
            raise PermissionError("Host execution requires a run workspace")
        fs = WorkspaceFS(context.workspace, expected_identity=context.workspace_identity)
        parts = fs.parts(params.get("workdir", "."))
        with fs.directory(parts):
            pass
        if self.database is None:
            raise RuntimeError("Host execution requires persisted container verification")
        async with self.database.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT t.name, t.arguments, t.result FROM tool_calls t "
                    "JOIN model_calls m ON m.id = t.model_call_id "
                    "WHERE t.run_id = %s AND t.name IN ('exec_command', 'run_tests') "
                    "AND t.status = 'completed' AND m.iteration < "
                    "(SELECT current.iteration FROM tool_calls own JOIN model_calls current "
                    " ON current.id = own.model_call_id WHERE own.run_id = %s AND own.call_id = %s) "
                    "ORDER BY t.ended_at DESC LIMIT 1",
                    (context.run_id, context.run_id, tool_call_id))
                evidence = await cursor.fetchone()
        if evidence is None:
            return AgentToolResult(content=[TextContent(
                "Run a container command or test successfully, inspect its result in the next model turn, then request host execution.")],
                is_error=True)
        details = evidence["result"].get("details") or {}
        verification = {
            "tool": evidence["name"],
            "command": evidence["arguments"].get("cmd") if evidence["name"] == "exec_command" else str(evidence["arguments"]),
            "exit_code": details.get("exit_code"),
            "output": str(details.get("output", ""))[:2000],
        }
        payload = {"workspace": str(fs.root), "workdir": "/".join(parts) or ".",
                   "command": params["command"], "purpose": params["purpose"],
                   "timeout_ms": params.get("timeout_ms", 30000),
                   "run_id": str(context.run_id), "call_id": tool_call_id,
                   "verification": verification}
        # Never inherit proxy settings or provider credentials into this channel.
        try:
            async with httpx.AsyncClient(timeout=250, trust_env=False) as client:
                response = await client.post(self.endpoint + "/execute", json=payload,
                                             headers={"Authorization": "Bearer " + self.token})
            response.raise_for_status()
            result = response.json()
        except (httpx.TimeoutException, httpx.TransportError) as error:
            return AgentToolResult(content=[TextContent(f"Host result unknown: {error}. Do not retry automatically.")],
                                   is_error=True, outcome_unknown=True)
        except (httpx.HTTPStatusError, ValueError) as error:
            return AgentToolResult(content=[TextContent(f"Host request rejected: {error}")], is_error=True)
        if result.get("status") == "denied":
            return AgentToolResult(content=[TextContent("Host approval denied. Command was not executed.")],
                                   details=result, is_error=True)
        if not result.get("started"):
            return AgentToolResult(content=[TextContent("Host command was not started.")], details=result, is_error=True)
        body = (f"Host approval {result.get('id')}: {result.get('status')}; "
                f"exit {result.get('exit_code')}.\n{result.get('output', '')}")
        return AgentToolResult(content=[TextContent(body)], details=result,
                               is_error=result.get("status") != "completed" or result.get("exit_code") != 0)
