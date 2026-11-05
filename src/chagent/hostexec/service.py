"""Small host-only approval service. Start it in an interactive terminal."""

import argparse
import asyncio
from collections import deque
import hmac
import io
import json
import os
from pathlib import Path
import select
import signal
import sys
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
import uvicorn

from chagent.boundaries import validate_workspace
from chagent.config.dotenv import load_missing
from chagent.tools.workspace import WorkspaceFS


class HostRequest(BaseModel):
    workspace: str = Field(min_length=1, max_length=4096)
    workdir: str = Field(default=".", max_length=4096)
    command: str = Field(min_length=1, max_length=32768)
    purpose: str = Field(min_length=1, max_length=1000)
    run_id: str = Field(min_length=1, max_length=100)
    call_id: str = Field(min_length=1, max_length=100)
    timeout_ms: int = Field(default=30000, ge=1, le=120000)
    verification: dict | None = None


class SwitchRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


def host_directory(root: Path, container_root: Path, workspace: str, workdir: str) -> Path:
    source = Path(workspace)
    try:
        relative = source.relative_to(container_root)
    except ValueError:
        raise ValueError("Workspace is outside the container mount") from None
    if any(part in {"..", "."} for part in relative.parts):
        raise ValueError("Invalid workspace")
    fs = WorkspaceFS(root)
    parts = fs.parts(str(relative))
    with fs.directory(parts):
        pass
    target = validate_workspace(root.joinpath(*parts))
    project = WorkspaceFS(target)
    subparts = project.parts(workdir)
    with project.directory(subparts):
        pass
    return target.joinpath(*subparts)


def create_service(*, host_root: Path, container_root: Path, token: str, input_stream=None,
                   output_stream=None, audit_path: Path | None = None) -> FastAPI:
    if not token or len(token) < 32:
        raise ValueError("Host token must contain at least 32 characters")
    root = host_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Host workspace root must be a directory")
    reader = input_stream or sys.stdin
    writer = output_stream or sys.stderr
    audit = audit_path or (Path.home() / "Library/Logs/chagent/hostexec.jsonl" if sys.platform == "darwin"
                           else Path.home() / ".local/state/chagent/hostexec.jsonl")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    lock = asyncio.Lock()
    attempts = deque()
    seen: set[tuple[str, str]] = set()
    if audit.is_file():
        for line in audit.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if row.get("run_id") and row.get("call_id"):
                    seen.add((row["run_id"], row["call_id"]))
            except (ValueError, TypeError):
                continue

    def record(row: dict):
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
            file.flush()
            os.fsync(file.fileno())

    def rate_limit():
        now = monotonic()
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 20:
            raise HTTPException(429, "Too many host requests")
        attempts.append(now)

    async def confirmation():
        def read():
            try:
                descriptor = reader.fileno()
            except (AttributeError, OSError, io.UnsupportedOperation):
                return reader.readline()
            ready, _, _ = select.select([descriptor], [], [], 120)
            return reader.readline() if ready else ""
        return await asyncio.to_thread(read)

    @app.get("/health")
    async def health(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401)
        return {"available": reader.isatty() and writer.isatty()}

    @app.post("/workspace/switch")
    async def switch_workspace(request: SwitchRequest, authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401)
        raise HTTPException(503, "Workspace switching is disabled: remounting under /workspaces aliases persisted sessions")

    @app.post("/execute")
    async def execute(request: HostRequest, authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401)
        if not reader.isatty() or not writer.isatty():
            raise HTTPException(503, "Host confirmation requires an interactive terminal")
        async with lock:
            rate_limit()
            key = (request.run_id, request.call_id)
            if key in seen:
                raise HTTPException(409, "This host request was already handled; inspect the host audit log")
            seen.add(key)
            try:
                cwd = host_directory(root, container_root, request.workspace, request.workdir)
            except (ValueError, OSError) as error:
                seen.discard(key)
                raise HTTPException(400, str(error)) from None
            approval_id = uuid4().hex
            evidence = request.verification or {}
            writer.write(f"\n[Chagent host request {approval_id}]\nRun: {request.run_id}  Call: {request.call_id}\nPurpose: {request.purpose}\nDirectory: {cwd}\nCommand: {request.command}\nContainer verification (reported by container): {evidence.get('tool', 'none')} exit={evidence.get('exit_code', 'unknown')}\n{str(evidence.get('output', ''))[:2000]}\nType {approval_id} to approve; anything else denies: ")
            writer.flush()
            answer = await confirmation()
            if answer.strip() != approval_id:
                record({"id": approval_id, "run_id": request.run_id, "call_id": request.call_id,
                        "cwd": str(cwd), "command": request.command, "status": "denied"})
                return {"id": approval_id, "status": "denied", "started": False}
            # Resolve and validate again after a human may have left the prompt open.
            try:
                cwd = host_directory(root, container_root, request.workspace, request.workdir)
            except (ValueError, OSError) as error:
                raise HTTPException(400, str(error)) from None
            record({"id": approval_id, "run_id": request.run_id, "call_id": request.call_id,
                    "cwd": str(cwd), "command": request.command, "status": "approved"})
            env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "SHELL") if key in os.environ}
            process = await asyncio.create_subprocess_exec("/bin/sh", "-c", request.command,
                cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
            output = bytearray()
            truncated = False

            async def collect():
                nonlocal truncated
                while chunk := await process.stdout.read(4096):
                    room = max(0, 65536 - len(output))
                    output.extend(chunk[:room])
                    truncated = truncated or len(chunk) > room

            collection = asyncio.create_task(collect())
            try:
                try:
                    await asyncio.wait_for(process.wait(), request.timeout_ms / 1000)
                    status = "completed"
                except asyncio.TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                    status = "timed_out"
            except asyncio.CancelledError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                await collection
                record({"id": approval_id, "status": "cancelled", "exit_code": process.returncode})
                raise
            await collection
            text = output.decode("utf-8", errors="replace")
            record({"id": approval_id, "run_id": request.run_id, "call_id": request.call_id,
                    "status": status, "exit_code": process.returncode})
            return {"id": approval_id, "status": status, "started": True,
                    "exit_code": process.returncode, "output": text,
                    "truncated": truncated}
    return app


def main():
    parser = argparse.ArgumentParser(description="Chagent host execution approval agent")
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--container-root", type=Path, default=Path("/workspaces"))
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parents[3] / ".env")
    args = parser.parse_args()
    load_missing(args.env_file)
    token = os.environ.get("CHAGENT_HOST_TOKEN", "")
    if not sys.stdin.isatty():
        parser.error("Start this service in an interactive host terminal")
    uvicorn.run(create_service(host_root=args.host_root, container_root=args.container_root,
                               token=token), host=args.bind, port=args.port)


if __name__ == "__main__":
    main()
