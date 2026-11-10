"""Single-process local web application, sharing the existing agent and storage.

This module assembles: the routers live in ``info_api``, ``sessions_api``,
``context_api``, ``runs_api`` (and the older ``file_api``/``workspace_api``/
``memory_api``), the dependencies they share live in ``deps``, and the JSON
reply shape lives in ``responses``. What is left here is the lifespan, the
request boundary, the error mapping and the static asset allow-list.
"""

from contextlib import asynccontextmanager
from importlib.resources import files
import logging
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from chagent.bootstrap import create_demo_runner, create_runner
from chagent.boundaries import validate_workspace
from chagent.config import Settings
from chagent.domain.summaries import CompactionPolicy
from chagent.models.factory import create_model_client
from chagent.models.settings import ModelSettings
from chagent.sessions.errors import SessionArchivedError, SessionBusyError, SessionNotFoundError, StateConflictError
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import RunBusyError
from chagent.tools.policy import ApprovalPolicy
from chagent.tools.sandbox import Sandbox
from chagent.tools.web import WebAccess
from chagent.web.auth import HostAuth, HostAuthBoundary
from chagent.web.context_api import router as context_router
from chagent.web.file_api import router as file_router
from chagent.web.info_api import router as info_router
from chagent.web.memory_api import router as memory_router
from chagent.web.policy import validate_web_settings
from chagent.web.responses import reply
from chagent.web.runtime import WebRuntime
from chagent.web.runs_api import router as runs_router
from chagent.web.sessions_api import UPLOAD_PATH
from chagent.web.sessions_api import router as sessions_router
from chagent.web.workspace_api import router as workspace_router

logger = logging.getLogger(__name__)


#: The only paths a client may fetch under /static/. A dict literal rather than a
#: directory read, so adding a module is an explicit line here — and so
#: tests/test_web_ui.py can prove the page's module graph is fully listed.
ASSETS = {
    "index.html": "text/html", "api-docs.html": "text/html", "style.css": "text/css",
    "main.js": "text/javascript", "app.js": "text/javascript",
    "file-editor.js": "text/javascript", "markdown.js": "text/javascript",
    "api-docs.js": "text/javascript",
    "core/api.js": "text/javascript", "core/clipboard.js": "text/javascript",
    "core/dom.js": "text/javascript", "core/format.js": "text/javascript",
    "core/markdown.js": "text/javascript", "core/store.js": "text/javascript",
    "run/run-reducer.js": "text/javascript", "run/run-render.js": "text/javascript",
    "render/approval.js": "text/javascript", "render/files.js": "text/javascript",
    "render/message.js": "text/javascript", "render/run-summary.js": "text/javascript",
    "render/sources.js": "text/javascript",
    "render/usage.js": "text/javascript",
    "views/chat.js": "text/javascript", "views/config.js": "text/javascript",
    "views/context.js": "text/javascript", "views/generated.js": "text/javascript",
    "views/memory.js": "text/javascript", "views/sessions.js": "text/javascript",
    "views/drafts.js": "text/javascript", "views/layout.js": "text/javascript",
    "views/preview.js": "text/javascript",
    "views/uploads.js": "text/javascript", "views/operations.js": "text/javascript",
    "views/workspace.js": "text/javascript",
}


async def asset(name: str = "index.html"):
    # An allow-list, not a directory read: the route must not become a way to
    # fetch anything else that happens to sit next to the page. Keys are the
    # only paths that exist for a client, so `..` and absolute paths have no
    # meaning here — they are simply not in the dict.
    if name not in ASSETS:
        raise HTTPException(404)
    return Response(files("chagent.web").joinpath("static", name).read_bytes(), media_type=ASSETS[name],
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
                                                wall_timeout=configured.run_wall_timeout,
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
                                           wall_timeout=configured.run_wall_timeout,
                                           approval_grace=configured.approval_unwatched_grace,
                                           process_wall_timeout=configured.process_timeout,
                                           max_processes=configured.max_processes, web=web,
                                           host_exec=host_exec,
                                           native_host_exec=configured.execution_profile == "host-controller",
                                           defer_tools=configured.deferred_tools)
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
    # Registration order is the order FastAPI tries a path in; keep the api
    # routes in the order they were written in when they shared one module.
    for mounted in (info_router, sessions_router, context_router, runs_router,
                    file_router, memory_router, workspace_router):
        app.include_router(mounted)
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
