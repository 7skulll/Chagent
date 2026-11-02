"""A platform profile wrapped around a host command, not around this process.

The file tools already confine themselves with directory descriptors and
no-follow operations (``workspace.py``). The surface they cannot cover is a host
command: once ``/bin/sh`` starts, every path check that lived in Python is gone.
This module adds the one boundary that survives ``exec`` — a kernel profile.

The legacy workspace-write mode uses targeted denials and is not read isolation.
The isolated mode is deny-default with explicit runtime/workspace reads, private
scratch and a synthetic HOME/environment. It must pass positive and negative
controls before execution; it never falls back to the host. This is a macOS
kernel profile, not a VM or protection from the host account itself.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import sys
import tempfile
from typing import Protocol, Sequence


from chagent.boundaries import runtime_roots, validate_workspace, protected_source


MODES = ("off", "workspace-write", "isolated")
DEFAULT_DENY_READ = (
    "~/.ssh", "~/.aws", "~/.kube", "~/.docker",
    "~/.config/gh", "~/.config/gcloud", "~/.npmrc", "~/.pypirc", "~/.git-credentials",
)
SEATBELT_PATH = "/usr/bin/sandbox-exec"
PROBE_TIMEOUT = 3.0


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """Host configuration, before a workspace is known."""

    mode: str = "off"
    network: bool = False
    # ``None`` means the built-in credential paths; ``()`` means protect nothing.
    deny_read: tuple[Path, ...] | None = None
    writable_tmp: bool = True
    writable_workspace: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.writable_workspace, bool):
            raise ValueError("writable_workspace must be boolean")
        if self.mode not in MODES:
            raise ValueError(f"Sandbox mode must be one of {MODES}")
        if self.mode == "isolated" and self.network:
            raise ValueError("isolated execution requires network=false; network authorization is not implemented")
        source = DEFAULT_DENY_READ if self.deny_read is None else self.deny_read
        resolved = tuple(dict.fromkeys(Path(item).expanduser().resolve() for item in source))
        object.__setattr__(self, "deny_read", resolved)


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Everything a profile depends on; also the probe cache key."""

    workspace: Path
    network: bool = False
    deny_read: tuple[Path, ...] = ()
    writable_tmp: bool = True
    isolated: bool = False
    scratch: Path | None = None
    writable_workspace: bool = True

    def __post_init__(self) -> None:
        # A denied path that contains the workspace, or sits inside it, would
        # make ordinary edits fail. Dropping it is the only safe resolution:
        # silently keeping it turns a misconfiguration into a broken session.
        denied = tuple(item for item in self.deny_read
                       if item != self.workspace
                       and not self.workspace.is_relative_to(item)
                       and not item.is_relative_to(self.workspace))
        object.__setattr__(self, "deny_read", denied)


@dataclass(frozen=True, slots=True)
class SandboxSupport:
    available: bool
    backend: str
    checks: dict[str, bool]
    detail: str = ""


class SandboxBackend(Protocol):
    name: str

    def wrap(self, argv: Sequence[str], policy: SandboxPolicy) -> list[str]: ...


class DisabledBackend:
    """No profile: commands run with the host's full authority."""

    name = "none"

    def wrap(self, argv: Sequence[str], policy: SandboxPolicy) -> list[str]:
        return list(argv)


class SeatbeltBackend:
    """macOS seatbelt.

    ``sandbox-exec`` execs into the child, so the pid taken by
    ``create_subprocess_exec`` is the command's own pid. ``start_new_session``
    and the existing ``killpg`` cancellation path therefore keep working without
    changes; the leader only lingers as a zombie until it is reaped, which
    ``communicate()`` already does.
    """

    name = "seatbelt"

    def __init__(self, binary: str = SEATBELT_PATH):
        self.binary = binary

    @property
    def available(self) -> bool:
        return os.path.isfile(self.binary) and os.access(self.binary, os.X_OK)

    def wrap(self, argv: Sequence[str], policy: SandboxPolicy) -> list[str]:
        return [self.binary, "-p", profile(policy), "--", *argv]


