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
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1")
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
        for asset in ("app.js", "style.css"):
            self.assertEqual((await self.client.get(f"/static/{asset}")).status_code, 200)
        info = (await self.client.get("/api/info")).json()
        self.assertEqual(set(info), {"workspace", "provider", "model"})

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
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=5) as client:
            response = await client.post(f"/api/sessions/{identity}/runs", json={"text": "持续运行"})
            run_id = response.json()["run_id"]
            await asyncio.wait_for(entered.wait(), 5)
            cursor = 0
            async with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("id: "):
                        cursor = int(line[4:])
                    if line.startswith("data: ") and json.loads(line[6:]).get("type") == "text_delta":
                        break
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
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1")
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
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                self.assertEqual((await client.get(f"/api/runs/{self.run_id}")).status_code, 404)
                self.assertEqual((await client.post(f"/api/approvals/{self.request.id}/decide",
                                                    json={"approve": True})).status_code, 404)
