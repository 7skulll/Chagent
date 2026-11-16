"""Workspace listing and opening; registration grants no access on its own."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException

from chagent.boundaries import validate_workspace
from chagent.storage.repositories import ConversationRepository
from chagent.tools.context import ToolContext
from chagent.web import deps
from chagent.web.schemas import (ErrorResponse, WorkspaceCreate, WorkspaceDirectoryCreate,
                                 WorkspacePage, WorkspaceView)

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


@router.get("/containers")
async def containers(current: deps.Current):
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Container inventory requires the host controller")
    return {"items": await current.runner.sandbox.container_inventory(current.runner.runs.database)}


@router.get("/workspaces/{workspace_id}/executor")
async def workspace_executor(workspace_id: UUID, current: deps.Current):
    """One workspace's executor state; reads what is known and starts nothing."""
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Executor management requires the host controller")
    root = current.workspace_path(workspace_id)
    if root is None:
        raise HTTPException(404, "Workspace is not open")
    return current.executor_state(root) or {"state": "idle", "available": False, "reason": None}


@router.post("/workspaces/{workspace_id}/executor")
async def prepare_workspace_executor(workspace_id: UUID, current: deps.Current):
    """Create this directory's container now, without holding up the caller.

    A container is per workspace and used to be created inside the first turn,
    so the person waited for Docker mid-message and a missing image read as a
    broken agent. Preparing on open moves both the wait and the reason to an
    empty transcript. Idempotent: opening the same directory again joins the
    attempt already in flight.
    """
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Executor management requires the host controller")
    root = current.workspace_path(workspace_id)
    if root is None:
        raise HTTPException(404, "Workspace is not open")
    return current.schedule_provision(root) or {"state": "preparing", "available": False, "reason": None}


@router.get("/workspaces/{workspace_id}/container")
async def workspace_container(workspace_id: UUID, current: deps.Current):
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Container management requires the host controller")
    root = current.workspace_path(workspace_id)
    if root is None:
        raise HTTPException(404, "Workspace is not open")
    await current.workspace_id(root)
    detail = await current.runner.sandbox.container_detail(root)
    async with current.runner.runs.database.connection() as connection:
        cursor = await connection.execute(
            "SELECT last_used_at FROM workspace_containers WHERE workspace_id=%s", (workspace_id,))
        row = await cursor.fetchone()
    return {**detail, "idle_seconds": current.settings.container_idle_seconds,
            "last_used_at": row["last_used_at"] if row else None}


@router.post("/workspaces/{workspace_id}/container/rebuild")
async def rebuild_workspace_container(workspace_id: UUID, current: deps.Current):
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Container management requires the host controller")
    root = current.workspace_path(workspace_id)
    if root is None:
        raise HTTPException(404, "Workspace is not open")
    await current.require_programming(root)
    try:
        name = await current.runner.sandbox.rebuild_container(root, workspace_id, current.runner.runs.database)
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {"container_name": name, "state": "running"}


@router.post("/workspaces", response_model=WorkspaceView, status_code=201)
async def open_workspace(value: WorkspaceCreate, current: deps.Current):
    """Open one directory; from then on requests may name it as their workspace."""
    return await deps.open_workspace(value, current)


@router.post("/workspaces/mkdir", response_model=WorkspaceView, status_code=201)
async def create_workspace_directory(value: WorkspaceDirectoryCreate, current: deps.Current):
    """Make a folder inside the allowed tree and open it as a workspace.

    Opening and creating are different verbs and the dialog only had the first.
    Under account isolation the allowed tree starts empty, so "create a new
    workspace" had no path at all: every directory that already existed was
    outside what the account may open. The server creates the folder inside a
    directory it has already opened, so the guard runs before ``mkdir`` rather
    than after — nothing is left behind when the answer is no.
    """
    name = value.name.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(400, "文件夹名不能为空，且不能包含路径分隔符")
    parent = current.open_workspace(value.parent)
    target = parent / name
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        raise HTTPException(409, "同名文件夹已存在") from None
    except OSError:
        raise HTTPException(400, "无法在该目录创建文件夹") from None
    return await deps.open_workspace(WorkspaceCreate(path=str(target)), current)


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
    root = current.workspace_path(workspace_id)
    if root is None:
        raise HTTPException(404, "Workspace is not open")
    # Status is read-only: the normal programming preflight would call
    # ensure_container() and restart a stopped container before we inspect it.
    await current.workspace_id(root)
    site = current.runner.tools.registry.get("site")
    result = await site.execute("web-status", {"action": "status"}, context=ToolContext(workspace=root))
    return result.details


@router.post("/workspaces/{workspace_id}/site/stop")
async def stop_workspace_site(workspace_id: UUID, current: deps.Current):
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Site management requires the host controller")
    root = current.workspace_path(workspace_id)
    if root is None:
        raise HTTPException(404, "Workspace is not open")
    await current.require_programming(root)
    site = current.runner.tools.registry.get("site")
    result = await site.execute("web-stop", {"action": "stop"}, context=ToolContext(workspace=root))
    return result.details


@router.post("/workspaces/{workspace_id}/site/restart")
async def restart_workspace_site(workspace_id: UUID, current: deps.Current):
    if current.settings.execution_profile != "host-controller":
        raise HTTPException(404, "Site management requires the host controller")
    root = current.workspace_path(workspace_id)
    if root is None:
        raise HTTPException(404, "Workspace is not open")
    await current.require_programming(root)
    async with current.runner.runs.database.connection() as connection:
        cursor = await connection.execute(
            "SELECT command,internal_port,status FROM workspace_services WHERE workspace_id=%s", (workspace_id,))
        saved = await cursor.fetchone()
    if not saved or not saved["command"]:
        raise HTTPException(409, "No saved website command is available")
    site = current.runner.tools.registry.get("site")
    result = await site.execute("web-restart", {"action": "start", "command": saved["command"],
                        "port": saved["internal_port"]}, context=ToolContext(workspace=root))
    if result.is_error:
        raise HTTPException(409, result.content[0].text)
    return result.details
