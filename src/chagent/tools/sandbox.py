"""Execution boundary description for a process already placed in a container.

The filesystem and shell tools keep this interface so their run lifecycle does
not depend on a particular deployment. No in-process kernel sandbox is applied.
"""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Sequence

from chagent.boundaries import validate_workspace


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    mode: str = "off"
    network: bool = True
    writable_workspace: bool = True
    containerized: bool = False

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

    @classmethod
    def from_settings(cls, settings) -> "Sandbox":
        return cls(SandboxSettings(
            mode=settings.sandbox,
            network=settings.sandbox_network,
            writable_workspace=settings.sandbox_writable,
            containerized=settings.axes.boundary == "container",
        ))

    def policy(self, workspace: Path, *, owner=None) -> SandboxPolicy:
        return SandboxPolicy(validate_workspace(Path(workspace)), self.settings.network,
                             self.settings.writable_workspace, self.settings.containerized)

    def environment(self, workspace: Path, *, owner=None) -> dict[str, str] | None:
        if self.settings.containerized:
            # Commands need toolchains, never the agent's API keys or DB URL.
            return {"PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
                    "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8"}
        return None

    def record(self, policy: SandboxPolicy) -> dict:
        return {"boundary": "container" if policy.containerized else "none",
                "network": policy.network, "workspace_writable": policy.writable_workspace,
                "image": os.environ.get("CHAGENT_IMAGE") if policy.containerized else None}

    async def wrap(self, argv: Sequence[str], workspace: Path, *, owner=None) -> tuple[list[str], dict]:
        policy = self.policy(workspace, owner=owner)
        if not policy.containerized:
            raise PermissionError("Command execution requires the container boundary")
        return list(argv), self.record(policy)

    async def support(self, policy: SandboxPolicy) -> SandboxSupport:
        return SandboxSupport(policy.containerized, "container" if policy.containerized else "none", {},
                              "" if policy.containerized else "container boundary is required")

    async def audit(self, workspace: Path, *, owner=None) -> dict:
        policy = self.policy(workspace, owner=owner)
        support = await self.support(policy)
        return {**self.record(policy), "available": support.available,
                "checks": support.checks, "detail": support.detail}

    def release(self, owner) -> None:
        pass

    def close(self) -> None:
        pass
