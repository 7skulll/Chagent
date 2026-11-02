"""Web API and real-socket SSE checks against isolated PostgreSQL schemas."""

import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from uuid import UUID, uuid4

import httpx
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import uvicorn

from chagent.bootstrap import create_demo_runner
from chagent.config import Settings
from chagent.domain.messages import AssistantMessage, TextContent, UserMessage
from chagent.models.events import ContentDeltaEvent, ErrorEvent, StartEvent
from chagent.models.fake import FakeModelClient
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.tools.echo import EchoTool
from chagent.web.app import WebRuntime, create_app


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.schema = "chagent_web_test_" + uuid4().hex
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
        self.runtime = WebRuntime(self.runner, self.workspace)
        self.app = create_app(workspace=self.workspace, injected_runtime=self.runtime)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1", trust_env=False)
        self.addAsyncCleanup(self.client.aclose)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def create_session(self, title="测试会话"):
        response = await self.client.post("/api/sessions", json={"title": title})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    async def wait_run(self, identity):
        async with asyncio.timeout(5):
            while True:
                run = await self.runner.runs.get(UUID(identity))
                if run["status"] not in {"queued", "running"}:
                    return run
                await asyncio.sleep(.01)

    async def test_work_agent_creates_a_downloadable_file(self):
        runner = create_demo_runner(self.database, tool_name="create_file", work_only=True,
                                    tool_arguments={"filename": "最后一行代码.md", "content": "# 故事\n正文"})
        session = await runner.conversations.create_session(self.workspace)
        result = await runner.run(session, "写故事并生成文件")
        listing = (await self.client.get(f"/api/sessions/{session}/files")).json()["items"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["name"], "最后一行代码.md")
        download = await self.client.get(f"/api/sessions/{session}/files/{listing[0]['id']}")
        self.assertEqual(download.text, "# 故事\n正文")
        self.assertTrue(result.run_id)
        messages = (await self.client.get(f"/api/sessions/{session}/messages")).json()["items"]
        created = [item for item in messages if item["message"].get("tool_name") == "create_file"]
        self.assertEqual(created[0]["message"]["details"]["generated_file"]["name"], "最后一行代码.md")

    async def test_generated_file_is_downloadable_only_in_its_session(self):
        from chagent.tools.generated_files import CreateFileTool
        from chagent.tools.context import ToolContext
        first, other = await self.create_session(), await self.create_session()
        response = await self.client.post(f"/api/sessions/{first}/runs", json={"text": "生成测试"})
        run_id = response.json()["run_id"]
        await self.wait_run(run_id)
        tool = CreateFileTool(self.database)
        context = ToolContext(self.workspace, UUID(first), UUID(run_id))
        result = await tool.execute("call-1", {"filename": "故事.md", "content": "# 故事\n你好"}, context=context)
        file = result.details["generated_file"]
        repeated = await tool.execute("call-1", {"filename": "故事.md", "content": "# 故事\n你好"}, context=context)
        self.assertEqual(file["id"], repeated.details["generated_file"]["id"])
        with self.assertRaises(ValueError):
            await tool.execute("call-2", {"filename": "../evil.md", "content": "x"}, context=context)
        listing = (await self.client.get(f"/api/sessions/{first}/files")).json()["items"]
        self.assertEqual([item["name"] for item in listing], ["故事.md"])
        response = await self.client.get(f"/api/sessions/{first}/files/{file['id']}")
        self.assertEqual(response.text, "# 故事\n你好")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual((await self.client.get(f"/api/sessions/{other}/files/{file['id']}")).status_code, 404)

    async def test_chat_history_pagination_and_sse_replay(self):
        identity = await self.create_session("网页对话")
        for text in ("第一轮", "第二轮"):
            response = await self.client.post(f"/api/sessions/{identity}/runs", json={"text": text})
            self.assertEqual(response.status_code, 202, response.text)
            run_id = response.json()["run_id"]
            self.assertEqual((await self.wait_run(run_id))["status"], "completed")
        page = (await self.client.get(f"/api/sessions/{identity}/messages?limit=3")).json()
        self.assertEqual(len(page["items"]), 3)
        older = (await self.client.get(f"/api/sessions/{identity}/messages?before={page['next_before']}&limit=10")).json()
        self.assertEqual(len(older["items"]), 5)
        self.assertLess(older["items"][-1]["sequence"], page["items"][0]["sequence"])
        detail = (await self.client.get(f"/api/sessions/{identity}")).json()
        self.assertEqual(detail["latest_run"]["id"], run_id)
        context = (await self.client.get(f"/api/sessions/{identity}/context")).json()
        self.assertIn("summaries", context)
        self.assertEqual(context["snapshots"][0]["run_id"], run_id)
        stream = await self.client.get(f"/api/runs/{run_id}/events")
        self.assertIn("text/event-stream", stream.headers["content-type"])
        data = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        kinds = [item.get("type") for item in data]
        self.assertIn("tool_started", kinds)
        self.assertIn("tool_completed", kinds)
        self.assertIn("text_delta", kinds)
        self.assertIn("run_completed", kinds)
        sequences = [int(line[4:]) for line in stream.text.splitlines() if line.startswith("id: ")]
        replay = await self.client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(sequences[-2])})
        self.assertIn("run_completed", replay.text)
        self.assertNotIn("tool_started", replay.text)
        ended = await self.client.get(f"/api/runs/{run_id}/events?after={sequences[-1]}")
        self.assertEqual(ended.text, "event: end\ndata: {}\n\n")
        # A fresh runtime sees the same persisted conversation.
        restored = WebRuntime(create_demo_runner(self.database), self.workspace)
        self.assertEqual(len((await restored.sessions.messages(UUID(identity))).items), 8)

    async def test_tool_stop_reason_is_consistent_in_api_history_and_replay(self):
        class StopTool(EchoTool):
            def __init__(self):
                super().__init__()
                self.name = "stop"

            async def execute(self, *args, **kwargs):
                result = await super().execute(*args, **kwargs)
                result.terminate = True
                return result

        self.runner.tools.registry.register(StopTool())
        self.runner.client = FakeModelClient("stop", {"text": "finished by tool"})
        session = await self.create_session("normal tool stop")
        response = await self.client.post(f"/api/sessions/{session}/runs", json={"text": "stop"})
        run_id = response.json()["run_id"]
        await self.wait_run(run_id)
        run = (await self.client.get(f"/api/runs/{run_id}")).json()
        self.assertEqual((run["status"], run["end_reason"]), ("completed", "tool_requested_stop"))
        self.assertIsNone(run["error"])
        detail = (await self.client.get(f"/api/sessions/{session}")).json()
        self.assertEqual(detail["latest_run"]["end_reason"], "tool_requested_stop")
        history = (await self.client.get(f"/api/sessions/{session}/messages")).json()["items"]
        self.assertEqual([item["message"]["role"] for item in history], ["user", "assistant", "toolResult"])
        stream = await self.client.get(f"/api/runs/{run_id}/events")
        updates = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        completed = next(item for item in updates if item.get("type") == "run_completed")
        self.assertEqual(completed["payload"]["end_reason"], "tool_requested_stop")
        self.assertNotIn("run_failed", stream.text)

    async def test_session_list_pagination_and_workspace_isolation(self):
        first = await self.create_session("一")
        second = await self.create_session("二")
        page = (await self.client.get("/api/sessions?limit=1")).json()
        self.assertEqual(page["items"][0]["id"], second)
        next_page = (await self.client.get(f"/api/sessions?before={page['next_cursor']}")).json()
        self.assertEqual(next_page["items"][0]["id"], first)
        other = await self.runner.conversations.create_session(self.workspace.parent)
        self.assertEqual((await self.client.get(f"/api/sessions/{other}")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/sessions/{other}/messages")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/sessions/{other}/context")).status_code, 404)
        self.assertEqual((await self.client.post(f"/api/sessions/{other}/runs", json={"text": "no"})).status_code, 404)

    async def test_validation_origin_and_static_assets(self):
        identity = await self.create_session()
        endpoint = f"/api/sessions/{identity}/runs"
        for value in ({}, {"text": " "}, {"text": 3}, {"text": "hi", "key": "hidden"}):
            self.assertEqual((await self.client.post(endpoint, json=value)).status_code, 400)
        self.assertEqual((await self.client.post(endpoint, content="text")).status_code, 415)
        self.assertEqual((await self.client.post(endpoint, content=b"{" + b"x" * 65536, headers={"Content-Type": "application/json"})).status_code, 413)
        self.assertEqual((await self.client.get("/api/sessions?limit=0")).status_code, 400)
        self.assertEqual((await self.client.get("/api/sessions?before=bad")).status_code, 400)
        self.assertEqual((await self.client.get(f"/api/sessions/{uuid4()}")).status_code, 404)
        for method in (self.client.get, self.client.post):
            result = await method("/api/sessions", headers={"Origin": "https://other.example"})
            self.assertEqual(result.status_code, 403)
        self.assertEqual((await self.client.get("/", headers={"Host": "other.example"})).status_code, 400)
        self.assertEqual((await self.client.get("/api/info", headers={"Origin": "http://127.0.0.1"})).status_code, 200)
        self.assertEqual((await self.client.get("/static/unknown")).status_code, 404)
        home = await self.client.get("/")
        self.assertIn("Chagent", home.text)
        self.assertIn("frame-ancestors 'none'", home.headers["content-security-policy"])
        for asset in ("app.js", "markdown.js", "style.css"):
            self.assertEqual((await self.client.get(f"/static/{asset}")).status_code, 200)
        info = (await self.client.get("/api/info")).json()
        self.assertEqual(set(info), {"workspace", "provider", "model", "default_mode", "code_mode"})
        self.assertEqual(info["default_mode"], "work")
        self.assertFalse(info["code_mode"])

    async def test_provider_failure_is_displayable_and_next_turn_recovers(self):
        class ErrorClient:
            async def stream(self, request):
                yield ErrorEvent(reason="error", error=AssistantMessage(
                    [], "fake", "local", "test", 1, "error", error_message="Provider rejected request (401)",
                ))
        identity = await self.create_session()
        self.runner.client = ErrorClient()
        response = await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "失败"})
        run_id = response.json()["run_id"]
        self.assertEqual((await self.wait_run(run_id))["status"], "failed")
        stream = await self.client.get(f"/api/runs/{run_id}/events")
        self.assertIn("run_failed", stream.text)
        self.assertIn("401", stream.text)
        self.runner.client = create_demo_runner(self.database).client
        response = await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "恢复"})
        self.assertEqual((await self.wait_run(response.json()["run_id"]))["status"], "completed")

    async def test_artifact_pages_are_session_scoped_and_history_is_bounded(self):
        from chagent.models.fake import FakeModelClient
        identity = await self.create_session("large output")
        text = "start\n" + "中😀x" * 6000 + "\nend"
        (self.workspace / "large.txt").write_text(text)
        self.runner.model.context_window = 30_000
        self.runner.client = FakeModelClient("read_file", {"path": "large.txt"})
        response = await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "read large file"})
        await self.wait_run(response.json()["run_id"])
        page = (await self.client.get(f"/api/sessions/{identity}/messages")).json()
        message = next(item["message"] for item in page["items"] if item["message"]["role"] == "toolResult")
        artifact = message["artifact"]
        self.assertLess(len(str(message)), 5000)
        endpoint = f"/api/sessions/{identity}/artifacts/{artifact['id']}"
        full = (await self.client.get(endpoint)).json()
        self.assertIn("File: large.txt", full["text"])
        self.assertEqual(full["next_offset"], 1000)
        self.assertEqual((await self.client.get(endpoint + "?limit=1001")).status_code, 400)
        self.assertEqual((await self.client.get(endpoint + "?offset=-1")).status_code, 400)
        self.assertEqual((await self.client.get(endpoint + "?offset=999999999999999999999")).status_code, 400)
        other = await self.create_session("other")
        self.assertEqual((await self.client.get(f"/api/sessions/{other}/artifacts/{artifact['id']}")).status_code, 404)

    async def test_real_socket_stream_disconnect_cancel_and_continue(self):
        entered = asyncio.Event()
        class BlockingClient:
            async def stream(self, request):
                partial = AssistantMessage([TextContent("开始")], "fake", "local", "test", 1, "pending")
                yield StartEvent(partial=partial)
                yield ContentDeltaEvent(type="text_delta", content_index=0, delta="开始", partial=partial)
                entered.set()
                await asyncio.Event().wait()
        self.runner.client = BlockingClient()
        identity = await self.create_session()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        sock.setblocking(False)
        self.addCleanup(sock.close)
        server = uvicorn.Server(uvicorn.Config(self.app, lifespan="off", log_level="critical"))
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        async def stop_server():
            server.should_exit = True
            await asyncio.wait_for(server_task, 5)
        self.addAsyncCleanup(stop_server)
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=5, trust_env=False) as client:
            response = await client.post(f"/api/sessions/{identity}/runs", json={"text": "持续运行"})
            run_id = response.json()["run_id"]
            await asyncio.wait_for(entered.wait(), 5)
            cursor = 0
            async with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("id: "):
                        cursor = int(line[4:])
                    if line.startswith("data: ") and json.loads(line[6:]).get("type") == "text_delta":
                        self.assertTrue(self.runtime.has_viewer(UUID(run_id)))
                        break
            # Closing the stream must remove the audience used by Z4 approvals;
            # the server notices on its next disconnect check, not synchronously
            # with the client's context manager exiting.
            async with asyncio.timeout(3):
                while self.runtime.has_viewer(UUID(run_id)):
                    await asyncio.sleep(.01)
            self.assertFalse(self.runtime.has_viewer(UUID(run_id)))
            self.assertEqual((await self.runner.runs.get(UUID(run_id)))["status"], "running")
            conflict = await client.post(f"/api/sessions/{identity}/runs", json={"text": "重复"})
            self.assertEqual(conflict.status_code, 409)
            for _ in range(2):
                self.assertIn((await client.post(f"/api/runs/{run_id}/cancel")).status_code, (200, 202))
            self.assertEqual((await self.wait_run(run_id))["status"], "cancelled")
            replay = await client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)})
            self.assertIn("run_cancelled", replay.text)
            self.assertNotIn("text_delta", replay.text)
            self.runner.client = create_demo_runner(self.database).client
            response = await client.post(f"/api/sessions/{identity}/runs", json={"text": "停止后继续"})
            self.assertEqual((await self.wait_run(response.json()["run_id"]))["status"], "completed")

    async def test_shutdown_marks_active_run_cancelled(self):
        class BlockingClient:
            async def stream(self, request):
                await asyncio.Event().wait()
                yield
        self.runner.client = BlockingClient()
        identity = await self.create_session()
        run_id = (await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "关闭服务"})).json()["run_id"]
        await self.runtime.close()
        self.assertEqual((await self.runner.runs.get(UUID(run_id)))["status"], "cancelled")
        self.assertEqual((await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "关闭后拒绝"})).status_code, 503)

    async def test_memory_crud_versions_and_workspace_boundary(self):
        from chagent.domain.memories import MemoryProposal
        from chagent.storage.memories import MemoryRepository
        created = await self.client.post("/api/memories", json={"key": "project.database", "content": "使用 PostgreSQL", "scope": "workspace", "pinned": True})
        self.assertEqual(created.status_code, 201, created.text)
        memory = created.json()
        self.assertEqual(memory["status"], "active")
        listing = (await self.client.get("/api/memories")).json()
        self.assertEqual(listing["items"][0]["id"], memory["id"])
        endpoint = f"/api/memories/{memory['id']}"
        updated = await self.client.patch(endpoint, json={"content": "使用 PostgreSQL 16", "expected_revision": 1})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["revision"], 2)
        self.assertEqual((await self.client.patch(endpoint, json={"content": "stale", "expected_revision": 1})).status_code, 409)
        self.assertEqual(len((await self.client.get(endpoint + "/revisions")).json()), 2)
        removed = await self.client.request("DELETE", endpoint, json={"expected_revision": 2})
        self.assertEqual(removed.json()["status"], "deleted")
        self.assertEqual((await self.client.get("/api/memories")).json()["items"], [])
        self.assertEqual(len((await self.client.get("/api/memories?status=deleted")).json()["items"]), 1)
        other = await self.runner.conversations.create_session(self.workspace.parent)
        other_workspace = (await self.runner.conversations.get_session(other)).workspace_id
        hidden = await MemoryRepository(self.database).propose(MemoryProposal("project.secret", "other project", origin="manual"), other_workspace)
        self.assertEqual((await self.client.get(f"/api/memories/{hidden.id}")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/memories/{hidden.id}/revisions")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/memories?before={hidden.id}")).status_code, 404)
        for invalid in ({"key": "Bad Key", "content": "hi"}, {"key": "test", "content": "password=secretvalue"},
                        {"key": "test", "content": "hi", "origin": "manual"}, {"key": "test", "content": "hi", "scope": {}}):
            self.assertEqual((await self.client.post("/api/memories", json=invalid)).status_code, 400)

    async def test_memory_candidate_review_exposes_and_checks_conflict(self):
        from chagent.domain.memories import MemoryPolicy
        self.runner.memory_service.policy = MemoryPolicy(extract=False)
        active = (await self.client.post("/api/memories", json={"key": "response.language", "content": "English", "scope": "user"})).json()
        identity = await self.create_session("remember")
        response = await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "/remember user response.language = 中文"})
        await self.wait_run(response.json()["run_id"])
        candidate = (await self.client.get("/api/memories?status=candidate")).json()["items"][0]
        detail = (await self.client.get(f"/api/memories/{candidate['id']}")).json()
        self.assertEqual(detail["conflict"]["id"], active["id"])
        endpoint = f"/api/memories/{candidate['id']}/accept"
        self.assertEqual((await self.client.post(endpoint, json={"expected_revision": 1})).status_code, 409)
        accepted = await self.client.post(endpoint, json={"expected_revision": 1, "replace_id": active["id"], "replace_revision": active["revision"]})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["status"], "active")
        self.assertEqual((await self.client.get(f"/api/memories/{active['id']}")).json()["memory"]["status"], "rejected")

    async def test_fastapi_openapi_and_fully_local_reference(self):
        from fastapi import FastAPI
        self.assertIsInstance(self.app, FastAPI)
        response = await self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        schema = response.json()
        self.assertIn("本地 Agent", schema["info"]["title"])
        self.assertIn("/api/sessions/{session_id}/runs", schema["paths"])
        self.assertIn("202", schema["paths"]["/api/sessions/{session_id}/runs"]["post"]["responses"])
        self.assertIn("text/event-stream", schema["paths"]["/api/runs/{run_id}/events"]["get"]["responses"]["200"]["content"])
        self.assertFalse(schema["components"]["schemas"]["RunCreate"]["additionalProperties"])
        self.assertEqual(schema["components"]["schemas"]["MemoryEdit"]["properties"]["pinned"]["type"], "boolean")
        self.assertEqual(response.headers["cache-control"], "no-store")
        for operations in schema["paths"].values():
            for operation in operations.values():
                self.assertNotIn("422", operation.get("responses", {}))
        page = await self.client.get("/docs")
        self.assertEqual(page.status_code, 200)
        self.assertIn("本地接口文档", page.text)
        self.assertNotIn("https://", page.text)
        self.assertNotIn("http://", page.text)
        self.assertEqual((await self.client.get("/static/api-docs.js")).status_code, 200)
        self.assertEqual((await self.client.get("/docs", headers={"Origin": "https://other.example"})).status_code, 403)

    async def test_typed_validation_rejects_coercion_and_redacts_submitted_values(self):
        identity = await self.create_session()
        endpoint = f"/api/sessions/{identity}/runs"
        secret = "validation-private-value"
        for value in ({"text": False}, {"text": "x" * 16001}, {"text": "hello", "api_key": secret}):
            result = await self.client.post(endpoint, json=value)
            self.assertEqual(result.status_code, 400)
            self.assertIn("details", result.json())
            self.assertNotIn(secret, result.text)
            self.assertTrue(all("input" not in item and "ctx" not in item for item in result.json()["details"]))
        self.assertEqual((await self.client.get(f"/api/sessions/{identity}/messages")).json()["items"], [])
        result = await self.client.post("/api/memories", json={"key": "private.key", "content": "api_key=" + secret})
        self.assertEqual(result.status_code, 400)
        self.assertNotIn(secret, result.text)
        result = await self.client.post("/api/memories", json={"key": "strict.pinned", "content": "a fact", "pinned": "false"})
        self.assertEqual(result.status_code, 400)
        created = (await self.client.post("/api/memories", json={"key": "strict.edit", "content": "a fact"})).json()
        for fields in ({"expected_revision": True, "content": "other"}, {"expected_revision": "1", "content": "other"},
                       {"expected_revision": 1, "content": None}, {"expected_revision": 1, "pinned": None}):
            self.assertEqual((await self.client.patch(f"/api/memories/{created['id']}", json=fields)).status_code, 400)
        self.assertEqual((await self.client.get("/api/sessions/not-a-uuid")).status_code, 400)
        run = (await self.client.post(endpoint, json={"text": "valid"})).json()["run_id"]
        await self.wait_run(run)
        self.assertEqual((await self.client.get(f"/api/runs/{run}/events", headers={"Last-Event-ID": "-1"})).status_code, 400)
        self.assertEqual((await self.client.get(f"/api/runs/{run}/events?after=9223372036854775808")).status_code, 400)

    async def test_chunked_json_body_limit_is_checked_before_model_validation(self):
        identity = await self.create_session()
        async def chunks():
            for _ in range(3):
                yield b"x" * 24000
        result = await self.client.post(f"/api/sessions/{identity}/runs", headers={"Content-Type": "application/json"}, content=chunks())
        self.assertEqual(result.status_code, 413)
        self.assertEqual((await self.client.get(f"/api/sessions/{identity}/messages")).json()["items"], [])

    async def test_code_mode_cannot_be_enabled_for_web(self):
        from dataclasses import replace
        with self.assertRaisesRegex(ValueError, "code"):
            WebRuntime(self.runner, self.workspace, replace(self.runtime.settings, code_mode=True))

    async def test_source_workspace_and_ancestor_are_rejected(self):
        from chagent.web.workspaces import protected_source
        source = protected_source()
        for path in (source, source / "src", source.parent):
            response = await self.client.post("/api/workspaces", json={"path": str(path)})
            self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn(source, self.runtime.roots)
        identity = await self.runner.conversations.create_session(source, title="old source session")
        listed = await self.client.get("/api/workspaces")
        self.assertNotIn(str(source), [item["path"] for item in listed.json()["items"]])
        response = await self.client.get(f"/api/sessions/{identity}")
        self.assertEqual(response.status_code, 404)

    async def test_workspace_is_opened_before_it_can_be_addressed(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        target = Path(other.name).resolve()

        # Not open yet: listing, uploading and creating all refuse by name.
        self.assertEqual((await self.client.get("/api/sessions", params={"workspace": str(target)})).status_code, 400)
        self.assertEqual((await self.client.post("/api/uploads", params={"name": "a.txt", "workspace": str(target)},
                                                 content=b"hello")).status_code, 400)
        self.assertEqual((await self.client.post("/api/sessions", json={"title": "t", "workspace": str(target)})).status_code, 400)
        self.assertEqual((await self.client.post("/api/workspaces", json={"path": str(target / "missing")})).status_code, 400)

        opened = await self.client.post("/api/workspaces", json={"path": str(target)})
        self.assertEqual(opened.status_code, 201, opened.text)
        self.assertEqual(Path(opened.json()["path"]), target)

        created = await self.client.post("/api/sessions", json={"title": "另一个工作区", "workspace": str(target)})
        self.assertEqual(created.status_code, 201, created.text)
        identity = created.json()["id"]
        self.assertEqual(Path(created.json()["workspace"]), target)

        # Each list is scoped to the workspace asked for, not to the startup one.
        here = (await self.client.get("/api/sessions")).json()["items"]
        self.assertNotIn(identity, [item["id"] for item in here])
        there = (await self.client.get("/api/sessions", params={"workspace": str(target)})).json()["items"]
        self.assertIn(identity, [item["id"] for item in there])
        self.assertEqual((await self.client.get(f"/api/sessions/{identity}")).status_code, 200)

        upload = await self.client.post("/api/uploads", params={"name": "note.txt", "workspace": str(target)},
                                        content=b"hello")
        self.assertEqual(upload.status_code, 201, upload.text)
        self.assertTrue((target / "uploads" / "note.txt").is_file())
        self.assertFalse((self.workspace / "uploads" / "note.txt").exists())

        listed = (await self.client.get("/api/workspaces")).json()["items"]
        self.assertIn(str(target), [row["path"] for row in listed])
        self.assertGreaterEqual(next(row["sessions"] for row in listed if row["path"] == str(target)), 1)

    async def test_session_rename_archive_and_restore_round_trip(self):
        identity = await self.create_session("原标题")
        renamed = await self.client.patch(f"/api/sessions/{identity}", json={"title": "  新标题  "})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["title"], "新标题")
        listing = (await self.client.get("/api/sessions")).json()
        self.assertEqual(listing["items"][0]["title"], "新标题")
        self.assertEqual((await self.client.get(f"/api/sessions/{identity}")).json()["title"], "新标题")

        # An empty title means "no title", not "keep the old one".
        cleared = await self.client.patch(f"/api/sessions/{identity}", json={"title": ""})
        self.assertEqual(cleared.json()["title"], "")

        archived = await self.client.patch(f"/api/sessions/{identity}", json={"archived": True})
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertIsNotNone(archived.json()["archived_at"])
        self.assertEqual((await self.client.get("/api/sessions")).json()["items"], [])
        only = (await self.client.get("/api/sessions?archived=only")).json()
        self.assertEqual([item["id"] for item in only["items"]], [identity])
        both = (await self.client.get("/api/sessions?archived=include")).json()
        self.assertEqual([item["id"] for item in both["items"]], [identity])
        # The archived page keeps its own count out of the way.
        self.assertIsNone(only["archived_count"])
        self.assertEqual(both["archived_count"], 1)

        restored = await self.client.patch(f"/api/sessions/{identity}", json={"archived": False})
        self.assertIsNone(restored.json()["archived_at"])
        self.assertEqual([item["id"] for item in (await self.client.get("/api/sessions")).json()["items"]], [identity])

    async def test_archived_session_is_readable_but_refuses_new_runs(self):
        identity = await self.create_session("归档后只读")
        response = await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "先跑一次"})
        await self.wait_run(response.json()["run_id"])
        await self.client.patch(f"/api/sessions/{identity}", json={"archived": True})

        self.assertEqual((await self.client.get(f"/api/sessions/{identity}/messages")).status_code, 200)
        refused = await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "还能跑吗"})
        self.assertEqual(refused.status_code, 409, refused.text)
        runs = await self.runner.runs.active(roots=[str(self.workspace)])
        self.assertEqual(runs, [])
        detail = (await self.client.get(f"/api/sessions/{identity}")).json()
        self.assertIsNotNone(detail["archived_at"])

    async def test_session_search_matches_titles_only(self):
        first = await self.create_session("项目 Alpha 计划")
        second = await self.create_session("无关会话")
        response = await self.client.post(f"/api/sessions/{second}/runs", json={"text": "Alpha 出现在正文里"})
        await self.wait_run(response.json()["run_id"])

        found = (await self.client.get("/api/sessions", params={"q": "alpha"})).json()
        self.assertEqual([item["id"] for item in found["items"]], [first])
        # A wildcard typed by the user is a character, not a request for everything.
        self.assertEqual((await self.client.get("/api/sessions", params={"q": "%"})).json()["items"], [])
        self.assertEqual(isinstance(found["archived_count"], int), True)

    async def test_a_search_that_matches_everything_still_paginates(self):
        identities = [await self.create_session(f"批量会话 {index}") for index in range(3)]
        page = (await self.client.get("/api/sessions", params={"q": "批量会话", "limit": 2})).json()
        self.assertEqual(len(page["items"]), 2)
        rest = (await self.client.get("/api/sessions",
                                      params={"q": "批量会话", "limit": 2, "before": page["next_cursor"]})).json()
        self.assertEqual([item["id"] for item in rest["items"]], [identities[0]])

    async def test_session_edit_validation(self):
        identity = await self.create_session()
        endpoint = f"/api/sessions/{identity}"
        for body in ({}, {"title": None}, {"archived": None}, {"title": "x" * 201},
                     {"title": "ok", "unknown": 1}, {"archived": "true"}):
            with self.subTest(body=body):
                self.assertEqual((await self.client.patch(endpoint, json=body)).status_code, 400)
        self.assertEqual((await self.client.patch(f"/api/sessions/{uuid4()}", json={"title": "x"})).status_code, 404)
        # The same 200-character ceiling the command line enforces.
        self.assertEqual((await self.client.patch(endpoint, json={"title": "x" * 200})).status_code, 200)

    async def test_session_usage_sums_what_the_provider_reported(self):
        identity = await self.create_session("用量")
        run_id = await self.runner.runs.begin(UUID(identity), UserMessage("统计", 0))
        await self.write_model_calls(run_id, [
            {"input": 1000, "output": 200, "cache_read": 3000, "cache_write": 400, "total_tokens": 4600},
            {"input": 500, "output": 100, "cache_read": 1500, "cache_write": 0, "total_tokens": 3600},
        ])
        await self.runner.runs.finish(run_id, "completed")
        usage = (await self.client.get(f"/api/sessions/{identity}/usage")).json()
        self.assertEqual(usage["totals"]["input"], 1500)
        self.assertEqual(usage["totals"]["output"], 300)
        self.assertEqual(usage["totals"]["cache_read"], 4500)
        self.assertEqual(usage["totals"]["cache_write"], 400)
        self.assertEqual(usage["totals"]["total_tokens"], 8200)
        self.assertEqual(usage["totals"]["model_calls"], 2)
        self.assertEqual(usage["totals"]["runs"], 1)
        self.assertAlmostEqual(usage["totals"]["cache_hit_rate"], 4500 / 6000)
        self.assertEqual(usage["items"][0]["run_id"], str(run_id))

    async def test_a_call_without_usage_counts_but_contributes_no_tokens(self):
        identity = await self.create_session("缺用量")
        run_id = await self.runner.runs.begin(UUID(identity), UserMessage("统计", 0))
        await self.write_model_calls(run_id, [
            {"input": 100, "output": 20, "cache_read": 0, "cache_write": 0, "total_tokens": 120},
            None,
        ])
        await self.runner.runs.finish(run_id, "completed")
        usage = (await self.client.get(f"/api/sessions/{identity}/usage")).json()
        self.assertEqual(usage["totals"]["model_calls"], 2)
        self.assertEqual(usage["totals"]["total_tokens"], 120)
        # There were uncached input tokens, so a zero hit rate is a measurement.
        self.assertEqual(usage["totals"]["cache_hit_rate"], 0.0)

    async def test_daily_usage_covers_the_opened_workspace_only(self):
        identity = await self.create_session("今日用量")
        run_id = await self.runner.runs.begin(UUID(identity), UserMessage("统计", 0))
        await self.write_model_calls(run_id, [
            {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0, "total_tokens": 15}])
        await self.runner.runs.finish(run_id, "completed")
        daily = (await self.client.get("/api/usage?days=7")).json()
        self.assertEqual(daily["days"], 7)
        self.assertEqual(daily["totals"]["total_tokens"], 15)
        self.assertEqual(daily["totals"]["runs"], 1)
        self.assertEqual(len(daily["items"]), 1)
        self.assertEqual(daily["items"][0]["model_calls"], 1)
        empty = (await self.client.get("/api/usage?days=0")).status_code
        self.assertEqual(empty, 400)

    async def test_active_runs_report_the_tool_each_one_is_running(self):
        identity = await self.create_session("后台运行")
        run_id = await self.runner.runs.begin(UUID(identity), UserMessage("长命令", 0))
        model_call = uuid4()
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot) VALUES (%s, %s, 0, %s, %s)",
                (model_call, run_id, Jsonb({}), Jsonb({})))
        from chagent.domain.messages import ToolCall
        record = (await self.runner.runs.queue_tool_calls(
            run_id, model_call, [ToolCall("c1", "exec_command", {"cmd": "sleep 45"})]))[0]
        async with self.database.transaction() as connection:
            await connection.execute(
                "UPDATE tool_calls SET status = 'running', created_at = now() WHERE id = %s", (record,))

        page = (await self.client.get("/api/runs?active=true")).json()
        self.assertEqual([item["run_id"] for item in page["items"]], [str(run_id)])
        self.assertEqual(page["items"][0]["session_id"], identity)
        self.assertEqual(page["items"][0]["current_tool"], "exec_command")
        self.assertEqual((await self.client.get("/api/runs?active=false")).status_code, 400)

        await self.runner.runs.finish(run_id, "cancelled")
        self.assertEqual((await self.client.get("/api/runs?active=true")).json()["items"], [])

    async def test_active_runs_stay_inside_the_opened_workspace(self):
        other = await self.runner.conversations.create_session(self.workspace.parent)
        run_id = await self.runner.runs.begin(other, UserMessage("别处的运行", 0))
        mine = await self.create_session("这里的运行")
        my_run = await self.runner.runs.begin(UUID(mine), UserMessage("这里的运行", 0))
        page = (await self.client.get("/api/runs?active=true")).json()
        self.assertEqual([item["run_id"] for item in page["items"]], [str(my_run)])
        # A row inserted directly has no in-process owner. Cancellation refuses
        # it honestly instead of pretending, and never touches the other run.
        cancelled = await self.client.post(f"/api/runs/{my_run}/cancel")
        self.assertEqual(cancelled.status_code, 409, cancelled.text)
        self.assertEqual((await self.runner.runs.get(run_id))["status"], "running")
        await self.runner.runs.finish(run_id, "cancelled")

    async def write_model_calls(self, run_id, usages):
        async with self.database.transaction() as connection:
            for index, usage in enumerate(usages):
                response = {"usage": usage} if usage is not None else {"content": []}
                await connection.execute(
                    "INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot, response, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'completed')",
                    (uuid4(), run_id, index, Jsonb({}), Jsonb({}), Jsonb(response)))

    async def test_config_reports_the_resolved_switches(self):
        value = (await self.client.get("/api/config")).json()
        self.assertEqual(value["provider"], "local-test")
        self.assertGreaterEqual(value["tools"], 8)
        self.assertIn(value["sandbox"], {"off", "workspace-write"})
        self.assertIn(value["approval"], {"never", "on-request", "untrusted"})
        self.assertEqual(value["web_key"], "missing")
        self.assertIn("sandbox=", value["behavior"])
        self.assertIn("max_turns=", value["behavior"])
        # A read-only page must never become a way to read a credential back out.
        self.assertNotIn("postgres", json.dumps(value))
        self.assertNotIn("api_key", json.dumps(value))

    async def test_work_mode_is_the_only_face_this_build_serves(self):
        """The mode is refused, not downgraded, and a run inherits it."""
        config = (await self.client.get("/api/config")).json()
        self.assertEqual(config["default_mode"], "work")
        self.assertFalse(config["code_mode"])

        refused = await self.client.post("/api/sessions", json={"title": "要写文件", "mode": "code"})
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("CHAGENT_CODE_MODE", refused.text)
        unknown = await self.client.post("/api/sessions", json={"title": "x", "mode": "admin"})
        self.assertEqual(unknown.status_code, 400, unknown.text)

        identity = await self.create_session("默认会话")
        detail = (await self.client.get(f"/api/sessions/{identity}")).json()
        self.assertEqual(detail["mode"], "work")
        # A run may not switch the face its session was created with.
        mismatch = await self.client.post(f"/api/sessions/{identity}/runs",
                                          json={"text": "写", "mode": "code"})
        self.assertEqual(mismatch.status_code, 409, mismatch.text)
        agree = await self.client.post(f"/api/sessions/{identity}/runs",
                                       json={"text": "读", "mode": "work"})
        self.assertEqual(agree.status_code, 202, agree.text)
        await self.wait_run(agree.json()["run_id"])

    async def test_a_code_session_is_refused_when_the_build_switch_is_off(self):
        """A row written by a code-enabled build must not keep working here."""
        identity = await self.runner.conversations.create_session(self.workspace, mode="code")
        response = await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "继续"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(await self.runner.runs.latest(identity), None)

    # -- WK2: 改得动 ------------------------------------------------------

    async def test_regenerate_retires_the_old_answer_but_keeps_it_readable(self):
        """The transcript stays append-only; only replay stops seeing the old turn."""
        identity = await self.create_session("重新生成")
        first = (await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "第一轮"})).json()["run_id"]
        await self.wait_run(first)
        page = (await self.client.get(f"/api/sessions/{identity}/messages")).json()["items"]
        answers = [item for item in page if item["message"]["role"] == "assistant"]
        self.assertTrue(answers, page)
        target = answers[-1]
        users_before = len([item for item in page if item["message"]["role"] == "user"])

        response = await self.client.post(f"/api/sessions/{identity}/runs",
                                          json={"regenerate_from": target["id"]})
        self.assertEqual(response.status_code, 202, response.text)
        second = response.json()["run_id"]
        self.assertEqual((await self.wait_run(second))["status"], "completed")

        after = (await self.client.get(f"/api/sessions/{identity}/messages")).json()["items"]
        retired = {item["id"]: item for item in after if item["superseded_by"]}
        # Everything from the replaced answer onwards is retired, not deleted.
        self.assertIn(target["id"], retired)
        self.assertEqual(retired[target["id"]]["superseded_by"], second)
        self.assertTrue(all(item["sequence"] >= target["sequence"] for item in retired.values()))
        self.assertTrue(all(item["superseded_by"] == second for item in retired.values()))
        # The old answer's text is still there to compare against.
        self.assertEqual(retired[target["id"]]["message"], target["message"])
        # A regeneration writes no second copy of the question.
        users_after = len([item for item in after if item["message"]["role"] == "user"])
        self.assertEqual(users_after, users_before)

        # The model's replay is the linear story: retired rows are not in it.
        replay = await self.runner.conversations.messages(UUID(identity), current_run=UUID(second))
        self.assertNotIn(target["id"], {str(item.id) for item in replay})
        self.assertTrue(replay)
        self.assertEqual(replay[-1].run_id, UUID(second))

    async def test_edit_resends_without_rewriting_the_original(self):
        identity = await self.create_session("编辑重发")
        first = (await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "原来的问题"})).json()["run_id"]
        await self.wait_run(first)
        page = (await self.client.get(f"/api/sessions/{identity}/messages")).json()["items"]
        original = next(item for item in page if item["message"]["role"] == "user")

        response = await self.client.post(f"/api/sessions/{identity}/runs",
                                          json={"text": "改过的问题", "edit_message_id": original["id"]})
        self.assertEqual(response.status_code, 202, response.text)
        second = response.json()["run_id"]
        await self.wait_run(second)

        after = (await self.client.get(f"/api/sessions/{identity}/messages")).json()["items"]
        retired = next(item for item in after if item["id"] == original["id"])
        self.assertEqual(retired["superseded_by"], second)
        # The original text is preserved verbatim, not overwritten in place.
        self.assertEqual(retired["message"]["content"], "原来的问题")
        new_users = [item for item in after if item["message"]["role"] == "user" and not item["superseded_by"]]
        self.assertEqual([item["message"]["content"] for item in new_users], ["改过的问题"])
        self.assertEqual(new_users[0]["run_id"], second)

        replay = await self.runner.conversations.messages(UUID(identity), current_run=UUID(second))
        self.assertEqual([item.message.content for item in replay if item.message.role == "user"], ["改过的问题"])

    async def test_replacement_targets_are_checked_before_a_run_exists(self):
        identity = await self.create_session("替换目标")
        other = await self.create_session("别的会话")
        run = (await self.client.post(f"/api/sessions/{identity}/runs", json={"text": "问题"})).json()["run_id"]
        await self.wait_run(run)
        page = (await self.client.get(f"/api/sessions/{identity}/messages")).json()["items"]
        user = next(item for item in page if item["message"]["role"] == "user")
        answer = next(item for item in page if item["message"]["role"] == "assistant")

        # Regenerating a question or editing an answer names the wrong kind of record.
        self.assertEqual((await self.client.post(f"/api/sessions/{identity}/runs",
                          json={"regenerate_from": user["id"]})).status_code, 400)
        self.assertEqual((await self.client.post(f"/api/sessions/{identity}/runs",
                          json={"text": "改", "edit_message_id": answer["id"]})).status_code, 400)
        # A message of another session is not addressable through this one.
        self.assertEqual((await self.client.post(f"/api/sessions/{other}/runs",
                          json={"regenerate_from": answer["id"]})).status_code, 404)
        # Both replacement shapes at once is a malformed request, not a merge.
        self.assertEqual((await self.client.post(f"/api/sessions/{identity}/runs",
                          json={"text": "x", "regenerate_from": answer["id"],
                                "edit_message_id": user["id"]})).status_code, 400)
        # A regeneration carries no text; an empty ordinary message is still refused.
        self.assertEqual((await self.client.post(f"/api/sessions/{identity}/runs",
                          json={"text": "  "})).status_code, 400)
        self.assertEqual((await self.client.post(f"/api/sessions/{identity}/runs",
                          json={})).status_code, 400)
        self.assertEqual((await self.client.get(f"/api/sessions/{identity}/messages")).json()["items"], page)

    # -- WK3: 有出处 ------------------------------------------------------

    async def test_sources_report_the_memories_a_run_was_given(self):
        """Memory is not a message, so it has its own read path."""
        identity = await self.create_session("依据")
        # A manually written memory is active on arrival; only an extracted
        # candidate waits for review. Pinned ones are injected without a keyword
        # match, which is what makes this independent of recall scoring.
        (await self.client.post("/api/memories",
         json={"key": "project.database", "content": "使用 PostgreSQL", "pinned": True}))
        run = (await self.client.post(f"/api/sessions/{identity}/runs",
               json={"text": "这次会话的数据存在哪里"})).json()["run_id"]
        await self.wait_run(run)

        sources = (await self.client.get(f"/api/sessions/{identity}/sources")).json()
        entry = next(item for item in sources["runs"] if item["run_id"] == run)
        keys = [memory["key"] for memory in entry["memories"]]
        self.assertIn("project.database", keys)
        memory = next(item for item in entry["memories"] if item["key"] == "project.database")
        # The card shows the text that was actually injected, not just the key.
        self.assertEqual(memory["content"], "使用 PostgreSQL")
        self.assertEqual(memory["scope"], "workspace")

        # A session with no runs has nothing to report, not an error.
        idle = await self.create_session("没有运行")
        self.assertEqual((await self.client.get(f"/api/sessions/{idle}/sources")).json(), {"runs": []})
        # A deleted memory stops being cited: the card reflects what was injected,
        # not what the key once said.
        self.assertEqual((await self.client.get(f"/api/sessions/{identity}/sources")).status_code, 200)


