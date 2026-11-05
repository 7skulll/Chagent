"""Single-process local web application, sharing the existing agent and storage."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib.resources import files
import json
import logging
import os
import httpx
from pathlib import Path
from time import monotonic
from uuid import UUID
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from chagent.bootstrap import create_demo_runner, create_runner
from chagent.config import Settings
from chagent.tools.policy import ApprovalPolicy
from chagent.tools.sandbox import Sandbox
from chagent.tools.web import WebAccess
from chagent.domain.codec import message_to_dict
from chagent.diagnostics.behavior import describe as describe_behavior
from chagent.domain.summaries import CompactionPolicy
from chagent.models.factory import create_model_client
from chagent.models.settings import ModelSettings
from chagent.sessions.errors import SessionArchivedError, SessionBusyError, SessionNotFoundError
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import RunBusyError
from chagent.storage.summaries import SummaryRepository
from chagent.storage.usage import UsageRepository
from chagent.storage.artifacts import ArtifactRepository
from chagent.domain.artifacts import artifact_text
from chagent.domain.messages import ToolResultMessage
from chagent.storage.memory_jobs import MemoryJobRepository
from chagent.web.policy import validate_web_settings
from chagent.web.deps import Current, MessageLimit, PageLimit, SequenceCursor, WorkspacePath
from chagent.web.runtime import WebRuntime
from chagent.web.auth import HostAuth, HostAuthBoundary, COOKIE, SESSION_SECONDS
from chagent.web.schemas import MemoryJobState
from chagent.sessions.errors import StateConflictError
from chagent.web.schemas import (ActiveRunPage, AppInfo, ApprovalDecision, ArtifactPage, Cancellation,
    CancelRequest, ConfigView, ContextView, DailyUsageView, ErrorResponse, HostLogin, MessagePage, RunAccepted, RunCreate, RunView, SessionCreate,
    SessionDetail, SessionEdit, SessionPage, SessionSources, UsageView)
from chagent.web.uploads import save as save_upload
from chagent.boundaries import validate_workspace
from chagent.domain.sessions import Session


SSE_POLL_INTERVAL = 1.0
TERMINAL_EVENTS = {"run_completed", "run_failed", "run_cancelled", "run_interrupted"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
#: The one write that is not JSON: the browser sends the file body directly, so
#: a 20 MiB document never has to become a base64 string inside a JSON envelope.
UPLOAD_PATH = "/api/uploads"
logger = logging.getLogger(__name__)


def reply(value, status=200, headers=None):
    return JSONResponse(jsonable_encoder(value), status_code=status,
                        headers={"Cache-Control": "no-store", **(headers or {})})


api = APIRouter(prefix="/api", responses={code: {"model": ErrorResponse} for code in (400, 403, 404, 409, 413, 415, 503)})


@api.post("/auth", include_in_schema=False)
async def host_login(value: HostLogin, request: Request):
    auth = getattr(request.app.state, "host_auth", None)
    if auth is None:
        raise HTTPException(404)
    session = auth.login(value.token)
    if session is None:
        raise HTTPException(401, "Invalid host UI token")
    response = reply({"authenticated": True})
    response.set_cookie(COOKIE, session, max_age=SESSION_SECONDS, httponly=True,
                        samesite="strict", path="/")
    return response


@api.get("/info", response_model=AppInfo, tags=["本地 Agent"])
async def info(current: Current):
    axes = current.settings.axes
    return {"workspace": current.workspace, "provider": current.runner.model.provider,
            "model": current.runner.model.id,
            "programmable": current.programmable,
            "boundary": axes.boundary, "capability": axes.capability, "gate": axes.gate,
            # Compatibility for the pre-unified page: there is no mode to pick.
            "code_mode": False}


@api.get("/config", response_model=ConfigView, tags=["本地 Agent"])
async def config(current: Current):
    """The resolved switches, so the browser is not blind to what the CLI prints."""
    settings = current.settings
    axes = settings.axes
    policy = current.compaction or CompactionPolicy.from_env()
    tokenizer = current.runner.context.counter.name
    # ``tools`` is the count of the face new sessions actually get: the demo
    # harness has one face, so the default-mode lookup falls back to it.
    tools = len(current.runner.tools.registry.definitions())
    from chagent.tools.builtin import WRITE_TOOLS
    work_writable = WRITE_TOOLS <= {tool.name for tool in current.runner.tools.registry.definitions()}
    return {
        "provider": current.runner.model.provider, "model": current.runner.model.id,
        "run_timeout": current.runner.timeout,
        "boundary": axes.boundary, "capability": axes.capability, "gate": axes.gate,
        "controller": settings.execution_profile,
        "sandbox_network": settings.sandbox_network,
        "workspace_writable": work_writable,
        "process_stdin": (settings.execution_profile != "host-controller" and
                          bool(getattr(current.runner.tools.registry.get("exec_command"), "process_stdin", False))),
        "approval": settings.approval, "approval_timeout": settings.approval_timeout,
        "approval_grace": settings.approval_unwatched_grace,
        "host_commands": current.runner.tools.allow_host_commands,
        "host_execution": current.runner.tools.registry.get("host_exec") is not None,
        "host_workspace_path": os.environ.get("CHAGENT_HOST_WORKSPACE_PATH", ""),
        "web_access": settings.web_access,
        "web_key": "set" if settings.search_api_key.strip() else "missing",
        "compaction": policy.enabled, "tokenizer": tokenizer,
        "max_turns": settings.max_turns, "max_tool_concurrency": settings.max_tool_concurrency,
        "max_processes": 0 if settings.execution_profile == "host-controller" else settings.max_processes,
        "process_timeout": settings.process_timeout,
        "code_mode": False, "programmable": current.programmable,
        "tools": tools,
        # Describe the actual work face; code capability is a separate field.
        "behavior": describe_behavior(settings, policy, tokenizer, workspace_writes=work_writable),
    }


@api.get("/sessions", response_model=SessionPage, tags=["会话"])
async def sessions(current: Current, limit: PageLimit = 50, before: UUID | None = None,
                   workspace: WorkspacePath = None,
                   archived: Literal["hide", "include", "only"] = "hide",
                   q: Annotated[str | None, Query(max_length=200)] = None):
    """Sessions of one workspace; archived ones are hidden unless asked for.

    ``q`` searches titles only. Message bodies are not searched: that would need
    an index over every payload and would answer a different question than the
    one the box in the sidebar asks.
    """
    return await current.sessions.list(workspace=current.root(workspace), before=before, limit=limit,
                                       archived=archived, query=q)


@api.get("/legacy/workspaces", tags=["会话"])
async def legacy_workspaces(current: Current):
    """Read-only inventory of old container-path workspaces awaiting proof of their host path."""
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Only the host controller has legacy bindings")
    async with current.runner.conversations.database.connection() as connection:
        cursor = await connection.execute(
            "SELECT w.id, w.root_path, b.state, count(s.id) AS sessions "
            "FROM workspace_bindings b JOIN workspaces w ON w.id=b.workspace_id "
            "LEFT JOIN sessions s ON s.workspace_id=w.id WHERE b.state='rebind_required' "
            "GROUP BY w.id,b.state ORDER BY w.root_path")
        return {"items": await cursor.fetchall()}


@api.get("/legacy/sessions/{session_id}/messages", tags=["会话"])
async def legacy_messages(session_id: UUID, current: Current, limit: MessageLimit = 100,
                          before: int | None = None):
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Only the host controller has legacy bindings")
    async with current.runner.conversations.database.connection() as connection:
        cursor = await connection.execute(
            "SELECT 1 FROM sessions s JOIN workspace_bindings b ON b.workspace_id=s.workspace_id "
            "WHERE s.id=%s AND b.state='rebind_required'", (session_id,))
        if await cursor.fetchone() is None:
            raise HTTPException(404, "Legacy session not found")
    return await current.runner.conversations.message_page(session_id, before=before, limit=limit)


@api.patch("/sessions/{session_id}", response_model=Session, tags=["会话"])
async def edit_session(session_id: UUID, value: SessionEdit, current: Current):
    """Rename or archive a session; the legacy mode is immutable."""
    session = await current.session(session_id)
    if value.title is not None:
        session = await current.sessions.rename(session_id, value.title)
    if value.archived is not None:
        session = (await current.sessions.archive(session_id) if value.archived
                   else await current.sessions.restore(session_id))
    return session


@api.get("/sessions/{session_id}/usage", response_model=UsageView, tags=["用量"])
async def session_usage(session_id: UUID, current: Current,
                        limit: Annotated[int, Query(ge=1, le=200)] = 50):
    await current.session(session_id)
    return await UsageRepository(current.runner.conversations.database).session(session_id, limit=limit)


@api.get("/usage", response_model=DailyUsageView, tags=["用量"])
async def usage(current: Current, days: Annotated[int, Query(ge=1, le=90)] = 7):
    """Tokens per day across the workspaces this process has opened."""
    return await UsageRepository(current.runner.conversations.database).daily(
        days=days, roots=sorted(str(root) for root in current.roots))


@api.post("/sessions", response_model=Session, status_code=201, tags=["会话"])
async def create_session(value: SessionCreate, current: Current):
    """Create a session with the unified programmable tool surface."""
    await current.require_programming(current.root(value.workspace))
    return await current.sessions.create(current.root(value.workspace), value.title, mode="work")


@api.post("/uploads", status_code=201, tags=["会话"])
async def upload(request: Request, current: Current, response: Response,
                 request_id: UUID | None = None,
                 name: Annotated[str, Query(min_length=1, max_length=300)] = "upload",
                 workspace: WorkspacePath = None):
    """Store one file under the workspace, where read_file can parse it."""
    root = current.root(workspace)
    try:
        from chagent.outputs import OutputService
        result = await save_upload(root, name, request.stream(), outputs=OutputService(current.runner.conversations.database),
                                   workspace_id=await current.workspace_id(root), request_id=request_id)
        if result['status'] != 'committed':
            response.status_code = 202
        return result
    except PermissionError:
        raise HTTPException(403, "上传目录不可写，请检查工作区权限后重试") from None
    except OSError as error:
        import errno
        if error.errno == errno.ENOSPC:
            raise HTTPException(507, "磁盘空间不足，请释放空间后重试上传") from None
        raise HTTPException(500, "无法保存上传文件，请检查工作区存储后重试") from None


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
                        "superseded_by": item.superseded_by,
                        "message": visible(item.message)} for item in page.items], "next_before": page.next_before}


@api.get("/sessions/{session_id}/files", tags=["上下文与产物"])
async def generated_files(session_id: UUID, current: Current):
    await current.session(session_id)
    async with current.runner.conversations.database.connection() as connection:
        outputs = await connection.execute("SELECT id,workspace_id,output_path AS path,size,status FROM file_outputs WHERE session_id=%s AND status='committed' ORDER BY created_at DESC", (session_id,))
        disk = [{**row, "name": Path(row["path"]).name} for row in await outputs.fetchall()]
        cursor = await connection.execute(
            "SELECT id, run_id, name, octet_length(content) AS size, created_at FROM generated_files "
            "WHERE session_id = %s ORDER BY created_at DESC, id DESC", (session_id,))
        return {"items": disk + await cursor.fetchall()}


@api.get("/sessions/{session_id}/files/{file_id}", tags=["上下文与产物"])
async def generated_file_download(session_id: UUID, file_id: UUID, current: Current):
    await current.session(session_id)
    async with current.runner.conversations.database.connection() as connection:
        cursor = await connection.execute("SELECT name, content FROM generated_files WHERE session_id = %s AND id = %s", (session_id, file_id))
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, "File does not exist in this session")
    return Response(content=row["content"].encode("utf-8"), media_type="application/octet-stream",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["name"], safe=""),
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@api.post('/sessions/{session_id}/files/{file_id}/export', tags=['上下文与产物'])
async def export_legacy(session_id: UUID, file_id: UUID, current: Current):
    from chagent.outputs import OutputService
    session = await current.session(session_id)
    async with current.runner.conversations.database.connection() as connection:
        cursor = await connection.execute('SELECT name,content FROM generated_files WHERE id=%s AND session_id=%s', (file_id,session_id))
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, '历史产物不存在')
    return await OutputService(current.runner.conversations.database).submit(session.workspace_id, session.workspace,
        row['name'],row['content'].encode('utf-8'),source='legacy-export',session_id=session_id)


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
    session = await current.session(session_id)
    supersede_from = await _replacement_point(current, session_id, value)
    return {"run_id": await current.start(session_id, value.text or "", supersede_from=supersede_from),
            "session_id": session_id}


async def _replacement_point(current: WebRuntime, session_id: UUID, value: RunCreate) -> int | None:
    """Which transcript record a regeneration or an edit starts over from.

    The sequence, not the id: retirement is a suffix, and a message cannot be
    replaced without the turns that followed it going stale too. Both refusals
    are 400 rather than 404: the session exists, the message does not belong to
    the kind of record the request names.
    """
    target_id = value.regenerate_from or value.edit_message_id
    if target_id is None:
        return None
    record = await current.sessions.message(session_id, target_id)
    if record is None:
        raise HTTPException(404, "消息不属于这个会话")
    expected = "assistant" if value.regenerate_from is not None else "user"
    if record.message.role != expected:
        raise HTTPException(400, "重新生成针对回答，编辑针对你自己的消息")
    if record.superseded_by is not None:
        raise HTTPException(409, "这条消息已经被替换过，刷新后再试")
    return record.sequence


@api.get("/sessions/{session_id}/sources", response_model=SessionSources, tags=["上下文与产物"])
async def session_sources(session_id: UUID, current: Current):
    """The memories each run of this session was given, for its 依据 section.

    One request per session rather than one per answer: memory is not a message,
    so the transcript cannot rebuild it from what it already has.
    """
    await current.session(session_id)
    return {"runs": await current.runner.conversations.run_memory_references(session_id)}


@api.get("/runs", response_model=ActiveRunPage, tags=["运行"])
async def active_runs(current: Current, active: bool = True):
    """Runs that have not converged yet across the opened workspaces.

    Only the active view exists: a list of every run ever would be a different
    endpoint with pagination, and nothing asks for it.
    """
    if not active:
        raise HTTPException(400, "Only active runs are listed; omit the parameter or pass active=true")
    rows = await current.runner.runs.active(roots=sorted(str(root) for root in current.roots))
    return {"items": [{"run_id": row["id"], "session_id": row["session_id"],
                        "started_at": row["started_at"], "current_tool": row["current_tool"]}
                       for row in rows]}


@api.get("/runs/{run_id}", response_model=RunView, tags=["运行"])
async def run_detail(run_id: UUID, current: Current):
    return await current.run(run_id)


@api.get("/runs/{run_id}/approvals", tags=["运行"])
async def run_approvals(run_id: UUID, current: Current):
    run = await current.run(run_id)
    pending = await current.approvals.pending(run_id)
    return {"waiting_for": "approval" if pending else None,
            "pending": [{"id": str(item.id), "tool_name": item.tool_name, "subject": item.subject,
                         "cwd": item.cwd, "expires_at": item.expires_at.isoformat(),
                         "status": item.status} for item in pending]}


@api.post("/approvals/{approval_id}/decide", tags=["运行"])
async def decide_approval(approval_id: UUID, value: ApprovalDecision, current: Current):
    """The only way to approve: a local UI request bound to this workspace."""
    approval = await current.approvals.get(approval_id)
    if approval is None:
        raise HTTPException(404, "Approval does not exist")
    run = await current.run(approval.run_id)
    status = await current.approvals.decide(approval_id, approve=value.approve, note=value.note)
    if status == "conflict":
        raise HTTPException(409, "Approval is no longer pending (decided, expired or the run ended)")
    return {"id": str(approval_id), "status": status, "run_id": str(run["id"])}


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
    async with current.runner.conversations.database.connection() as connection:
        await connection.execute("UPDATE file_outputs SET status='cancelled',payload=NULL WHERE run_id=%s AND status='pending'", (run_id,))
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
        # A viewer is counted for as long as this generator runs. The broker
        # reads the count to decide whether a waiting command still has a page
        # to be approved on; ``finally`` is what keeps a dropped connection from
        # leaving a phantom audience behind.
        current.watch(identity)
        try:
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
        finally:
            current.unwatch(identity)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
    })


async def asset(name: str = "index.html"):
    # An allow-list, not a directory read: the route must not become a way to
    # fetch anything else that happens to sit next to the page. Keys are the
    # only paths that exist for a client, so `..` and absolute paths have no
    # meaning here — they are simply not in the dict.
    media = {"index.html": "text/html", "main.js": "text/javascript", "app.js": "text/javascript", "file-editor.js": "text/javascript", "markdown.js": "text/javascript",
             "core/dom.js": "text/javascript", "core/api.js": "text/javascript",
             "style.css": "text/css", "api-docs.html": "text/html", "api-docs.js": "text/javascript"}
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
    if isinstance(error, (RunBusyError, SessionBusyError, SessionArchivedError, StateConflictError)):
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

        # The upload route reads its own body: it accepts any content type and
        # enforces its limit while streaming. Buffering it here would hold a
        # whole document in memory twice and reject it for not being JSON.
        if (scope["path"].startswith("/api/") and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}
                and not scope["path"].startswith(UPLOAD_PATH)):
            content = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                content.extend(message.get("body", b""))
                file_request = scope["path"].startswith("/api/workspaces/")
                body_limit = 6_400_000 if file_request else 65_536
                if len(content) > body_limit:
                    return await reply({"error": "Request exceeds body limit"}, 413)(scope, receive, send)
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
                defaults = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"),
                ]
                existing = {key.lower() for key, _ in message["headers"]}
                message["headers"] += [(key,value) for key,value in defaults if key not in existing]
            await send(message)

        await self.app(scope, receive, secured)


def create_app(*, workspace: Path, settings: Settings | None = None,
               model_settings: ModelSettings | None = None, demo: bool = False,
               injected_runtime: WebRuntime | None = None) -> FastAPI:
    """Injected runtime is caller-owned; production resources belong to lifespan."""
    workspace = validate_workspace(workspace)
    effective_settings = settings or (injected_runtime.settings if injected_runtime is not None else Settings.from_env())
    host_auth = HostAuth() if effective_settings.execution_profile == "host-controller" else None
    if host_auth is not None:
        host_auth.guard_workspace(workspace)

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
        if not demo:
            validate_web_settings(configured)
        selected = None if demo else (model_settings or ModelSettings.from_env())
        approval = ApprovalPolicy.from_settings(configured)
        client = create_model_client(selected) if selected is not None else None
        try:
            async with Database(configured) as database:
                await migrate(database)
                if configured.execution_profile == "host-controller":
                    from chagent.hostexec.native import mark_interrupted_native_executions
                    await mark_interrupted_native_executions(database)
                sandbox = Sandbox.from_settings(configured)
                web = WebAccess(configured.search_api_key) if configured.web_access != "off" else None
                if selected is None:
                    runner = create_demo_runner(database, work_only=True, allow_host_commands=False, max_turns=configured.max_turns,
                                                max_tool_concurrency=configured.max_tool_concurrency,
                                                memory_worker_concurrency=configured.memory_worker_concurrency,
                                                sandbox=sandbox, approval_policy=approval,
                                                approval_timeout=configured.approval_timeout,
                                                approval_grace=configured.approval_unwatched_grace,
                                                process_wall_timeout=configured.process_timeout,
                                                max_processes=configured.max_processes, web=web,
                                                allow_workspace_writes=True, versioned_files=True)
                else:
                    host_exec = None
                    if configured.execution_profile != "host-controller" and os.environ.get("CHAGENT_HOST_EXEC", "off") == "approval":
                        endpoint = os.environ.get("CHAGENT_HOST_ENDPOINT", "")
                        token = os.environ.get("CHAGENT_HOST_TOKEN", "")
                        if not endpoint or len(token) < 32:
                            raise ValueError("Host execution requires CHAGENT_HOST_ENDPOINT and a 32-character CHAGENT_HOST_TOKEN")
                        async with httpx.AsyncClient(timeout=2, trust_env=False) as probe:
                            response = await probe.get(endpoint.rstrip("/") + "/health",
                                                       headers={"Authorization": "Bearer " + token})
                        response.raise_for_status()
                        if not response.json().get("available"):
                            raise ValueError("Host executor has no interactive confirmation terminal")
                        host_exec = (endpoint, token)
                    runner = create_runner(database, selected.model(), client,
                                           allow_host_commands=configured.allow_host_commands,
                                           code_mode=configured.code_mode,
                                           unified=True,
                                           containerized=configured.axes.boundary == "container",
                                           # One writable file and command surface for both legacy rows.
                                           allow_workspace_writes=True, versioned_files=True,
                                           process_stdin=True,
                                           timeout=max(selected.run_timeout, 300) if host_exec else selected.run_timeout,
                                           max_turns=configured.max_turns,
                                           max_tool_concurrency=configured.max_tool_concurrency,
                                           memory_worker_concurrency=configured.memory_worker_concurrency,
                                           sandbox=sandbox, approval_policy=approval,
                                           approval_timeout=configured.approval_timeout,
                                           approval_grace=configured.approval_unwatched_grace,
                                           process_wall_timeout=configured.process_timeout,
                                           max_processes=configured.max_processes, web=web,
                                           host_exec=host_exec,
                                           native_host_exec=configured.execution_profile == "host-controller")
                app.state.runtime = WebRuntime(runner, workspace, configured, CompactionPolicy.from_env(),
                                               programmable=not demo)
                if host_auth is not None:
                    app.state.runtime.auth_guard = host_auth.guard_workspace
                try:
                    if not demo:
                        await app.state.runtime.require_programming(app.state.runtime.workspace)
                    app.state.runtime.start_worker()
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
                            SessionBusyError: application_error,
                            SessionArchivedError: application_error, ValueError: application_error,
                            LookupError: application_error, StateConflictError: application_error,
                            Exception: application_error},
    )
    app.state.host_auth = host_auth
    app.include_router(api)
    from chagent.web.file_api import router as file_router
    from chagent.web.memory_api import router as memory_router
    from chagent.web.workspace_api import router as workspace_router
    app.include_router(file_router)
    app.include_router(memory_router)
    app.include_router(workspace_router)
    app.add_api_route("/", asset, include_in_schema=False)
    app.add_api_route("/health", lambda: {"app": "chagent"}, include_in_schema=False)
    # `path` because the page loads ES modules from subdirectories (`core/…`).
    # The allow-list below is still the only thing that decides what exists.
    app.add_api_route("/static/{name:path}", asset, include_in_schema=False)
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
    if host_auth is not None:
        app.add_middleware(HostAuthBoundary, auth=host_auth)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    return app
