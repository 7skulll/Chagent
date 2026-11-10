"""Session lifecycle, transcript pages, usage and uploads.

The session is the unit a browser holds on to: one exists before any run does,
and its id is what the run, event and artifact endpoints hang off.
"""

from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response

from chagent.domain.artifacts import artifact_text
from chagent.domain.codec import message_to_dict
from chagent.domain.messages import ToolResultMessage
from chagent.storage.usage import UsageRepository
from chagent.web.deps import Current, MessageLimit, PageLimit, WorkspacePath
from chagent.web.responses import ERROR_RESPONSES
from chagent.web.schemas import (DailyUsageView, MessagePage, Session, SessionCreate, SessionDetail,
                                 SessionEdit, SessionPage, UsageView)
from chagent.web.uploads import save as save_upload

router = APIRouter(prefix="/api", responses=ERROR_RESPONSES)

#: The one write that is not JSON: the browser sends the file body directly, so
#: a 20 MiB document never has to become a base64 string inside a JSON envelope.
#: ``LocalBoundary`` in ``app`` exempts this path from body buffering, so the
#: constant lives next to the route that owns it.
UPLOAD_PATH = "/api/uploads"


@router.get("/sessions", response_model=SessionPage, tags=["会话"])
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


@router.get("/legacy/workspaces", tags=["会话"])
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


@router.get("/legacy/sessions/{session_id}/messages", tags=["会话"])
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


@router.patch("/sessions/{session_id}", response_model=Session, tags=["会话"])
async def edit_session(session_id: UUID, value: SessionEdit, current: Current):
    """Rename or archive a session; the legacy mode is immutable."""
    session = await current.session(session_id)
    if value.title is not None:
        session = await current.sessions.rename(session_id, value.title)
    if value.archived is not None:
        session = (await current.sessions.archive(session_id) if value.archived
                   else await current.sessions.restore(session_id))
    return session


@router.get("/sessions/{session_id}/usage", response_model=UsageView, tags=["用量"])
async def session_usage(session_id: UUID, current: Current,
                        limit: Annotated[int, Query(ge=1, le=200)] = 50):
    await current.session(session_id)
    return await UsageRepository(current.runner.conversations.database).session(session_id, limit=limit)


@router.get("/usage", response_model=DailyUsageView, tags=["用量"])
async def usage(current: Current, days: Annotated[int, Query(ge=1, le=90)] = 7):
    """Tokens per day across the workspaces this process has opened."""
    return await UsageRepository(current.runner.conversations.database).daily(
        days=days, roots=sorted(str(root) for root in current.roots))


@router.post("/sessions", response_model=Session, status_code=201, tags=["会话"])
async def create_session(value: SessionCreate, current: Current):
    """Create a session with the unified programmable tool surface."""
    await current.require_programming(current.root(value.workspace))
    return await current.sessions.create(current.root(value.workspace), value.title, mode="work")


@router.post("/uploads", status_code=201, tags=["会话"])
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


@router.get("/sessions/{session_id}", response_model=SessionDetail, tags=["会话"])
async def session_detail(session_id: UUID, current: Current):
    session = await current.session(session_id)
    return {**asdict(session), "latest_run": await current.runner.runs.latest(session_id)}


@router.get("/sessions/{session_id}/messages", response_model=MessagePage, tags=["会话"])
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