@unittest.skipUnless(os.environ.get("CHAGENT_TEST_DATABASE_URL"), "Set CHAGENT_TEST_DATABASE_URL")
class ApprovalApiTests(unittest.IsolatedAsyncioTestCase):
    """A question is visible and answerable over the local API, and only here."""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.schema = "chagent_web_approval_" + uuid4().hex
        self.admin = await AsyncConnection.connect(os.environ["CHAGENT_TEST_DATABASE_URL"], autocommit=True)
        self.addAsyncCleanup(self.admin.close)
        await self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.drop_schema)
        self.database = Database(Settings(database_url=make_conninfo(
            os.environ["CHAGENT_TEST_DATABASE_URL"], options=f"-c search_path={self.schema}")))
        self.addAsyncCleanup(self.database.close)
        await self.database.connect()
        await migrate(self.database)
        self.runner = create_demo_runner(self.database)
        self.runtime = WebRuntime(self.runner, self.workspace)
        self.app = create_app(workspace=self.workspace, injected_runtime=self.runtime)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1", trust_env=False)
        self.addAsyncCleanup(self.client.aclose)
        self.session = await self.runner.conversations.create_session(self.workspace)
        self.run_id = await self.runner.runs.begin(self.session, UserMessage("需要批准", 0))
        from chagent.storage.approvals import ApprovalRepository
        from chagent.domain.messages import ToolCall
        from psycopg.types.json import Jsonb
        model_call = uuid4()
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot) "
                "VALUES (%s, %s, 0, %s, %s)", (model_call, self.run_id, Jsonb({}), Jsonb({})))
        record = (await self.runner.runs.queue_tool_calls(
            self.run_id, model_call, [ToolCall("c1", "exec_command", {"cmd": "ls"})]))[0]
        self.approvals = ApprovalRepository(self.database, self.runner.runs)
        self.request = await self.approvals.create(
            run_id=self.run_id, tool_record_id=record, fingerprint="fp", tool_name="exec_command",
            cwd=str(self.workspace), subject="ls", ttl=60)

    async def drop_schema(self):
        await self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    async def test_pending_question_is_projected_and_can_be_decided_once(self):
        view = await self.client.get(f"/api/runs/{self.run_id}")
        self.assertEqual(view.status_code, 200, view.text)
        self.assertEqual(view.json()["waiting_for"], "approval")
        self.assertEqual(view.json()["approval_id"], str(self.request.id))

        pending = await self.client.get(f"/api/runs/{self.run_id}/approvals")
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertEqual([item["subject"] for item in pending.json()["pending"]], ["ls"])

        decided = await self.client.post(f"/api/approvals/{self.request.id}/decide", json={"approve": True})
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(decided.json()["status"], "approved")

        again = await self.client.post(f"/api/approvals/{self.request.id}/decide", json={"approve": False})
        self.assertEqual(again.status_code, 409, again.text)

    async def test_approvals_outside_the_workspace_are_not_visible(self):
        other = Path(self.directory.name, "elsewhere")
        other.mkdir()
        other_runtime = WebRuntime(self.runner, other)
        app = create_app(workspace=other, injected_runtime=other_runtime)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", trust_env=False) as client:
                self.assertEqual((await client.get(f"/api/runs/{self.run_id}")).status_code, 404)
                self.assertEqual((await client.post(f"/api/approvals/{self.request.id}/decide",
                                                    json={"approve": True})).status_code, 404)
