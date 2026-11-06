"""Workspace listing and opening; registration grants no access on its own."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException

from chagent.boundaries import validate_workspace
from chagent.storage.repositories import ConversationRepository
from chagent.tools.context import ToolContext
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
    bindings = {}
    if current.settings.execution_profile == "host-controller":
        async with current.runner.conversations.database.connection() as connection:
            cursor = await connection.execute("SELECT workspace_id,state FROM workspace_bindings")
            bindings = {row["workspace_id"]: row["state"] for row in await cursor.fetchall()}
    items = []
    for row in rows:
        if current.settings.execution_profile == "host-controller" and bindings.get(row["id"]) != "active":
            row.update(available=False, reason="旧容器路径待核实原宿主目录，不能执行")
            items.append(row)
            continue
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


@router.post("/workspaces/switch-host")
async def switch_host_workspace(value: WorkspaceCreate, current: deps.Current):
    """The host controller opens a new host path without remounting the old one."""
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(503, "当前容器主控不能跨宿主目录切换；请启动宿主主控")
    return await deps.open_workspace(value, current)


@router.get("/workspaces/{workspace_id}/site")
async def workspace_site(workspace_id: UUID, current: deps.Current):
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Site management requires the host controller")
    root = next((root for root in current.roots if current._workspace_ids.get(root) == workspace_id), None)
    if root is None:
        raise HTTPException(404, "Workspace is not open")
    await current.require_programming(root)
    site = current.runner.tools.registry.get("site")
    result = await site.execute("web-status", {"action": "status"}, context=ToolContext(workspace=root))
    return result.details


@router.post("/workspaces/{workspace_id}/site/stop")
async def stop_workspace_site(workspace_id: UUID, current: deps.Current):
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Site management requires the host controller")
    root = next((root for root in current.roots if current._workspace_ids.get(root) == workspace_id), None)
    if root is None:
        raise HTTPException(404, "Workspace is not open")
    await current.require_programming(root)
    site = current.runner.tools.registry.get("site")
    result = await site.execute("web-stop", {"action": "stop"}, context=ToolContext(workspace=root))
    return result.details
