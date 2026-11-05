"""Which host command needs a human decision. Pure, no I/O, no shell parsing.

The gate covers tools that run a model-chosen command on the host and each
subsequent write to a session process. A command grant does not grant future
stdin bytes or EOF.

Modes, following the execution-boundary design:

``never``
    No approvals; host commands behave exactly as before. This is the default so
    an upgrade cannot start blocking existing runs.
``on-request``
    Ask only when the command would exceed the default permission: with the
    sandbox off there is no kernel boundary, so the command is outside what was
    configured. Sandboxed execution is inside it and runs without asking.
``untrusted``
    Ask for every host command, sandboxed or not, unless it is allowlisted.
    ``process(write)`` always asks in either approval mode.

The allowlist is a convenience for known-safe commands, not a security parser:
any shell control character disqualifies a command from it, because a prefix
match on ``pytest`` must not bless ``pytest; curl … | sh``.
"""

from dataclasses import dataclass
import hashlib
import json

# Tools that run something the model chose on the host.
HOST_TOOLS = frozenset({"exec_command", "run_tests", "host_exec"})
MODES = ("never", "on-request", "untrusted")
# Anything that could turn a prefix-matched command into a different program.
SHELL_CONTROL = frozenset(";|&<>`$()\n\r")


def subject(tool_name: str, arguments: dict) -> str:
    """What the decision is about: the command line, or a host-shaped label."""
    if tool_name == "exec_command":
        return str(arguments.get("cmd", ""))
    if tool_name == "run_tests":
        suite = arguments.get("suite", "auto")
        selection = arguments.get("selection")
        return f"run_tests {suite}" + (f" {selection}" if selection else "")
    if tool_name == "host_exec":
        return f"{arguments.get('command', '')} (workdir={arguments.get('workdir', '.')})"
    if tool_name == "process" and arguments.get("action") == "write":
        chars = arguments.get("chars", "")
        return (f"process {arguments.get('process_id', '<missing>')} stdin "
                f"{json.dumps(chars, ensure_ascii=False)} "
                f"({len(str(chars).encode('utf-8'))} UTF-8 bytes); "
                f"close_stdin={arguments.get('close_stdin', False) is True}")
    return tool_name


def fingerprint(tool_name: str, arguments: dict, cwd: str) -> str:
    """A grant is bound to one exact invocation in one directory."""
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}\0{cwd}\0{canonical}".encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    mode: str = "never"
    allowlist: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"Approval mode must be one of {MODES}")

    @classmethod
    def from_settings(cls, settings) -> "ApprovalPolicy":
        entries = []
        for item in settings.approval_allowlist.split(":"):
            tokens = tuple(item.split())
            if tokens:
                entries.append(tokens)
        return cls(mode=settings.approval, allowlist=tuple(entries))

    def covers(self, tool_name: str, arguments: dict) -> bool:
        """Only host-executing tools are subject to approval."""
        return tool_name in HOST_TOOLS or (tool_name == "process" and arguments.get("action") == "write")

    def allowlisted(self, command: str) -> bool:
        if not command.strip() or any(character in command for character in SHELL_CONTROL):
            return False
        tokens = tuple(command.split())
        return any(tokens[:len(prefix)] == prefix for prefix in self.allowlist)

    def requires_approval(self, tool_name: str, arguments: dict, *, sandbox: str) -> bool:
        if sandbox == "managed-container":
            return tool_name == "host_exec"
        if self.mode == "never" or not self.covers(tool_name, arguments):
            return False
        # A command allowlist says nothing about bytes later sent to its stdin.
        if tool_name == "process":
            return True
        if self.allowlisted(subject(tool_name, arguments)):
            return False
        if self.mode == "untrusted":
            return True
        # on-request: sandboxed execution is inside the configured permission;
        # unsandboxed execution is not.
        return sandbox == "off"


#: Tools that change the workspace, which is a bind mount of the user's own
#: directory once the container is in the picture. Kept equal to
#: ``tools.builtin.WRITE_TOOLS`` by a test: that set decides which tools may
#: exist, this one decides which need a human, and they must name the same
#: tools even though they answer different questions. ``create_file`` is absent
#: on purpose: it already stops at a host confirmation when a name collides.
FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "create_directory"})

#: Where a *new* file is as consequential as an overwrite. Git hooks and CI
#: workflows run later without anyone re-reading them, and package or shell
#: configuration decides what executes the next time the user types a command.
SENSITIVE_DIRS = frozenset({".git", ".github", ".husky", ".devcontainer", ".vscode", ".idea"})
SENSITIVE_NAMES = frozenset({".npmrc", ".pypirc", ".netrc", ".gitconfig", ".bashrc", ".zshrc",
                             ".profile", ".bash_profile", ".config"})


def sensitive_path(relative: str) -> bool:
    """Whether creating this path is a decision rather than a scratch write."""
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts:
        return False
    if parts[0] in SENSITIVE_DIRS:
        return True
    name = parts[-1]
    return name in SENSITIVE_NAMES or name == ".env" or name.startswith(".env.")
