"""Single-process local web application, sharing the existing agent and storage."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib.resources import files
import json
import logging
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from chagent.agent.runner import AgentRunner, RunFailedError
from chagent.bootstrap import create_demo_runner, create_runner
from chagent.config import Settings
from chagent.tools.sandbox import Sandbox
from chagent.domain.codec import message_to_dict
from chagent.models.factory import create_model_client
from chagent.models.settings import ModelSettings
from chagent.sessions.errors import SessionArchivedError, SessionNotFoundError
from chagent.sessions.manager import SessionManager
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import RunBusyError
from chagent.storage.summaries import SummaryRepository
from chagent.storage.artifacts import ArtifactRepository
from chagent.domain.artifacts import artifact_text
from chagent.domain.messages import ToolResultMessage
from chagent.domain.memories import MemoryProposal
from chagent.storage.memories import MemoryRepository
from chagent.storage.memory_jobs import MemoryJobRepository
from chagent.web.schemas import MemoryJobState
from chagent.sessions.errors import StateConflictError
from chagent.web.schemas import (AppInfo, ArtifactPage, Cancellation, CancelRequest, ContextView, ErrorResponse,
    MemoryCreate, MemoryDetail, MemoryEdit, MemoryPage, MemoryReview, MessagePage, RevisionRequest,
    RunAccepted, RunCreate, RunView, SessionCreate, SessionDetail, SessionPage)
from chagent.domain.sessions import Session
from chagent.domain.memories import Memory


SSE_POLL_INTERVAL = 1.0
TERMINAL_EVENTS = {"run_completed", "run_failed", "run_cancelled", "run_interrupted"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
logger = logging.getLogger(__name__)


def reply(value, status=200, headers=None):
    return JSONResponse(jsonable_encoder(value), status_code=status,
                        headers={"Cache-Control": "no-store", **(headers or {})})


class WebRuntime:
    def __init__(self, runner: AgentRunner, workspace: Path):
        self.runner = runner
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise NotADirectoryError("Workspace must be a directory")
        self.sessions = SessionManager(runner.conversations)
        self.tasks: set[asyncio.Task] = set()
        self.closing = False
        self._workspace_id = None
        self._recovery_task: asyncio.Task | None = None

    async def workspace_id(self):
        if self._workspace_id is None:
            async with self.runner.conversations.database.connection() as connection:
                cursor = await connection.execute(
                    "INSERT INTO workspaces (id, root_path) VALUES (%s, %s) ON CONFLICT (root_path) "
                    "DO UPDATE SET root_path = EXCLUDED.root_path RETURNING id", (uuid4(), str(self.workspace)))
                self._workspace_id = (await cursor.fetchone())["id"]
        return self._workspace_id

    async def session(self, identity: UUID):
        session = await self.sessions.get(identity)
        if session.workspace != self.workspace:
            raise SessionNotFoundError("Session does not belong to this workspace")
        return session

    async def run(self, identity: UUID):
        run = await self.runner.runs.get(identity)
        if run is None:
            raise HTTPException(404, "Run does not exist")
        await self.session(run["session_id"])
        return run

    async def start(self, session_id: UUID, text: str) -> UUID:
        if self.closing:
            raise HTTPException(503, "Server is shutting down")
        started = asyncio.get_running_loop().create_future()

        async def drive():
            try:
                await self.runner.run(session_id, text, on_started=started.set_result)
            except asyncio.CancelledError:
                if not started.done():
                    started.cancel()
                raise
            except Exception as error:
                if not started.done():
                    started.set_exception(error)
                elif not isinstance(error, RunFailedError):
                    # Avoid logging arbitrary exception strings that could contain credentials.
                    logger.error("Background run failed (%s)", type(error).__name__)

        task = asyncio.create_task(drive(), name=f"web-run-{session_id}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        # Request disconnection must not cancel the durable agent task.
        return await asyncio.shield(started)

    async def _recover_runs(self) -> None:
        while True:
            try:
                while await self.runner.runs.reap_expired():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Run recovery failed (%s)", type(error).__name__)
            await asyncio.sleep(5)

    def start_worker(self) -> None:
        """Start recovery and extraction while the application is serving."""
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._recover_runs(), name="run-recovery")
        if getattr(self.runner, "memory_worker", None) is not None:
            self.runner.memory_worker.start()

    async def close(self):
        self.closing = True
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            await asyncio.gather(self._recovery_task, return_exceptions=True)
            self._recovery_task = None
        for task in self.tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # Queued jobs survive shutdown: an interrupted job is returned to the
        # queue and picked up by the next process.
        if getattr(self.runner, "memory_worker", None) is not None:
            await self.runner.memory_worker.stop()


def runtime(request: Request) -> WebRuntime:
    return request.app.state.runtime


Current = Annotated[WebRuntime, Depends(runtime)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
MessageLimit = Annotated[int, Query(ge=1, le=200)]
SequenceCursor = Annotated[int, Query(ge=0, le=9_223_372_036_854_775_807)]
api = APIRouter(prefix="/api", responses={code: {"model": ErrorResponse} for code in (400, 403, 404, 409, 413, 415, 503)})


@api.get("/info", response_model=AppInfo, tags=["本地 Agent"])
async def info(current: Current):
    return {"workspace": current.workspace, "provider": current.runner.model.provider, "model": current.runner.model.id}


@api.get("/memories", response_model=MemoryPage, tags=["长期记忆"])
async def memories(current: Current, status: Literal["active", "candidate", "deleted", "rejected", "all"] = "active",
                   limit: PageLimit = 50, before: UUID | None = None):
    repository = MemoryRepository(current.runner.conversations.database)
    return await repository.list(await current.workspace_id(), status=status, limit=limit, before=before)


@api.post("/memories", response_model=Memory, status_code=201, tags=["长期记忆"])
async def create_memory(value: MemoryCreate, current: Current):
    repository = MemoryRepository(current.runner.conversations.database)
    return await repository.propose(MemoryProposal(value.key, value.content, value.scope, value.pinned, "manual"),
                                    await current.workspace_id())


@api.get("/memories/{memory_id}", response_model=MemoryDetail, tags=["长期记忆"])
async def memory_detail(memory_id: UUID, current: Current):
    repository = MemoryRepository(current.runner.conversations.database)
    memory = await repository.get(memory_id, await current.workspace_id())
    active = await repository.current(memory.scope, memory.workspace_id, memory.key) if memory.status == "candidate" else None
    return {"memory": memory, "conflict": active}


@api.patch("/memories/{memory_id}", response_model=Memory, tags=["长期记忆"])
async def edit_memory(memory_id: UUID, value: MemoryEdit, current: Current):
    return await MemoryRepository(current.runner.conversations.database).change(
        memory_id, await current.workspace_id(), expected_revision=value.expected_revision,
        content=value.content, pinned=value.pinned)


@api.delete("/memories/{memory_id}", response_model=Memory, tags=["长期记忆"])
async def delete_memory(memory_id: UUID, value: RevisionRequest, current: Current):
    return await MemoryRepository(current.runner.conversations.database).change(
        memory_id, await current.workspace_id(), expected_revision=value.expected_revision, action="delete")


@api.post("/memories/{memory_id}/accept", response_model=Memory, tags=["长期记忆"])
async def accept_memory(memory_id: UUID, value: MemoryReview, current: Current):
    return await MemoryRepository(current.runner.conversations.database).change(
        memory_id, await current.workspace_id(), expected_revision=value.expected_revision, action="accept",
        replace_revision=value.replace_revision, replace_id=value.replace_id)


@api.post("/memories/{memory_id}/reject", response_model=Memory, tags=["长期记忆"])
async def reject_memory(memory_id: UUID, value: RevisionRequest, current: Current):
    return await MemoryRepository(current.runner.conversations.database).change(
        memory_id, await current.workspace_id(), expected_revision=value.expected_revision, action="reject")


@api.get("/memories/{memory_id}/revisions", tags=["长期记忆"])
async def memory_history(memory_id: UUID, current: Current):
    return await MemoryRepository(current.runner.conversations.database).revisions(memory_id, await current.workspace_id())


@api.get("/sessions", response_model=SessionPage, tags=["会话"])
async def sessions(current: Current, limit: PageLimit = 50, before: UUID | None = None):
    return await current.sessions.list(workspace=current.workspace, before=before, limit=limit)


@api.post("/sessions", response_model=Session, status_code=201, tags=["会话"])
async def create_session(value: SessionCreate, current: Current):
    return await current.sessions.create(current.workspace, value.title)


@api.get("/sessions/{session_id}", response_model=SessionDetail, tags=["会话"])
async def session_detail(session_id: UUID, current: Current):
    session = await current.session(session_id)
    return {**asdict(session), "latest_run": await current.runner.runs.latest(session_id)}


@api.get("/sessions/{session_id}/messages", response_model=MessagePage, tags=["会话"])
async def history(session_id: UUID, current: Current, limit: MessageLimit = 100,
                  before: Annotated[int | None, Query(ge=1, le=9_223_372_036_854_775_807)] = None):
    await current.session(session_id)
    page = await current.sessions.messages(session_id, before=before, limit=limit)
    def visible(message):
        payload = message_to_dict(message)
        if isinstance(message, ToolResultMessage) and message.artifact:
            payload["content"] = [{"type": "text", "text": artifact_text(message.artifact)},
                                  *(block for block in payload["content"] if block["type"] == "image")]
            payload["details"] = None
        return payload
    return {"items": [{"id": item.id, "sequence": item.sequence, "run_id": item.run_id,
                        "message": visible(item.message)} for item in page.items], "next_before": page.next_before}


@api.get("/sessions/{session_id}/artifacts/{artifact_id}", response_model=ArtifactPage, tags=["上下文与产物"])
async def artifact_page(session_id: UUID, artifact_id: UUID, current: Current,
                        offset: Annotated[int, Query(ge=0, le=2_147_483_646)] = 0,
                        limit: Annotated[int, Query(ge=1, le=1000)] = 1000):
    await current.session(session_id)
    try:
        return await ArtifactRepository(current.runner.conversations.database).read(session_id, artifact_id, offset=offset, limit=limit)
    except LookupError:
        raise HTTPException(404, "Artifact does not exist in this session") from None


@api.get("/sessions/{session_id}/context", response_model=ContextView, tags=["上下文与产物"])
async def context_detail(session_id: UUID, current: Current):
    await current.session(session_id)
    summaries = await SummaryRepository(current.runner.conversations.database).list(session_id, limit=10)
    versions = []
    for summary in summaries:
        source = await current.runner.runs.get(summary.source_run_id) if summary.source_run_id else None
        versions.append({**asdict(summary), "source_run_status": source["status"] if source else None,
                         "replayable": source is None or source["status"] in {"running", "completed"}})
    return {"summaries": versions, "snapshots": await current.sessions.contexts(session_id, limit=3)}


@api.post("/sessions/{session_id}/runs", response_model=RunAccepted, status_code=202, tags=["运行"])
async def send(session_id: UUID, value: RunCreate, current: Current):
    await current.session(session_id)
    return {"run_id": await current.start(session_id, value.text), "session_id": session_id}


@api.get("/runs/{run_id}", response_model=RunView, tags=["运行"])
async def run_detail(run_id: UUID, current: Current):
    return await current.run(run_id)


@api.post("/runs/{run_id}/cancel", response_model=Cancellation, response_model_exclude_none=True,
          responses={202: {"model": Cancellation}}, tags=["运行"])
async def cancel(run_id: UUID, current: Current, response: Response, value: CancelRequest | None = None):
    run = await current.run(run_id)
    if run["status"] in TERMINAL:
        return {"accepted": False, "status": run["status"]}
    accepted = current.runner.cancel(run_id)
    if not accepted:
        latest = await current.run(run_id)
        if latest["status"] in TERMINAL:
            return {"accepted": False, "status": latest["status"]}
        raise HTTPException(409, "Run belongs to another process or needs recovery")
    response.status_code = 202
    return {"accepted": accepted}


@api.get("/runs/{run_id}/memory-job", response_model=MemoryJobState, tags=["长期记忆"])
async def memory_job(run_id: UUID, current: Current):
    await current.run(run_id)  # Same workspace isolation as the run/event endpoints.
    jobs = MemoryJobRepository(current.runner.runs.database)
    return {"job": await jobs.for_run(run_id)}


@api.get("/runs/{run_id}/events", response_class=StreamingResponse, tags=["运行"],
         responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def events(run_id: UUID, request: Request, current: Current, after: SequenceCursor = 0,
                 last_event_id: Annotated[int | None, Header(ge=0, le=9_223_372_036_854_775_807)] = None):
    identity = run_id
    await current.run(identity)
    after = last_event_id if last_event_id is not None else after

    async def stream():
        sequence = after
        heartbeat = monotonic()
        runs = current.runner.runs
        with runs.notifications.subscribe(identity) as subscription:
            while True:
                # Capture before catchup, so commits between querying and waiting
                # change the version and cannot be lost.
                version = subscription.version
                run = await runs.get(identity)
                stop_at = await runs.terminal_sequence(identity) if run["status"] in TERMINAL else None
                if stop_at is not None and sequence >= stop_at:
                    yield "event: end\ndata: {}\n\n"
                    return
                batch = await runs.events(identity, sequence, limit=200)
                for event in batch:
                    sequence = event["sequence"]
                    payload = json.dumps({"type": event["type"], "payload": event["payload"]}, ensure_ascii=False)
                    yield f"id: {sequence}\nevent: update\ndata: {payload}\n\n"
                    if event["type"] in TERMINAL_EVENTS:
                        yield "event: end\ndata: {}\n\n"
                        return
                if run["status"] in TERMINAL and len(batch) < 200:
                    yield "event: end\ndata: {}\n\n"
                    return
                if await request.is_disconnected():
                    return
                if monotonic() - heartbeat > 15:
                    yield ": heartbeat\n\n"
                    heartbeat = monotonic()
                if len(batch) < 200:
                    await subscription.wait(version, timeout=SSE_POLL_INTERVAL)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
    })


async def asset(name: str = "index.html"):
    media = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css",
             "api-docs.html": "text/html", "api-docs.js": "text/javascript"}
    if name not in media:
        raise HTTPException(404)
    return Response(files("chagent.web").joinpath("static", name).read_bytes(), media_type=media[name],
                    headers={"Cache-Control": "no-cache"})


async def http_error(request, error):
    return reply({"error": str(error.detail)}, error.status_code, headers=error.headers)


async def validation_error(request: Request, error: RequestValidationError):
    # Never echo Pydantic's input/ctx or the submitted body (it may contain secrets).
    details = [{"location": list(item["loc"]), "message": item["msg"], "type": item["type"]}
               for item in error.errors()]
    first = details[0] if details else None
    message = f"{'.'.join(map(str, first['location']))}: {first['message']}" if first else "Invalid request"
    return reply({"error": message, "details": details}, 400)


async def application_error(request, error):
    if isinstance(error, LookupError):
        return reply({"error": str(error)}, 404)
    if isinstance(error, SessionNotFoundError):
        return reply({"error": str(error)}, 404)
    if isinstance(error, (RunBusyError, SessionArchivedError, StateConflictError)):
        return reply({"error": str(error)}, 409)
    if isinstance(error, ValueError):
        return reply({"error": str(error)}, 400)
    return reply({"error": "Server request failed"}, 500)


class LocalBoundary:
    """Reject cross-origin calls to the unauthenticated loopback-only service."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        origin = headers.get(b"origin")
        expected = scope["scheme"].encode() + b"://" + headers.get(b"host", b"")
        if (origin is not None and origin != expected) or headers.get(b"sec-fetch-site") == b"cross-site":
            return await JSONResponse({"error": "Cross-origin access is disabled"}, 403)(scope, receive, send)

        if scope["path"].startswith("/api/") and scope["method"] in {"POST", "PATCH", "DELETE"}:
            content = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                content.extend(message.get("body", b""))
                if len(content) > 65_536:
                    return await reply({"error": "Request exceeds 64 KiB"}, 413)(scope, receive, send)
                if not message.get("more_body", False):
                    break
            media_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
            if content and media_type != b"application/json":
                return await reply({"error": "Use application/json"}, 415)(scope, receive, send)
            upstream_receive = receive
            delivered = False

            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(content), "more_body": False}
                return await upstream_receive()
            receive = buffered_receive

        async def secured(message):
            if message["type"] == "http.response.start":
                if (scope["path"].startswith("/api/") or scope["path"] == "/openapi.json") and not any(
                        key.lower() == b"cache-control" for key, _ in message["headers"]):
                    message["headers"].append((b"cache-control", b"no-store"))
                message["headers"] += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"),
                ]
            await send(message)

        await self.app(scope, receive, secured)


