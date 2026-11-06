"""Execution boundary description for a process already placed in a container.

The filesystem and shell tools keep this interface so their run lifecycle does
not depend on a particular deployment. No in-process kernel sandbox is applied.
"""

from dataclasses import dataclass
from hashlib import sha256
import asyncio
import json
import os
from pathlib import Path
import shutil
from typing import Sequence

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
        self._run_containers: dict[str, str] = {}

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
            return name

    async def wrap(self, argv: Sequence[str], workspace: Path, *, owner=None,
                   workdir: Path | None = None, timeout_seconds: float | None = None) -> tuple[list[str], dict]:
        policy = self.policy(workspace, owner=owner)
        if not policy.containerized:
            raise PermissionError("Command execution requires the container boundary")
        if self.settings.host_controller:
            name = await self.ensure_container(policy.workspace)
            if owner is None:
                raise ValueError("Container command requires a run owner")
            scratch = f"/tmp/chagent-run-{owner}"
            self._run_containers[str(owner)] = name
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
        self._run_containers.pop(str(owner), None)

    def close(self) -> None:
        pass
