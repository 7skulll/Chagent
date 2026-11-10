"""Application identity, the resolved switches, and host-UI login.

These three are the routes that describe the process rather than a session, so
they are the only ones that read ``app.state`` instead of the runtime.
"""

import os
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from chagent.diagnostics.behavior import describe as describe_behavior
from chagent.domain.summaries import CompactionPolicy
from chagent.web.auth import COOKIE, SESSION_SECONDS
from chagent.web.deps import Current
from chagent.web.responses import ERROR_RESPONSES, reply
from chagent.web.schemas import AppInfo, ConfigView, HostLogin

router = APIRouter(prefix="/api", responses=ERROR_RESPONSES)


@router.post("/auth", include_in_schema=False)
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


@router.get("/info", response_model=AppInfo, tags=["本地 Agent"])
async def info(current: Current):
    axes = current.settings.axes
    return {"workspace": current.workspace, "provider": current.runner.model.provider,
            "model": current.runner.model.id,
            "programmable": current.programmable,
            "boundary": axes.boundary, "capability": axes.capability, "gate": axes.gate,
            # Compatibility for the pre-unified page: there is no mode to pick.
            "code_mode": False}


@router.get("/config", response_model=ConfigView, tags=["本地 Agent"])
async def config(current: Current, workspace_id: UUID | None = None):
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
    container = None
    if workspace_id is not None and settings.execution_profile == "host-controller":
        root = next((root for root in current.roots if current._workspace_ids.get(root) == workspace_id), None)
        if root is not None:
            await current.workspace_id(root)
            container = await current.runner.sandbox.container_detail(root)
    return {
        "provider": current.runner.model.provider, "model": current.runner.model.id,
        "run_timeout": current.runner.timeout,
        "run_wall_timeout": current.runner.wall_timeout,
        "boundary": axes.boundary, "capability": axes.capability, "gate": axes.gate,
        "controller": settings.execution_profile,
        "sandbox_network": settings.sandbox_network,
        "workspace_writable": work_writable,
        "process_stdin": (settings.execution_profile != "host-controller" and
                          bool(getattr(current.runner.tools.registry.get("exec_command"), "process_stdin", False))),
        "approval": settings.approval, "approval_timeout": settings.approval_timeout,
        "approval_grace": settings.approval_unwatched_grace,
        "container_idle_seconds": settings.container_idle_seconds,
        "container_state": container["state"] if container else None,
        "image_stale": container["image_stale"] if container else None,
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