def create_app(*, workspace: Path, settings: Settings | None = None,
               model_settings: ModelSettings | None = None, demo: bool = False,
               injected_runtime: WebRuntime | None = None) -> FastAPI:
    """Injected runtime is caller-owned; production resources belong to lifespan."""
    workspace = workspace.expanduser().resolve(strict=True)

    @asynccontextmanager
    async def lifespan(app):
        if injected_runtime is not None:
            app.state.runtime = injected_runtime
            injected_runtime.start_worker()
            try:
                yield
            finally:
                await injected_runtime.close()
            return
        configured = settings or Settings.from_env()
        selected = None if demo else (model_settings or ModelSettings.from_env())
        client = create_model_client(selected) if selected is not None else None
        try:
            async with Database(configured) as database:
                await migrate(database)
                sandbox = Sandbox.from_settings(configured)
                if selected is None:
                    runner = create_demo_runner(database, allow_host_commands=configured.allow_host_commands, max_turns=configured.max_turns,
                                                max_tool_concurrency=configured.max_tool_concurrency,
                                                memory_worker_concurrency=configured.memory_worker_concurrency,
                                                sandbox=sandbox)
                else:
                    runner = create_runner(database, selected.model(), client,
                                           allow_host_commands=configured.allow_host_commands,
                                           timeout=selected.run_timeout, max_turns=configured.max_turns,
                                           max_tool_concurrency=configured.max_tool_concurrency,
                                           memory_worker_concurrency=configured.memory_worker_concurrency,
                                           sandbox=sandbox)
                app.state.runtime = WebRuntime(runner, workspace)
                app.state.runtime.start_worker()
                try:
                    yield
                finally:
                    await app.state.runtime.close()
        finally:
            if client is not None:
                await client.aclose()

    app = FastAPI(
        title="Chagent 本地 Agent API", version="0.1.0",
        description="本机单用户 Agent。会话、工具、上下文与记忆由本地进程和 PostgreSQL 托管；模型密钥仅在服务端配置。",
        lifespan=lifespan, docs_url=None, redoc_url=None,
        exception_handlers={HTTPException: http_error, RequestValidationError: validation_error,
                            SessionNotFoundError: application_error, RunBusyError: application_error,
                            SessionArchivedError: application_error, ValueError: application_error,
                            LookupError: application_error, StateConflictError: application_error,
                            Exception: application_error},
    )
    app.include_router(api)
    app.add_api_route("/", asset, include_in_schema=False)
    app.add_api_route("/static/{name}", asset, include_in_schema=False)
    async def local_docs():
        return await asset("api-docs.html")
    app.add_api_route("/docs", local_docs, include_in_schema=False)
    # Match the public OpenAPI contract to the existing 400 validation response.
    def schema():
        from fastapi.openapi.utils import get_openapi
        if app.openapi_schema is None:
            result = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
            for path in result["paths"].values():
                for operation in path.values():
                    if isinstance(operation, dict):
                        operation.get("responses", {}).pop("422", None)
            app.openapi_schema = result
        return app.openapi_schema
    app.openapi = schema
    app.add_middleware(LocalBoundary)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    return app
