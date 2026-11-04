"""Workspace listing and opening; registration grants no access on its own."""

from pathlib import Path

from fastapi import APIRouter

from chagent.boundaries import validate_workspace
from chagent.storage.repositories import ConversationRepository
from chagent.web import deps
from chagent.web.schemas import ErrorResponse, WorkspaceCreate, WorkspacePage, WorkspaceView

router = APIRouter(
    prefix="/api", tags=["本地 Agent"],
    responses={code: {"model": ErrorResponse} for code in (400, 403, 404, 409, 413, 415, 503)},
)


@router.get("/workspaces", response_model=WorkspacePage)
async def workspaces(current: deps.Current):
    """Known workspaces, including unavailable directories; registration grants no access."""
    rows = await ConversationRepository(current.runner.conversations.database).workspaces()
    items = []
    for row in rows:
        try:
            validate_workspace(Path(row["path"]))
        except (ValueError, OSError, RuntimeError):
            row.update(available=False, reason="目录不存在、无权访问或不符合工作区准入要求")
        else:
            row.update(available=True, reason=None)
        items.append(row)
    return {"items": items}


@router.post("/workspaces", response_model=WorkspaceView, status_code=201)
async def open_workspace(value: WorkspaceCreate, current: deps.Current):
    """Open one directory; from then on requests may name it as their workspace."""
    return await deps.open_workspace(value, current)
