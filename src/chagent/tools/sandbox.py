"""Execution boundary description for a process already placed in a container.

The filesystem and shell tools keep this interface so their run lifecycle does
not depend on a particular deployment. No in-process kernel sandbox is applied.
"""

from dataclasses import dataclass
from contextlib import asynccontextmanager
from hashlib import sha256
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
from typing import Sequence
from uuid import UUID

from chagent.boundaries import validate_workspace


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    mode: str = "off"
    network: bool = True
    writable_workspace: bool = True
    containerized: bool = False
    host_controller: bool = False

    def __post_init__(self) -> None:
        if self.mode != "off":
            raise ValueError("In-process sandbox profiles have been removed; run Chagent in a container")
        if not isinstance(self.writable_workspace, bool):
            raise ValueError("writable_workspace must be boolean")

    @property
    def allows_execution(self) -> bool:
        """The runtime half of ``boundary=container`` + ``capability=full``.

        One predicate, so the tool assembly, the web entry and the runner
        cannot each spell out a slightly different subset of these three flags.
        """
        return self.containerized and self.mode == "off" and self.writable_workspace


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    workspace: Path
    network: bool = True
    writable_workspace: bool = True
    containerized: bool = False


@dataclass(frozen=True, slots=True)
class SandboxSupport:
    available: bool
    backend: str
    checks: dict[str, bool]
    detail: str = ""


