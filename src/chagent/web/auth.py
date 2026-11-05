"""Browser session gate for a host controller reachable from Docker Desktop."""

import hmac
import os
from pathlib import Path
import secrets
import stat
from time import time
from http.cookies import SimpleCookie

from starlette.responses import JSONResponse


SESSION_SECONDS = 24 * 60 * 60
COOKIE = "chagent_host_session"


class HostAuth:
    def __init__(self, path: Path | None = None):
        self.path = (path or Path.home() / "Library/Application Support/Chagent/ui-token").expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="ascii") as file:
                file.write(secrets.token_urlsafe(32))
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Host UI token must be an owner-only regular file")
        self.token = self.path.read_text(encoding="ascii").strip()
        if len(self.token) < 32:
            raise ValueError("Host UI token is invalid")
        self.sessions: dict[str, float] = {}

    def guard_workspace(self, workspace: Path) -> None:
        root = workspace.expanduser().resolve(strict=True)
        if self.path.resolve().is_relative_to(root):
            raise ValueError("Workspace contains the host UI credential directory")

    def login(self, candidate: str) -> str | None:
        if not hmac.compare_digest(candidate, self.token):
            return None
        session = secrets.token_urlsafe(32)
        self.sessions[session] = time() + SESSION_SECONDS
        return session

    def valid(self, raw_cookie: str) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except Exception:
            return False
        item = cookie.get(COOKIE)
        if item is None:
            return False
        supplied = item.value
        for known, expires in list(self.sessions.items()):
            if expires <= time():
                self.sessions.pop(known, None)
            elif hmac.compare_digest(supplied, known):
                return True
        return False


class HostAuthBoundary:
    def __init__(self, app, auth: HostAuth):
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/") \
                and scope["path"] != "/api/auth":
            headers = dict(scope["headers"])
            if not self.auth.valid(headers.get(b"cookie", b"").decode("latin-1")):
                return await JSONResponse({"error": "请通过本机 Chagent 启动器打开网页完成认证"}, 401)(scope, receive, send)
        return await self.app(scope, receive, send)
