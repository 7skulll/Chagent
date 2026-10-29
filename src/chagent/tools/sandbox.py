"""A platform profile wrapped around a host command, not around this process.

The file tools already confine themselves with directory descriptors and
no-follow operations (``workspace.py``). The surface they cannot cover is a host
command: once ``/bin/sh`` starts, every path check that lived in Python is gone.
This module adds the one boundary that survives ``exec`` — a kernel profile.

What it is not: a container, and not a read allowlist. macOS refuses any profile
that denies reads globally (the loader aborts before ``main``), so reads are
protected by naming credential paths, never by narrowing to the workspace. The
capability table in ``docs/执行边界设计.md`` §10.4 is the honest description and
should stay in sync with this file.
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


MODES = ("off", "workspace-write")
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

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"Sandbox mode must be one of {MODES}")
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

    Allow-everything then deny is deliberate: the write and read denials below
    are the only ones macOS accepts. A global ``deny file-read*`` with an allow
    list aborts the process during dynamic linking.
    """
    lines = ["(version 1)", "(allow default)"]
    if not policy.network:
        lines.append("(deny network-outbound)")
    lines += ["(deny file-write*)", "(allow file-write*",
              f"  (subpath {_sbpl(policy.workspace)})",
              '  (literal "/dev/null")',
              '  (literal "/dev/stdout")',
              '  (literal "/dev/stderr")']
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


async def _run(argv: Sequence[str], timeout: float = PROBE_TIMEOUT) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
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
        self.enabled = not isinstance(self.backend, DisabledBackend)
        self._support: dict[SandboxPolicy, SandboxSupport] = {}
        self._lock = asyncio.Lock()

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
        ))

    def policy(self, workspace: Path) -> SandboxPolicy:
        return SandboxPolicy(Path(workspace).expanduser().resolve(strict=True),
                             self.settings.network, self.settings.deny_read, self.settings.writable_tmp)

    def record(self, policy: SandboxPolicy) -> dict:
        """What was actually enforced, not what was asked for.

        When the sandbox is off nothing is denied, so listing the configured
        paths here would describe a boundary that does not exist.
        """
        enforced = self.enabled
        return {
            "backend": self.backend.name,
            "network_allowed": policy.network if enforced else True,
            "denied_reads": [str(item) for item in policy.deny_read] if enforced else [],
            "unsandboxed": not enforced,
        }

    async def wrap(self, argv: Sequence[str], workspace: Path) -> tuple[list[str], dict]:
        """Return the argv to spawn and its audit record, or refuse to run."""
        policy = self.policy(workspace)
        support = await self.support(policy)
        if not support.available:
            raise PermissionError(
                f"Sandbox backend {support.backend} is unavailable ({support.detail or 'probe failed'}); "
                "refusing to run this command unsandboxed. Set CHAGENT_SANDBOX=off to allow that explicitly."
            )
        return self.backend.wrap(argv, policy), self.record(policy)

    async def support(self, policy: SandboxPolicy) -> SandboxSupport:
        if not self.enabled:
            return SandboxSupport(True, self.backend.name, {})
        async with self._lock:
            cached = self._support.get(policy)
            if cached is None:
                cached = await self._probe(policy)
                self._support[policy] = cached
            return cached

    async def audit(self, workspace: Path) -> dict:
        policy = self.policy(workspace)
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
        backend = self.backend
        if isinstance(backend, SeatbeltBackend) and not backend.available:
            return SandboxSupport(False, backend.name, {}, f"{backend.binary} is missing")
        with tempfile.TemporaryDirectory(prefix="chagent-sandbox-") as directory:
            base = Path(directory).resolve()
            workspace, secret, outside = base / "ws", base / "secret", base / "outside"
            workspace.mkdir()
            secret.write_text("probe\n", encoding="utf-8")
            probe = SandboxPolicy(workspace=workspace, network=policy.network,
                                  deny_read=(secret,), writable_tmp=False)
            checks = {
                "starts": await self._expect(backend.wrap(["/bin/sh", "-c", "echo alive"], probe), 0, "alive"),
                "workspace_writable": await self._expect(
                    backend.wrap(["/bin/sh", "-c", f"printf ok > {workspace}/written"], probe), 0),
                "outside_denied": await self._expect(
                    backend.wrap(["/bin/sh", "-c", f"printf no > {outside}"], probe), None),
                "read_denied": await self._expect(backend.wrap(["/bin/cat", str(secret)], probe), None),
                # A denied write that still leaves a file is worse than a hard
                # failure: it looks contained while the file exists.
                "no_residue": not outside.exists(),
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