class Sandbox:
    """Keep the tool wrapper API while execution is provided by deployment."""

    def __init__(self, settings: SandboxSettings):
        self.settings = settings
        self.enabled = settings.containerized
        self._locks: dict[str, asyncio.Lock] = {}
        self._lease_locks: dict[str, asyncio.Lock] = {}
        self._site_uses: dict[str, int] = {}
        self._run_containers: dict[str, str] = {}
        self.database = None

    def _lease_lock(self, name: str) -> asyncio.Lock:
        return self._lease_locks.setdefault(name, asyncio.Lock())

    @asynccontextmanager
    async def use(self, workspace: Path):
        """Keep GC away from a site operation, including its startup window."""
        name = self.container_name(workspace)
        async with self._lease_lock(name):
            self._site_uses[name] = self._site_uses.get(name, 0) + 1
        try:
            yield
        finally:
            async with self._lease_lock(name):
                remaining = self._site_uses[name] - 1
                if remaining:
                    self._site_uses[name] = remaining
                else:
                    self._site_uses.pop(name, None)

    async def _touch_container(self, root: Path, name: str, image_id: str) -> None:
        if self.database is None:
            return
        async with self.database.connection() as connection:
            await connection.execute(
                "INSERT INTO workspace_containers (workspace_id,container_name,image_id,state,last_used_at) "
                "SELECT w.id,%s,%s,'ready',now() FROM workspaces w "
                "JOIN workspace_bindings b ON b.workspace_id=w.id AND b.state='active' "
                "WHERE w.root_path=%s ON CONFLICT (workspace_id) DO UPDATE SET "
                "container_name=EXCLUDED.container_name,image_id=EXCLUDED.image_id,state='ready',last_used_at=now()",
                (name, image_id, str(root)))

    @classmethod
    def from_settings(cls, settings) -> "Sandbox":
        return cls(SandboxSettings(
            mode=settings.sandbox,
            network=settings.sandbox_network,
            writable_workspace=settings.sandbox_writable,
            containerized=settings.axes.boundary == "container",
            host_controller=settings.execution_profile == "host-controller",
        ))

    def policy(self, workspace: Path, *, owner=None) -> SandboxPolicy:
        return SandboxPolicy(validate_workspace(Path(workspace)), self.settings.network,
                             self.settings.writable_workspace, self.settings.containerized)

    def environment(self, workspace: Path, *, owner=None) -> dict[str, str] | None:
        if self.settings.host_controller:
            return {**{key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ},
                    "TMPDIR": f"/tmp/chagent-run-{owner}" if owner is not None else "/tmp"}
        if self.settings.containerized:
            # Commands need toolchains, never the agent's API keys or DB URL.
            return {"PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8"}
        return None

    def record(self, policy: SandboxPolicy) -> dict:
        return {"boundary": "container" if policy.containerized else "none",
                "network": policy.network, "workspace_writable": policy.writable_workspace,
                "image": os.environ.get("CHAGENT_IMAGE", "chagent:local") if policy.containerized else None,
                **({"backend": "docker-per-workspace"} if self.settings.host_controller else {})}

    @staticmethod
    def container_name(workspace: Path) -> str:
        root = validate_workspace(Path(workspace))
        return "chagent-ws-" + sha256(os.fsencode(root)).hexdigest()[:20]

    @staticmethod
    async def _docker(*args: str, timeout: float = 20) -> tuple[int, str]:
        if shutil.which("docker") is None:
            raise RuntimeError("Docker CLI is unavailable")
        child = await asyncio.create_subprocess_exec("docker", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(child.communicate(), timeout)
        except TimeoutError:
            child.kill()
            await child.communicate()
            raise RuntimeError("Docker operation timed out") from None
        return child.returncode, output.decode(errors="replace")

    async def ensure_container(self, workspace: Path) -> str:
        if not self.settings.host_controller:
            raise RuntimeError("Per-workspace containers require host-controller mode")
        root = validate_workspace(Path(workspace))
        name = self.container_name(root)
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            status, output = await self._docker("inspect", name, "--format", "{{json .}}")
            if status == 0:
                details = json.loads(output)
                mounts = details.get("Mounts", [])
                host = details.get("HostConfig", {})
                published = (host.get("PortBindings") or {}).get("18080/tcp") or []
                valid_mount = any(m.get("Type") == "bind" and m.get("Source") == str(root)
                                  and m.get("Destination") == str(root) and m.get("RW") for m in mounts)
                safe_mounts = all((m.get("Type") == "bind" and m.get("Source") == str(root)
                                   and m.get("Destination") == str(root)) or
                                  (m.get("Type") == "volume" and m.get("Destination") == "/cache")
                                  for m in mounts)
                if (not valid_mount or not safe_mounts or not host.get("ReadonlyRootfs")
                        or details.get("Config", {}).get("Image") != os.environ.get("CHAGENT_EXECUTOR_IMAGE", "chagent:local")
                        or host.get("Privileged") or host.get("NetworkMode") != "bridge"
                        or host.get("PidMode") or host.get("IpcMode") != "private"
                        or "ALL" not in (host.get("CapDrop") or [])
                        or "no-new-privileges" not in (host.get("SecurityOpt") or [])
                        or host.get("Memory", 0) > 2_147_483_648 or host.get("Memory", 0) <= 0
                        or host.get("PidsLimit", 0) > 256 or host.get("PidsLimit", 0) <= 0
                        or len(published) != 1 or published[0].get("HostIp") != "127.0.0.1"):
                    raise RuntimeError("Existing workspace container does not match the required isolation policy")
                if not details.get("State", {}).get("Running"):
                    code, message = await self._docker("start", name)
                    if code:
                        raise RuntimeError("Could not restart workspace container: " + message[-400:])
                await self._touch_container(root, name, details.get("Image", ""))
                return name
            image = os.environ.get("CHAGENT_EXECUTOR_IMAGE", "chagent:local")
            cache = "chagent-cache-" + sha256(os.fsencode(root)).hexdigest()[:20]
            code, message = await self._docker("run", "-d", "--name", name,
                "--label", "chagent.workspace=" + sha256(os.fsencode(root)).hexdigest(),
                "--mount", f"type=bind,src={root},dst={root}",
                "--mount", f"type=volume,src={cache},dst=/cache",
                "--workdir", str(root), "--publish", "127.0.0.1::18080",
                "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=512m",
                "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp",
                "--env", "MAVEN_OPTS=-Dmaven.repo.local=/cache/m2",
                "--env", "PIP_CACHE_DIR=/cache/pip", "--env", "npm_config_cache=/cache/npm",
                "--entrypoint", "/bin/sleep", image, "infinity", timeout=60)
            if code:
                raise RuntimeError("Could not create workspace container: " + message[-400:])
            code, image_id = await self._docker("inspect", name, "--format", "{{.Image}}")
            if code:
                raise RuntimeError("Could not inspect new workspace container")
            await self._touch_container(root, name, image_id.strip())
            return name

    async def wrap(self, argv: Sequence[str], workspace: Path, *, owner=None,
                   workdir: Path | None = None, timeout_seconds: float | None = None) -> tuple[list[str], dict]:
        policy = self.policy(workspace, owner=owner)
        if not policy.containerized:
            raise PermissionError("Command execution requires the container boundary")
        if self.settings.host_controller:
            if owner is None:
                raise ValueError("Container command requires a run owner")
            name = self.container_name(policy.workspace)
            async with self._lease_lock(name):
                self._run_containers[str(owner)] = name
            try:
                name = await self.ensure_container(policy.workspace)
                async with self._lease_lock(name):
                    self._run_containers[str(owner)] = name
            except BaseException:
                async with self._lease_lock(name):
                    self._run_containers.pop(str(owner), None)
                raise
            scratch = f"/tmp/chagent-run-{owner}"
            cwd = validate_workspace(workdir or policy.workspace)
            if cwd != policy.workspace and not cwd.is_relative_to(policy.workspace):
                raise PermissionError("Command directory is outside workspace")
            bounded = (["/usr/bin/timeout", "-k", "2s", f"{max(.001, timeout_seconds - .5):.3f}s", *argv]
                       if timeout_seconds is not None else list(argv))
            return (["docker", "exec", "--workdir", str(cwd),
                     "--env", "PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
                     "--env", "HOME=" + scratch, "--env", "TMPDIR=" + scratch,
                     "--env", "PYTHONPATH=" + str(policy.workspace),
                     name, "/bin/sh", "-c", 'mkdir -p -m 700 "$1" && shift && exec "$@"',
                     "chagent-run", scratch, *bounded],
                    {**self.record(policy), "container": name})
        return list(argv), self.record(policy)

    async def support(self, policy: SandboxPolicy) -> SandboxSupport:
        if self.settings.host_controller:
            try:
                await self.ensure_container(policy.workspace)
            except (RuntimeError, ValueError, OSError) as error:
                return SandboxSupport(False, "docker", {"workspace_container": False}, str(error))
            return SandboxSupport(True, "docker", {"workspace_container": True})
        return SandboxSupport(policy.containerized, "container" if policy.containerized else "none", {},
                              "" if policy.containerized else "container boundary is required")

    async def audit(self, workspace: Path, *, owner=None) -> dict:
        policy = self.policy(workspace, owner=owner)
        support = await self.support(policy)
        return {**self.record(policy), "available": support.available,
                "checks": support.checks, "detail": support.detail}

    async def release(self, owner) -> None:
        name = self._run_containers.get(str(owner))
        if name is None:
            return
        path = f"/tmp/chagent-run-{owner}"
        code, output = await self._docker("exec", name, "rm", "-rf", "--", path)
        if code:
            raise OSError("Could not remove run scratch: " + output[-200:])
        async with self._lease_lock(name):
            self._run_containers.pop(str(owner), None)

    async def reap_terminal_scratch(self, database) -> int:
        """Remove only scratch directories belonging to terminal database runs.

        A missing row is not proof that another controller is done with a run.
        Container and directory names are restricted before passing them to Docker.
        """
        if not self.settings.host_controller:
            return 0
        code, output = await self._docker("ps", "--filter", "label=chagent.workspace", "--format", "{{.Names}}")
        if code:
            raise RuntimeError("Could not list workspace containers: " + output[-200:])
        removed = 0
        for name in output.splitlines():
            if not re.fullmatch(r"chagent-ws-[0-9a-f]{20}", name):
                continue
            code, listing = await self._docker("exec", "--workdir", "/tmp", name, "ls", "-1", "/tmp")
            if code:
                continue
            candidates = []
            for entry in listing.splitlines():
                if not entry.startswith("chagent-run-"):
                    continue
                try:
                    owner = UUID(entry.removeprefix("chagent-run-"))
                except ValueError:
                    continue
                if entry != f"chagent-run-{owner}" or str(owner) in self._run_containers:
                    continue
                candidates.append(owner)
            if not candidates:
                continue
            async with database.connection() as connection:
                cursor = await connection.execute(
                    "SELECT id FROM runs WHERE id = ANY(%s) AND status IN ('completed','failed','cancelled','interrupted')",
                    (candidates,))
                terminal = {row["id"] for row in await cursor.fetchall()}
            for owner in candidates:
                if owner not in terminal:
                    continue
                status, _ = await self._docker("exec", name, "rm", "-rf", "--", f"/tmp/chagent-run-{owner}")
                removed += status == 0
        return removed

    async def reap_idle_containers(self, database, idle_seconds: int) -> int:
        """Reap registered idle containers after rechecking leases and identity."""
        if not self.settings.host_controller or idle_seconds == 0:
            return 0
        async with database.connection() as connection:
            cursor = await connection.execute(
                "SELECT c.workspace_id,c.container_name,b.host_path,b.device,b.inode "
                "FROM workspace_containers c JOIN workspace_bindings b ON b.workspace_id=c.workspace_id "
                "WHERE b.state='active' AND c.state <> 'reaped' "
                "AND c.last_used_at < now() - make_interval(secs => %s)", (idle_seconds,))
            candidates = await cursor.fetchall()
        reaped = 0
        for row in candidates:
            name, root = row["container_name"], Path(row["host_path"])
            if not re.fullmatch(r"chagent-ws-[0-9a-f]{20}", name):
                continue
            async with self._lease_lock(name):
                if self._site_uses.get(name) or name in self._run_containers.values():
                    continue
                async with self._locks.setdefault(name, asyncio.Lock()):
                    # A command may have touched the row after the first query.
                    async with database.connection() as connection:
                        cursor = await connection.execute(
                            "SELECT c.last_used_at < now() - make_interval(secs => %s) AS expired, "
                            "EXISTS (SELECT 1 FROM runs r JOIN sessions s ON s.id=r.session_id "
                            "WHERE s.workspace_id=c.workspace_id AND r.status IN ('queued','running')) AS active_run, "
                            "EXISTS (SELECT 1 FROM workspace_services ws WHERE ws.workspace_id=c.workspace_id "
                            "AND ws.status IN ('starting','running')) AS active_site "
                            "FROM workspace_containers c WHERE c.workspace_id=%s AND c.container_name=%s",
                            (idle_seconds, row["workspace_id"], name))
                        guard = await cursor.fetchone()
                    if guard is None or not guard["expired"] or guard["active_run"] or guard["active_site"]:
                        continue
                    try:
                        stat = root.stat()
                    except OSError:
                        continue
                    if (stat.st_dev, stat.st_ino) != (row["device"], row["inode"]):
                        continue
                    code, output = await self._docker("inspect", name, "--format", "{{json .}}")
                    if code:
                        # Externally removed: reconcile the database, no Docker deletion.
                        async with database.connection() as connection:
                            await connection.execute("UPDATE workspace_containers SET state='reaped' WHERE workspace_id=%s",
                                                     (row["workspace_id"],))
                        continue
                    details = json.loads(output)
                    expected_hash = sha256(os.fsencode(root)).hexdigest()
                    mounts = details.get("Mounts") or []
                    if (details.get("Config", {}).get("Labels", {}).get("chagent.workspace") != expected_hash
                            or name != "chagent-ws-" + expected_hash[:20]
                            or not any(m.get("Type") == "bind" and m.get("Source") == str(root)
                                       and m.get("Destination") == str(root) for m in mounts)):
                        continue
                    if await self._site_worker_running(name):
                        continue
                    if details.get("State", {}).get("Running"):
                        code, processes = await self._docker("top", name)
                        if code or len(processes.splitlines()) > 2:
                            continue
                    code, output = await self._docker("rm", "-f", name)
                    if code:
                        raise RuntimeError("Could not reap workspace container: " + output[-200:])
                    async with database.connection() as connection:
                        await connection.execute("UPDATE workspace_containers SET state='reaped' WHERE workspace_id=%s",
                                                 (row["workspace_id"],))
                    reaped += 1
        return reaped

    async def _site_worker_running(self, name: str) -> bool:
        """Conservatively protect a live site even if its DB row is stale."""
        for path in ("/cache/site/chagent-site.json", "/tmp/chagent-site.json"):
            code, raw = await self._docker("exec", "--workdir", "/tmp", name, "cat", path)
            if code:
                continue
            try:
                state = json.loads(raw)
                if state.get("state") not in {"running", "starting"}:
                    return False
                pid = str(state["worker_pid"])
                code, stat = await self._docker("exec", "--workdir", "/tmp", name,
                                                "cat", f"/proc/{pid}/stat")
                if code:
                    return False
                start = stat.rsplit(") ", 1)[1].split()[19]
                return not state.get("worker_start") or start == state["worker_start"]
            except (ValueError, KeyError, IndexError):
                return True
        return False

    async def reap_orphan_containers(self, database) -> int:
        """Remove unbound Chagent containers only when ownership is unambiguous."""
        if not self.settings.host_controller:
            return 0
        code, output = await self._docker("ps", "-a", "--filter", "label=chagent.workspace", "--format", "{{.Names}}")
        if code:
            raise RuntimeError("Could not list workspace containers: " + output[-200:])
        removed = 0
        for name in output.splitlines():
            if not re.fullmatch(r"chagent-ws-[0-9a-f]{20}", name):
                continue
            async with self._lease_lock(name):
                if self._site_uses.get(name) or name in self._run_containers.values():
                    continue
                async with self._locks.setdefault(name, asyncio.Lock()):
                    code, raw = await self._docker("inspect", name, "--format", "{{json .}}")
                    if code:
                        continue
                    details = json.loads(raw)
                    mounts = details.get("Mounts") or []
                    binds = [m for m in mounts if m.get("Type") == "bind"]
                    if len(binds) != 1 or len(mounts) != 2 or not any(
                            m.get("Type") == "volume" and m.get("Destination") == "/cache" for m in mounts):
                        continue
                    root = binds[0].get("Source", "")
                    if binds[0].get("Destination") != root or not root.startswith("/"):
                        continue
                    expected_hash = sha256(os.fsencode(root)).hexdigest()
                    if (name != "chagent-ws-" + expected_hash[:20]
                            or details.get("Config", {}).get("Labels", {}).get("chagent.workspace") != expected_hash):
                        continue
                    async with database.connection() as connection:
                        cursor = await connection.execute(
                            "SELECT b.state, "
                            "EXISTS (SELECT 1 FROM runs r JOIN sessions s ON s.id=r.session_id "
                            "WHERE s.workspace_id=w.id AND r.status IN ('queued','running')) AS active_run, "
                            "EXISTS (SELECT 1 FROM workspace_services ws WHERE ws.workspace_id=w.id "
                            "AND ws.status IN ('starting','running')) AS active_site "
                            "FROM workspaces w LEFT JOIN workspace_bindings b ON b.workspace_id=w.id "
                            "WHERE w.root_path=%s", (root,))
                        row = await cursor.fetchone()
                    if row is not None and (row["state"] == "active" or row["active_run"] or row["active_site"]):
                        continue
                    if details.get("State", {}).get("Running"):
                        code, listing = await self._docker("exec", "--workdir", "/tmp", name, "ls", "-1", "/tmp")
                        if code or any(re.fullmatch(r"chagent-run-[0-9a-f-]{36}", entry)
                                       for entry in listing.splitlines()):
                            continue
                        code, state = await self._docker("exec", "--workdir", "/tmp", name, "cat", "/cache/site/chagent-site.json")
                        if code:
                            code, state = await self._docker("exec", "--workdir", "/tmp", name, "cat", "/tmp/chagent-site.json")
                        if code == 0:
                            try:
                                if json.loads(state).get("state") in {"running", "starting"}:
                                    continue
                            except ValueError:
                                continue
                        code, processes = await self._docker("top", name)
                        if code or len(processes.splitlines()) > 2:
                            # The managed base container has only sleep infinity.
                            continue
                    code, message = await self._docker("rm", "-f", name)
                    if code:
                        raise RuntimeError("Could not reap orphan container: " + message[-200:])
                    removed += 1
        return removed

    async def container_inventory(self, database) -> list[dict]:
        """Read-only labeled Docker inventory for the host controller."""
        if not self.settings.host_controller:
            return []
        async with database.connection() as connection:
            cursor = await connection.execute("SELECT host_path,state FROM workspace_bindings")
            bindings = {sha256(os.fsencode(row["host_path"])).hexdigest(): row["state"]
                        for row in await cursor.fetchall()}
        code, output = await self._docker("ps", "-a", "--filter", "label=chagent.workspace", "--format", "{{.Names}}")
        if code:
            raise RuntimeError("Could not list workspace containers: " + output[-200:])
        items = []
        for name in output.splitlines():
            if not re.fullmatch(r"chagent-ws-[0-9a-f]{20}", name):
                continue
            code, raw = await self._docker("inspect", name, "--format", "{{json .}}")
            if code:
                continue
            details = json.loads(raw)
            label = details.get("Config", {}).get("Labels", {}).get("chagent.workspace", "")
            if not re.fullmatch(r"[0-9a-f]{64}", label) or name != "chagent-ws-" + label[:20]:
                continue
            items.append({"name": name, "running": bool(details.get("State", {}).get("Running")),
                          "binding": bindings.get(label, "missing"),
                          "orphan_candidate": bindings.get(label) != "active"})
        return items

    def close(self) -> None:
        pass