def _sbpl(value: Path | str) -> str:
    """Quote one SBPL string. Unquoted values are parsed as variables."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _tmp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def profile(policy: SandboxPolicy) -> str:
    """Render the seatbelt profile for one policy.

    Allow-everything then deny is deliberate: the current implementation uses targeted write and read denials. One tested
    read allowlist candidate aborted before the shell started; see C0 diagnostics.
    """
    if policy.isolated:
        return isolated_profile(policy)
    lines = ["(version 1)", "(allow default)"]
    if not policy.network:
        lines.append("(deny network-outbound)")
    lines += ["(deny file-write*)", "(allow file-write*",
              '  (literal "/dev/null")',
              '  (literal "/dev/stdout")',
              '  (literal "/dev/stderr")']
    if policy.writable_workspace:
        lines.append(f"  (subpath {_sbpl(policy.workspace)})")
    if policy.writable_tmp and not policy.writable_workspace and (policy.workspace.is_relative_to(_tmp_root()) or _tmp_root().is_relative_to(policy.workspace)):
        raise PermissionError("Read-only workspace overlaps writable temporary directory")
    if policy.writable_tmp:
        # TMPDIR is per-user; the shared /private/tmp is not granted because
        # leftovers there are reachable by other processes.
        lines.append(f"  (subpath {_sbpl(_tmp_root())})")
    lines.append(")")
    if policy.deny_read:
        lines.append("(deny file-read*")
        lines += [f"  (subpath {_sbpl(item)})" for item in policy.deny_read]
        lines.append(")")
    return "\n".join(lines) + "\n"


async def _run(argv: Sequence[str], timeout: float = PROBE_TIMEOUT, *, env: dict[str, str] | None = None, cwd: Path | None = None) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv, env=env, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        return process.returncode if process.returncode is not None else -1, output.decode("utf-8", errors="replace")
    except (asyncio.TimeoutError, TimeoutError):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, 9)
        with suppress(Exception):
            await process.communicate()
        return -1, ""


class Sandbox:
    """One configured backend, its probe results, and the audit record."""

    def __init__(self, settings: SandboxSettings, backend: SandboxBackend | None = None):
        self.settings = settings
        self.backend = backend or (SeatbeltBackend() if settings.mode != "off" else DisabledBackend())
        if settings.mode == "isolated" and not isinstance(self.backend, SeatbeltBackend):
            raise ValueError("Isolated execution requires the seatbelt backend; no fallback is allowed")
        self.enabled = not isinstance(self.backend, DisabledBackend)
        self._support: dict[SandboxPolicy, SandboxSupport] = {}
        self._lock = asyncio.Lock()
        self._scratch: dict[tuple[Path, object], tempfile.TemporaryDirectory] = {}

    def close(self) -> None:
        for temporary in self._scratch.values():
            temporary.cleanup()
        self._scratch.clear()
        self._support.clear()

    def __del__(self):
        if hasattr(self, "_scratch"):
            with suppress(Exception):
                self.close()

    @classmethod
    def from_settings(cls, settings) -> "Sandbox":
        """Build from ``config.Settings`` without importing it.

        An empty deny list means "the built-in credential paths", not "protect
        nothing": an unset variable must not silently weaken the profile.
        """
        configured = tuple(Path(item) for item in settings.sandbox_deny_read.split(":") if item.strip())
        return cls(SandboxSettings(
            mode=settings.sandbox,
            network=settings.sandbox_network,
            deny_read=configured or None,
            writable_tmp=settings.sandbox_writable_tmp,
            writable_workspace=settings.sandbox_writable,
        ))

    def policy(self, workspace: Path, *, owner=None) -> SandboxPolicy:
        root = validate_workspace(Path(workspace))
        if self.settings.mode != "isolated":
            return SandboxPolicy(workspace=root, network=self.settings.network, deny_read=self.settings.deny_read,
                                 writable_tmp=self.settings.writable_tmp, writable_workspace=self.settings.writable_workspace)
        if any(protected_source().is_relative_to(item) for item in runtime_roots()):
            raise PermissionError("Runtime allowlist overlaps protected application source")
        # Commands cannot create an outside hardlink under this policy. Reject
        # pre-existing multi-link files so a host-created alias is not readable.
        import stat
        for directory, _, names in os.walk(root, followlinks=False, onerror=_raise_walk_error):
            for name in names:
                info = (Path(directory) / name).lstat()
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    raise PermissionError("Isolated workspaces cannot contain hard-linked files")
        if any(root == item or root.is_relative_to(item) or item.is_relative_to(root) for item in runtime_roots()):
            raise PermissionError("An isolated workspace cannot overlap the runtime allowlist")
        key = (root, owner)
        if key not in self._scratch:
            temporary = tempfile.TemporaryDirectory(prefix="chagent-isolated-")
            self._scratch[key] = temporary
            scratch = Path(temporary.name).resolve()
            for name in ("home", "tmp"):
                (scratch / name).mkdir(mode=0o700)
        return SandboxPolicy(workspace=root, network=False, deny_read=self.settings.deny_read,
                             writable_tmp=False, isolated=True, scratch=Path(self._scratch[key].name).resolve(),
                             writable_workspace=self.settings.writable_workspace)

    def release(self, owner) -> None:
        keys = [key for key in self._scratch if key[1] == owner]
        for key in keys:
            temporary = self._scratch.pop(key)
            scratch = Path(temporary.name).resolve()
            temporary.cleanup()
            self._support = {p: result for p, result in self._support.items() if p.scratch != scratch}

    def environment(self, workspace: Path, *, owner=None) -> dict[str, str] | None:
        if self.settings.mode == "isolated":
            return isolated_environment(self.policy(workspace, owner=owner))
        if not self.settings.writable_workspace and self.enabled:
            return {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        return None

    def record(self, policy: SandboxPolicy) -> dict:
        """What was actually enforced, not what was asked for.

        When the sandbox is off nothing is denied, so listing the configured
        paths here would describe a boundary that does not exist.
        """
        enforced = self.enabled
        record = {
            "backend": self.backend.name,
            "network_allowed": policy.network if enforced else True,
            "denied_reads": [str(item) for item in policy.deny_read] if enforced else [],
            "unsandboxed": not enforced,
            "workspace_writable": policy.writable_workspace if enforced else True,
        }

        if policy.isolated:
            record.update(read_allowlist=[str(p) for p in runtime_roots()] + [str(policy.workspace), str(policy.scratch)],
                          environment="allowlist", home="synthetic", isolated=True)
        return record

    async def wrap(self, argv: Sequence[str], workspace: Path, *, owner=None) -> tuple[list[str], dict]:
        """Return the argv to spawn and its audit record, or refuse to run."""
        policy = self.policy(workspace, owner=owner)
        support = await self.support(policy)
        if not support.available:
            raise PermissionError(
                f"Sandbox backend {support.backend} is unavailable ({support.detail or 'probe failed'}); "
                "refusing to run this command; isolation will not be downgraded."
            )
        wrapped = self.backend.wrap(argv, policy)
        if policy.isolated:
            # env -i runs only after seatbelt applies, so no shell or user startup
            # code can observe the parent's environment. All spawn paths use wrap.
            assignments = [f"{key}={value}" for key, value in isolated_environment(policy).items()]
            wrapped = self.backend.wrap(["/usr/bin/env", "-i", *assignments, *argv], policy)
        return wrapped, self.record(policy)

    async def support(self, policy: SandboxPolicy) -> SandboxSupport:
        if not self.enabled:
            return SandboxSupport(True, self.backend.name, {})
        async with self._lock:
            cached = self._support.get(policy)
            if cached is None:
                cached = await self._probe(policy)
                self._support[policy] = cached
            return cached

    async def audit(self, workspace: Path, *, owner=None) -> dict:
        policy = self.policy(workspace, owner=owner)
        support = await self.support(policy)
        return {**self.record(policy), "available": support.available,
                "checks": support.checks, "detail": support.detail}

    # -- probing --------------------------------------------------------

    async def _probe(self, policy: SandboxPolicy) -> SandboxSupport:
        """Verify the mechanism on a synthetic tree, never on the workspace.

        The probe must not depend on the workspace being writable: a read-only
        repository is a normal target, and failing the probe there would disable
        the sandbox for exactly the sessions that need it least.
        """
        if policy.isolated:
            if not isinstance(self.backend, SeatbeltBackend) or not self.backend.available:
                return SandboxSupport(False, self.backend.name, {}, "isolated seatbelt backend is missing")
            from chagent.tools.isolation_probe import probe_isolated
            return await probe_isolated(self, policy)
        backend = self.backend
        if isinstance(backend, SeatbeltBackend) and not backend.available:
            return SandboxSupport(False, backend.name, {}, f"{backend.binary} is missing")
        with tempfile.TemporaryDirectory(prefix="chagent-sandbox-") as directory:
            base = Path(directory).resolve()
            workspace, secret, outside = base / "ws", base / "secret", base / "outside"
            workspace.mkdir()
            secret.write_text("probe\n", encoding="utf-8")
            probe = SandboxPolicy(workspace=workspace, network=policy.network,
                                  deny_read=(secret,), writable_tmp=False, writable_workspace=policy.writable_workspace)
            checks = {
                "starts": await self._expect(backend.wrap(["/bin/sh", "-c", "echo alive"], probe), 0, "alive"),
                "workspace_writable": await self._expect(
                    backend.wrap(["/bin/sh", "-c", f"printf ok > {workspace}/written"], probe),
                    0 if probe.writable_workspace else None),
                "outside_denied": await self._expect(
                    backend.wrap(["/bin/sh", "-c", f"printf no > {outside}"], probe), None),
                "read_denied": await self._expect(backend.wrap(["/bin/cat", str(secret)], probe), None),
                # A denied write that still leaves a file is worse than a hard
                # failure: it looks contained while the file exists.
                "no_residue": not outside.exists() and (probe.writable_workspace or not (workspace / "written").exists()),
            }
            if probe.network:
                checks["network_denied"] = True
            else:
                checks["network_denied"] = await self._network_denied(backend, probe)
        failed = [name for name, ok in checks.items() if not ok]
        return SandboxSupport(not failed, backend.name, checks,
                              "" if not failed else "failed checks: " + ", ".join(failed))

    @staticmethod
    async def _expect(argv: Sequence[str], expected: int | None, needle: str | None = None) -> bool:
        code, output = await _run(argv)
        if expected is None:
            return code != 0
        return code == expected and (needle is None or needle in output)

    @staticmethod
    async def _network_denied(backend: SandboxBackend, policy: SandboxPolicy) -> bool:
        """Prove the denial against a local listener, never a public host.

        A public request can fail for reasons unrelated to the profile (DNS,
        proxy), which would make the check pass without the profile working.
        """
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            port = listener.getsockname()[1]
            connect = (f"import socket\nsocket.create_connection(('127.0.0.1', {port}), 1)\n")
            code, _ = await _run(backend.wrap([sys.executable, "-c", connect], policy))
            return code != 0
        except OSError:
            return False
        finally:
            listener.close()


def isolated_environment(policy: SandboxPolicy) -> dict[str, str]:
    if policy.scratch is None:
        raise PermissionError("Isolated execution has no scratch directory")
    env = {"PATH": f"{Path(sys.base_prefix).resolve()}/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(policy.scratch / "home"), "TMPDIR": str(policy.scratch / "tmp") + "/",
            "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1", "DEVELOPER_DIR": "/Library/Developer/CommandLineTools",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
    if not policy.writable_workspace:
        env.update(GIT_OPTIONAL_LOCKS="0", PYTHONDONTWRITEBYTECODE="1")
    return env


def isolated_profile(policy: SandboxPolicy) -> str:
    if policy.scratch is None or policy.network:
        raise PermissionError("Isolated policy requires private scratch and no network")
    if policy.workspace == policy.scratch or policy.workspace.is_relative_to(policy.scratch) or policy.scratch.is_relative_to(policy.workspace):
        raise PermissionError("Private scratch cannot overlap workspace")
    readable = (*runtime_roots(), policy.workspace, policy.scratch)
    lines = ["(version 1)", "(deny default)", "(allow process-exec)", "(allow process-fork)",
             "(allow signal (target same-sandbox))", "(allow process-info* (target same-sandbox))",
             "(allow sysctl-read)",
             '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))',
             # dyld needs a read of the root vnode to start /bin/sh. This is a
             # literal grant, not subpath: it does not make child files readable.
             '(allow file-read* (literal "/") (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
             "(allow file-read* file-map-executable"]
    lines += [f"  (subpath {_sbpl(p)})" for p in readable]
    lines += [")", "(allow file-read-metadata"]
    # Allow traversing ancestors without granting their contents or enumeration.
    ancestors = {parent for p in readable for parent in p.parents}
    lines += [f"  (literal {_sbpl(p)})" for p in sorted(ancestors)]
    lines += [")", "(allow file-write*"]
    lines += [f"  (subpath {_sbpl(p)})" for p in ((policy.workspace, policy.scratch) if policy.writable_workspace else (policy.scratch,))]
    lines += ['  (literal "/dev/null")', ")"]
    if policy.deny_read:
        lines += ["(deny file-read*"]
        lines += [f"  (subpath {_sbpl(p)})" for p in policy.deny_read]
        lines += [")"]
    # No blanket mach, network, IPC, other-process or user-temp permissions.
    return "\n".join(lines) + "\n"


def _raise_walk_error(error: OSError) -> None:
    raise PermissionError("Cannot inspect the isolated workspace") from error
