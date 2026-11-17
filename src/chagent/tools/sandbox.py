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
from uuid import UUID, uuid4

from chagent.tools.container_policy import ContainerPolicy

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


class ContainerCleanupPending(RuntimeError):
    """Execution may have effects beyond the end of the tool call."""


class Sandbox:
    """Keep the tool wrapper API while execution is provided by deployment."""

    def __init__(self, settings: SandboxSettings):
        self.settings = settings
        self.enabled = settings.containerized
        self._locks: dict[str, asyncio.Lock] = {}
        self._lease_locks: dict[str, asyncio.Lock] = {}
        self._site_uses: dict[str, int] = {}
        self._run_containers: dict[str, str] = {}
        self.container_policy = ContainerPolicy()
        self._environments: dict[str, dict] = {}
        self._cleanup_pending: set[str] = set()
        self.database = None
        self.owner_guard = None

    def _lease_lock(self, name: str) -> asyncio.Lock:
        return self._lease_locks.setdefault(name, asyncio.Lock())

    @asynccontextmanager
    async def use(self, workspace: Path):
        """Keep GC away from a site operation, including its startup window."""
        if self.owner_guard is not None:
            self.owner_guard(Path(workspace))
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
        stale = await self.image_stale(image_id)
        state = "stale_image" if stale else "ready"
        async with self.database.connection() as connection:
            await connection.execute(
                "INSERT INTO workspace_containers (workspace_id,container_name,image_id,state,last_used_at) "
                "SELECT w.id,%s,%s,%s,now() FROM workspaces w "
                "JOIN workspace_bindings b ON b.workspace_id=w.id AND b.state='active' "
                "WHERE w.root_path=%s ON CONFLICT (workspace_id) DO UPDATE SET "
                "container_name=EXCLUDED.container_name,image_id=EXCLUDED.image_id,"
                "state=EXCLUDED.state,last_used_at=now()",
                (name, image_id, state, str(root)))

    async def image_stale(self, container_image_id: str) -> bool:
        image = os.environ.get("CHAGENT_EXECUTOR_IMAGE", "chagent:local")
        code, current = await self._docker("image", "inspect", image, "--format", "{{.Id}}")
        if code:
            raise RuntimeError("Configured executor image is unavailable")
        return current.strip() != container_image_id

    async def container_detail(self, workspace: Path) -> dict:
        root = validate_workspace(Path(workspace))
        if self.owner_guard is not None:
            self.owner_guard(root)
        name = self.container_name(root)
        code, raw = await self._docker("inspect", name, "--format", "{{json .}}")
        if code:
            return {"container_name": name, "state": "missing", "image_stale": False,
                    "image_id": None, "running": False}
        details = json.loads(raw)
        image_id = details.get("Image", "")
        return {"container_name": name,
                "state": "running" if details.get("State", {}).get("Running") else "stopped",
                "image_stale": await self.image_stale(image_id), "image_id": image_id,
                "running": bool(details.get("State", {}).get("Running")),
                "started_at": details.get("State", {}).get("StartedAt")}

    async def rebuild_container(self, workspace: Path, workspace_id, database) -> str:
        """Explicit replacement; refuse all active work and retain the cache volume."""
        root = validate_workspace(Path(workspace))
        if self.owner_guard is not None:
            self.owner_guard(root)
        name = self.container_name(root)
        async with self._lease_lock(name):
            if self._site_uses.get(name) or name in self._run_containers.values():
                raise RuntimeError("Workspace container is in use")
            async with self._locks.setdefault(name, asyncio.Lock()):
                async with database.connection() as connection:
                    cursor = await connection.execute(
                        "SELECT EXISTS (SELECT 1 FROM runs r JOIN sessions s ON s.id=r.session_id "
                        "WHERE s.workspace_id=%s AND r.status IN ('queued','running')) AS active_run, "
                        "EXISTS (SELECT 1 FROM workspace_services "
                        "WHERE workspace_id=%s AND status IN ('starting','running')) AS active_site",
                        (workspace_id, workspace_id))
                    guard = await cursor.fetchone()
                if guard["active_run"] or guard["active_site"]:
                    raise RuntimeError("Stop the active run or website before rebuilding")
                image = os.environ.get("CHAGENT_EXECUTOR_IMAGE", "chagent:local")
                image_code, _ = await self._docker("image", "inspect", image, "--format", "{{.Id}}")
                if image_code:
                    raise RuntimeError("Configured executor image is unavailable")
                code, raw = await self._docker("inspect", name, "--format", "{{json .}}")
                if code == 0:
                    details = json.loads(raw)
                    expected_hash = sha256(os.fsencode(root)).hexdigest()
                    mounts = details.get("Mounts") or []
                    if (details.get("Config", {}).get("Labels", {}).get("chagent.workspace") != expected_hash
                            or not any(m.get("Type") == "bind" and m.get("Source") == str(root)
                                       and m.get("Destination") == str(root) for m in mounts)):
                        raise RuntimeError("Container identity does not match this workspace")
                    if details.get("State", {}).get("Running") and await self._site_worker_running(name):
                        raise RuntimeError("Stop the website before rebuilding")
                    await self._remove_container(name, "Could not remove old workspace container: ")
                self._cleanup_pending.discard(name)
                async with database.connection() as connection:
                    await connection.execute(
                        "UPDATE container_invocations SET cleanup_confirmed=true WHERE container_name=%s", (name,))
                    await connection.execute("UPDATE workspace_containers SET state='reaped',cleanup_pending=false WHERE workspace_id=%s",
                                             (workspace_id,))
        return await self.ensure_container(root)

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
                "image": os.environ.get("CHAGENT_EXECUTOR_IMAGE", "chagent:local") if policy.containerized else None,
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

    async def ensure_container(self, workspace: Path, *, allow_cleanup_pending: bool = False) -> str:
        if not self.settings.host_controller:
            raise RuntimeError("Per-workspace containers require host-controller mode")
        root = validate_workspace(Path(workspace))
        if self.owner_guard is not None:
            self.owner_guard(root)
        name = self.container_name(root)
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            if self.database is not None:
                async with self.database.connection() as connection:
                    cursor = await connection.execute(
                        "SELECT cleanup_pending FROM workspace_containers WHERE container_name=%s", (name,))
                    row = await cursor.fetchone()
                    if row and row['cleanup_pending'] and not allow_cleanup_pending:
                        raise RuntimeError("Container cleanup_pending; explicitly rebuild after stopping active work")
                    cursor = await connection.execute(
                        "SELECT i.invocation FROM container_invocations i JOIN runs r ON r.id=i.run_id "
                        "WHERE i.container_name=%s AND NOT i.cleanup_confirmed "
                        "AND r.status NOT IN ('queued','running') LIMIT 1", (name,))
                    if await cursor.fetchone() and not allow_cleanup_pending:
                        raise RuntimeError("Container cleanup_pending; unfinished invocation from a terminal run")
            status, output = await self._docker("inspect", name, "--format", "{{json .}}")
            if status == 0:
                details = json.loads(output)
                differences = self.container_policy.differences(details, root,
                    os.environ.get("CHAGENT_EXECUTOR_IMAGE", "chagent:local"))
                if differences:
                    raise RuntimeError("Existing workspace container does not match the required isolation policy: "
                                       + ", ".join(differences) + "; explicitly rebuild after stopping active work")
                if name in self._cleanup_pending and not allow_cleanup_pending:
                    raise RuntimeError("Container cleanup_pending; explicitly rebuild after stopping active work")
                await self._remember_environment(name, root, details)
                if not details.get("State", {}).get("Running"):
                    code, message = await self._docker("start", name)
                    if code:
                        raise RuntimeError("Could not restart workspace container: " + message[-400:])
                await self._touch_container(root, name, details.get("Image", ""))
                return name
            if allow_cleanup_pending:
                raise RuntimeError("Workspace container is absent; no service to stop")
            image = os.environ.get("CHAGENT_EXECUTOR_IMAGE", "chagent:local")
            code, message = await self._docker("run", "-d", "--name", name,
                *self.container_policy.create_args(root, image), timeout=60)
            if code:
                raise RuntimeError("Could not create workspace container: " + message[-400:])
            code, raw = await self._docker("inspect", name, "--format", "{{json .}}")
            if code:
                raise RuntimeError("Could not inspect new workspace container")
            details = json.loads(raw)
            differences = self.container_policy.differences(details, root, image)
            if differences:
                raise RuntimeError("New workspace container violates policy: " + ", ".join(differences))
            await self._remember_environment(name, root, details)
            await self._touch_container(root, name, details["Image"])
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
            invocation = uuid4().hex
            control = f"{scratch}/calls/{invocation}"
            from chagent.hostexec import command_worker
            source = Path(command_worker.__file__).read_text()
            if self.database is not None:
                async with self.database.connection() as connection:
                    await connection.execute(
                        "INSERT INTO container_invocations (invocation,container_name,run_id,control_path,environment) "
                        "VALUES (%s,%s,%s,%s,%s::jsonb)",
                        (UUID(invocation), name, owner, control, json.dumps(self._environments[name])))
            return (["docker", "exec", "--workdir", str(cwd),
                     "--env", "PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
                     "--env", "HOME=" + scratch, "--env", "TMPDIR=" + scratch,
                     "--env", "PYTHONPATH=" + str(policy.workspace),
                     name, "/app/.venv/bin/python", "-I", "-c", source,
                     "run", control, str(timeout_seconds or 600), *argv],
                    {**self.record(policy), "container": name, "invocation": invocation,
                     "control_path": control, "environment": self._environments.get(name)})
        return list(argv), self.record(policy)

    async def _remember_environment(self, name, root, details):
        manifest = self.container_policy.manifest(details, root)
        if not manifest['container_id'] or not manifest['image_id']:
            raise RuntimeError("Container environment identity unavailable")
        code, raw = await self._docker("image", "inspect", details['Image'], "--format", "{{json .RepoDigests}}")
        manifest['repo_digests'] = json.loads(raw) if code == 0 and raw.strip() else None
        manifest['image_stale'] = await self.image_stale(details['Image'])
        self._environments[name] = manifest

    async def finish_invocation(self, record):
        if not self.settings.host_controller or not record.get('invocation'):
            return
        name = record['container']
        from chagent.hostexec import command_worker
        source = Path(command_worker.__file__).read_text()
        try:
            code, output = await self._docker('exec', name, '/app/.venv/bin/python', '-I', '-c', source,
                                              'stop', record['control_path'], timeout=12)
            if code:
                raise RuntimeError(output[-300:])
        except (OSError, RuntimeError):
            record['cleanup_confirmed'] = False
            self._cleanup_pending.add(name)
            if self.database is not None:
                async with self.database.connection() as connection:
                    await connection.execute(
                        "UPDATE workspace_containers SET cleanup_pending=true WHERE container_name=%s", (name,))
            raise ContainerCleanupPending('Container cleanup_pending; invocation exit could not be confirmed') from None
        if self.database is not None:
            async with self.database.connection() as connection:
                await connection.execute(
                    "UPDATE container_invocations SET cleanup_confirmed=true WHERE invocation=%s AND container_name=%s",
                    (UUID(record['invocation']), name))
        record['cleanup_confirmed'] = True


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
        if name in self._cleanup_pending:
            raise RuntimeError("Container cleanup_pending; run scratch retained")
        if self.database is not None:
            async with self.database.connection() as connection:
                cursor = await connection.execute(
                    "SELECT invocation,control_path FROM container_invocations "
                    "WHERE run_id=%s AND NOT cleanup_confirmed", (owner,))
                outstanding = await cursor.fetchall()
            for row in outstanding:
                await self.finish_invocation({'container': name, 'invocation': str(row['invocation']),
                                              'control_path': row['control_path']})
        path = f"/tmp/chagent-run-{owner}"
        code, output = await self._docker("exec", name, "rm", "-rf", "--", path)
        if code:
            raise OSError("Could not remove run scratch: " + output[-200:])
        async with self._lease_lock(name):
            self._run_containers.pop(str(owner), None)

    async def _remove_container(self, name: str, failure: str) -> None:
        """Delete a container; absence afterwards is the success condition.

        Two controllers share one Docker daemon. The loser of a race sees either
        "no such container" or "removal already in progress", and neither is a
        failure of its own cycle. Only a container that survives the wait is.
        """
        code, output = await self._docker("rm", "-f", name)
        if code == 0:
            return
        for _ in range(10):
            status, _ = await self._docker("inspect", name, "--format", "{{.Id}}")
            if status != 0:
                return
            await asyncio.sleep(0.2)
        raise RuntimeError(failure + output[-200:])

    async def _scoped_container_names(self, database, output):
        names = output.splitlines()
        if self.owner_guard is None:
            return names
        async with database.connection() as connection:
            cursor = await connection.execute("SELECT host_path FROM workspace_bindings")
            rows = await cursor.fetchall()
        allowed = set()
        for row in rows:
            try:
                self.owner_guard(Path(row['host_path']))
            except (PermissionError, ValueError, OSError):
                continue
            allowed.add('chagent-ws-' + sha256(os.fsencode(row['host_path'])).hexdigest()[:20])
        # Missing from this tenant's DB is *not* evidence that another user's
        # Docker container is an orphan. Do not inspect or execute in it either.
        return [name for name in names if name in allowed]

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
        for name in await self._scoped_container_names(database, output):
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
                async with database.connection() as connection:
                    cursor = await connection.execute(
                        "SELECT invocation FROM container_invocations WHERE run_id=%s AND NOT cleanup_confirmed",
                        (owner,))
                    if await cursor.fetchone():
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
            if self.owner_guard is not None:
                try:
                    self.owner_guard(root)
                except (PermissionError, ValueError, OSError):
                    continue
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
                    await self._remove_container(name, "Could not reap workspace container: ")
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
        for name in await self._scoped_container_names(database, output):
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
                    await self._remove_container(name, "Could not reap orphan container: ")
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
        for name in await self._scoped_container_names(database, output):
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
