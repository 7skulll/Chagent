"""The three execution axes: place, capability, gate.

Every deployment answers three separate questions, and one word used to answer
all of them at once:

=============  ================================  ====================
axis           question                          values
=============  ================================  ====================
``boundary``   where does the process run        ``container`` / ``none``
``capability`` may it execute commands           ``full`` / ``read-only``
``gate``       does a human approve each command ``none`` / ``approval``
=============  ================================  ====================

:func:`resolve` is the only place the rules live. The settings object, the web
entry, the CLI and the tool assembly all read its result, so a start-up banner,
``/api/info`` and the actual tool face cannot disagree about what this process
will do.

The published shapes are three. A fourth (``container`` + ``full`` +
``approval``) is defined but reserved: the approval broker exists and the axis
is real, yet no entry point wires it up, so it is not offered as a product
combination until the C3 batch.
"""

from dataclasses import dataclass


CONTAINER = "container"
NONE = "none"

FULL = "full"
READ_ONLY = "read-only"

GATE_NONE = "none"
GATE_APPROVAL = "approval"

BOUNDARIES = frozenset({CONTAINER, NONE})
CAPABILITIES = frozenset({FULL, READ_ONLY})
GATES = frozenset({GATE_NONE, GATE_APPROVAL})

#: What the product offers today, in the order they are documented.
PUBLISHED = (
    (CONTAINER, FULL, GATE_NONE),
    (CONTAINER, READ_ONLY, GATE_NONE),
    (NONE, READ_ONLY, GATE_NONE),
)
#: Accepted by configuration, never by a web entry point: nothing to approve
#: until the gate is wired to the scheduler.
RESERVED = ((CONTAINER, FULL, GATE_APPROVAL),)

PROFILES = frozenset({"configured", "containerized"})
PROFILE_BOUNDARY = {"configured": NONE, "containerized": CONTAINER}


@dataclass(frozen=True, slots=True)
class ExecutionAxes:
    """One resolved answer to the three questions."""

    boundary: str = NONE
    capability: str = READ_ONLY
    gate: str = GATE_NONE

    @property
    def combination(self) -> tuple[str, str, str]:
        return (self.boundary, self.capability, self.gate)

    @property
    def executable(self) -> bool:
        """Only a container plus full capability may run a command."""
        return self.boundary == CONTAINER and self.capability == FULL

    @property
    def published(self) -> bool:
        """Whether this is one of the three shapes the product defines."""
        return self.combination in PUBLISHED

    @property
    def programmable(self) -> bool:
        """The one shape the web entry accepts, and what ``programmable`` means."""
        return self.combination == (CONTAINER, FULL, GATE_NONE)

    def describe(self) -> str:
        return f"boundary={self.boundary} capability={self.capability} gate={self.gate}"


def resolve(*, execution_profile: str = "configured", boundary: str = "",
            code_mode: bool = True, allow_host_commands: bool = False,
            approval: str = "never", sandbox: str = "off",
            container_declared: bool = False, workspace_writable: bool = True,
            network: bool = False) -> ExecutionAxes:
    """Derive the axes from the switches and reject anything unsupported.

    ``boundary`` is the declared value; leaving it empty derives it from the
    profile. A declaration that contradicts the profile is a configuration
    mistake, not a preference, because the profile is what the deployment was
    built for.
    """
    if execution_profile == "trusted-development":
        raise ValueError("trusted-development was removed; run Chagent in a container")
    if execution_profile not in PROFILES:
        raise ValueError("CHAGENT_EXECUTION_PROFILE must be configured or containerized")
    expected = PROFILE_BOUNDARY[execution_profile]
    if boundary and boundary != expected:
        raise ValueError("CHAGENT_BOUNDARY conflicts with execution profile")
    if sandbox != "off":
        raise ValueError("CHAGENT_SANDBOX must be off; in-process sandbox profiles were removed")
    if approval == "on-failure":
        raise ValueError("CHAGENT_APPROVAL=on-failure is not supported yet")
    if approval not in {"never", "on-request", "untrusted"}:
        raise ValueError("CHAGENT_APPROVAL must be never, on-request or untrusted")

    axes = ExecutionAxes(
        boundary=expected,
        # A command face needs both the build switch and the runtime switch;
        # either one alone is a half-configured entry point, not a capability.
        capability=FULL if (code_mode and allow_host_commands) else READ_ONLY,
        gate=GATE_NONE if approval == "never" else GATE_APPROVAL,
    )
    if axes.combination not in PUBLISHED + RESERVED:
        if axes.boundary == NONE and axes.capability == FULL:
            # The common misconfiguration: a host command switch without the
            # container that is supposed to carry it.
            raise ValueError("Host commands require the container boundary; "
                             "set CHAGENT_EXECUTION_PROFILE=containerized")
        raise ValueError(
            f"unsupported execution combination boundary={axes.boundary} "
            f"capability={axes.capability} gate={axes.gate}")
    if axes.capability == FULL:
        # Commands exist only inside a declared container with a writable,
        # networked workspace; a read-only shape does not claim any of these.
        if not container_declared:
            raise ValueError("containerized requires an explicit container declaration, "
                             "code=on, host commands, network=true and workspace_writable=true")
        if not workspace_writable:
            raise ValueError("containerized requires CHAGENT_SANDBOX_WRITABLE=true")
        if not network:
            raise ValueError("containerized requires CHAGENT_SANDBOX_NETWORK=true")
    return axes


def from_settings(settings) -> ExecutionAxes:
    """Resolve the axes of a ``Settings`` instance without importing it here."""
    return resolve(
        execution_profile=settings.execution_profile,
        boundary=settings.boundary_setting,
        code_mode=settings.code_mode,
        allow_host_commands=settings.allow_host_commands,
        approval=settings.approval,
        sandbox=settings.sandbox,
        container_declared=settings.container_declared,
        workspace_writable=settings.sandbox_writable,
        network=settings.sandbox_network,
    )
