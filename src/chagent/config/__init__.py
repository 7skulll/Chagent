"""Environment-based application configuration."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import os

from psycopg.conninfo import conninfo_to_dict


def _search_api_key(values: Mapping[str, str]) -> dict[str, str]:
    """Resolve the outbound key and name the variable that won.

    The search key is a separate credential even when it holds the same value as
    the provider key; the provider key is only a fallback. Resolving both here
    keeps diagnostics from re-deriving precedence and disagreeing with runtime.
    """
    explicit = (values.get("CHAGENT_SEARCH_API_KEY") or "").strip()
    if explicit:
        return {"search_api_key": explicit, "search_api_key_source": "CHAGENT_SEARCH_API_KEY"}
    fallback = (values.get("DEEPSEEK_API_KEY") or "").strip()
    if fallback:
        return {"search_api_key": fallback, "search_api_key_source": "DEEPSEEK_API_KEY"}
    return {"search_api_key": "", "search_api_key_source": ""}


@dataclass(frozen=True, slots=True)
class Settings:
    # Prevent accidental disclosure of credentials in repr/logs.
    database_url: str = field(default="postgresql:///chagent", repr=False)
    db_pool_min_size: int = 1
    db_pool_max_size: int = 4
    db_timeout: float = 5.0
    allow_host_commands: bool = True
    max_turns: int = 32
    max_tool_concurrency: int = 4
    memory_worker_concurrency: int = 1
    # Host-command confinement. ``off`` keeps the pre-sandbox behaviour exactly.
    sandbox: str = "isolated"
    sandbox_network: bool = False
    sandbox_deny_read: str = ""
    sandbox_writable_tmp: bool = True
    sandbox_writable: bool = True
    # Human approval for host commands. ``never`` keeps the pre-approval
    # behaviour exactly; ``on-failure`` is intentionally unsupported.
    approval: str = "never"
    approval_allowlist: str = ""
    approval_timeout: float = 300.0
    #: Seconds a remote approval may wait with no browser attached before it is
    #: denied as unwatched. 0 keeps the absolute deadline (the pre-Z4 rule).
    approval_unwatched_grace: float = 10.0
    # Long-running host commands started with ``yield_time_ms``: how many one
    # run may hold at once, and the wall clock that bounds each of them.
    max_processes: int = 4
    process_timeout: float = 600.0
    # Outbound HTTP. ``off`` keeps the tool surface at five tools; ``deepseek``
    # opens web_search and web_fetch together (DeepSeek's Search/Fetch API). The
    # tools run inside the agent process, so the sandbox does not apply to them
    # and this switch is the only gate on leaving the machine.
    web_access: str = "off"
    #: Its own name, not ``DEEPSEEK_API_KEY``: the search key is a separate
    #: credential even when the same value is used for both. ``from_env`` falls
    #: back to the provider key so one key is enough to try it.
    search_api_key: str = field(default="", repr=False)
    #: Which variable supplied ``search_api_key``: ``CHAGENT_SEARCH_API_KEY``,
    #: ``DEEPSEEK_API_KEY`` (the fallback) or empty when neither is set. The name
    #: only — ``Settings.from_env`` is the single place that resolves precedence,
    #: so diagnostics do not have to re-derive it and get it wrong.
    search_api_key_source: str = ""
    #: Legacy build switch retained for diagnostic runners. Production unified
    #: startup requires it on; setting it off rejects startup.
    code_mode: bool = True
    #: Legacy diagnostic default. Production web creates a work-valued row and
    #: maps old work/code rows to the same executor.
    default_mode: str = "work"
    # configured preserves existing approval policy; trusted-development is an
    # explicit writable web capability, never an automatic approval override.
    execution_profile: str = "trusted-development"
    container_declared: bool = False
    boundary_setting: str = ""

    @property
    def boundary(self) -> str:
        return self.boundary_setting or ("container" if self.execution_profile == "containerized" else self.sandbox)

    def __post_init__(self) -> None:
        if self.execution_profile not in {"configured", "trusted-development", "containerized"}:
            raise ValueError("CHAGENT_EXECUTION_PROFILE must be configured, trusted-development or containerized")
        expected_boundary = "container" if self.execution_profile == "containerized" else self.sandbox
        if self.boundary_setting and self.boundary_setting != expected_boundary:
            raise ValueError("CHAGENT_BOUNDARY conflicts with execution profile or sandbox")
        if self.execution_profile == "containerized" and not (
            self.container_declared and self.code_mode and self.allow_host_commands
            and self.sandbox == "off" and self.sandbox_writable and self.approval == "never"
        ):
            raise ValueError("containerized requires an explicit container declaration, code=on, host commands, "
                             "sandbox=off, writable=true and approval=never")
        if self.trusted_development and not (
            self.code_mode and self.allow_host_commands and self.sandbox == "isolated"
            and self.sandbox_writable and not self.sandbox_network and self.approval == "never"
        ):
            raise ValueError("trusted-development requires code=on, host commands, isolated, "
                             "writable=true, network=false and CHAGENT_APPROVAL=never; "
                             "existing approval settings are never overridden")
        if not self.database_url.strip():
            raise ValueError("CHAGENT_DATABASE_URL must not be empty")
        try:
            options = conninfo_to_dict(self.database_url)
        except Exception:
            raise ValueError("CHAGENT_DATABASE_URL is not valid PostgreSQL connection info") from None
        if not options.get("dbname"):
            raise ValueError("CHAGENT_DATABASE_URL must specify a database")
        if self.db_pool_min_size < 0 or self.db_pool_max_size < 1:
            raise ValueError("Pool minimum must be nonnegative and maximum must be positive")
        if self.db_pool_min_size > self.db_pool_max_size:
            raise ValueError("Pool minimum must not exceed maximum")
        if not math.isfinite(self.db_timeout) or self.db_timeout <= 0:
            raise ValueError("CHAGENT_DB_TIMEOUT must be finite and positive")
        if not 1 <= self.max_turns <= 128:
            raise ValueError("CHAGENT_MAX_TURNS must be between 1 and 128")
        if isinstance(self.max_tool_concurrency, bool) or not isinstance(self.max_tool_concurrency, int) or not 1 <= self.max_tool_concurrency <= 32:
            raise ValueError("CHAGENT_MAX_TOOL_CONCURRENCY must be an integer between 1 and 32")
        if isinstance(self.memory_worker_concurrency, bool) or not isinstance(self.memory_worker_concurrency, int) \
                or not 1 <= self.memory_worker_concurrency <= 4:
            raise ValueError("CHAGENT_MEMORY_WORKER_CONCURRENCY must be an integer between 1 and 4")
        if not isinstance(self.sandbox_writable, bool):
            raise ValueError("CHAGENT_SANDBOX_WRITABLE must be true or false")
        if self.sandbox not in {"off", "workspace-write", "isolated"}:
            raise ValueError("CHAGENT_SANDBOX must be off, workspace-write or isolated")
        if self.approval == "on-failure":
            raise ValueError("CHAGENT_APPROVAL=on-failure is not supported yet")
        if self.approval not in {"never", "on-request", "untrusted"}:
            raise ValueError("CHAGENT_APPROVAL must be never, on-request or untrusted")
        if not math.isfinite(self.approval_timeout) or self.approval_timeout <= 0:
            raise ValueError("CHAGENT_APPROVAL_TIMEOUT must be finite and positive")
        if not math.isfinite(self.approval_unwatched_grace) or self.approval_unwatched_grace < 0:
            raise ValueError("CHAGENT_APPROVAL_UNWATCHED_GRACE must be finite and nonnegative")
        if isinstance(self.max_processes, bool) or not isinstance(self.max_processes, int) \
                or not 1 <= self.max_processes <= 16:
            raise ValueError("CHAGENT_MAX_PROCESSES must be an integer between 1 and 16")
        if not math.isfinite(self.process_timeout) or self.process_timeout <= 0:
            raise ValueError("CHAGENT_PROCESS_TIMEOUT must be finite and positive")
        if self.web_access == "tavily":
            # WD 批 removed it: the tools now speak DeepSeek's Search/Fetch API.
            # A silent rename would make an old .env appear to work while
            # talking to a different provider, so this fails instead.
            raise ValueError(
                "CHAGENT_WEB_ACCESS=tavily has been removed (the search tools now use DeepSeek's "
                "Search/Fetch API); set CHAGENT_WEB_ACCESS=deepseek")
        if self.web_access not in {"off", "deepseek"}:
            raise ValueError("CHAGENT_WEB_ACCESS must be off or deepseek")
        if self.default_mode not in {"work", "code"}:
            raise ValueError("CHAGENT_DEFAULT_MODE must be work or code")
        if self.default_mode == "code" and not self.code_mode:
            # Honouring it would mean registering the code face, which the build
            # switch forbids; degrading it would make the setting a lie.
            raise ValueError("CHAGENT_DEFAULT_MODE=code requires CHAGENT_CODE_MODE=on")
        # Fail loudly here rather than registering a tool that fails on its
        # first call: an enabled-but-keyless switch is a configuration mistake.
        if self.web_access == "deepseek" and not self.search_api_key.strip():
            raise ValueError(
                "CHAGENT_WEB_ACCESS=deepseek requires CHAGENT_SEARCH_API_KEY "
                "(or DEEPSEEK_API_KEY to fall back to)")
        # Each extracting worker holds a connection while it reads or writes. If
        # they can occupy the whole pool, a concurrent run waits until it gives
        # up on db_timeout and fails. A single-connection pool is exempt: there,
        # runs and workers serialize by construction.
        if self.memory_worker_concurrency > max(1, self.db_pool_max_size - 1):
            raise ValueError(
                "CHAGENT_MEMORY_WORKER_CONCURRENCY must leave one connection for runs "
                "(at most CHAGENT_DB_POOL_MAX_SIZE - 1)")

    @property
    def enabled_modes(self) -> tuple[str, ...]:
        """Modes this process can actually serve, default first."""
        return ("work", "code") if self.code_mode else ("work",)

    @property
    def trusted_development(self) -> bool:
        return self.execution_profile == "trusted-development"

    @property
    def readonly_code_available(self) -> bool:
        """Configured read-only web capability, before backend preflight."""
        return (self.code_mode and self.allow_host_commands and self.sandbox == "isolated"
                and not self.sandbox_writable and not self.sandbox_network)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if env is None else env
        allow_commands = values.get("CHAGENT_ALLOW_HOST_COMMANDS", "true").lower()
        if allow_commands not in {"true", "false"}:
            raise ValueError("CHAGENT_ALLOW_HOST_COMMANDS must be true or false")
        code_mode = values.get("CHAGENT_CODE_MODE", "on").strip().lower()
        if code_mode not in {"on", "off"}:
            raise ValueError("CHAGENT_CODE_MODE must be on or off")
        sandbox_network = values.get("CHAGENT_SANDBOX_NETWORK", "false").lower()
        if sandbox_network not in {"true", "false"}:
            raise ValueError("CHAGENT_SANDBOX_NETWORK must be true or false")
        sandbox_tmp = values.get("CHAGENT_SANDBOX_WRITABLE_TMP", "true").lower()
        if sandbox_tmp not in {"true", "false"}:
            raise ValueError("CHAGENT_SANDBOX_WRITABLE_TMP must be true or false")
        sandbox_writable = values.get("CHAGENT_SANDBOX_WRITABLE", "true").lower()
        if sandbox_writable not in {"true", "false"}:
            raise ValueError("CHAGENT_SANDBOX_WRITABLE must be true or false")
        container_declared = values.get("CHAGENT_CONTAINER_DECLARED", "false").lower()
        if container_declared not in {"true", "false"}:
            raise ValueError("CHAGENT_CONTAINER_DECLARED must be true or false")
        return cls(
            execution_profile=values.get("CHAGENT_EXECUTION_PROFILE", "trusted-development").strip().lower(),
            container_declared=container_declared == "true",
            boundary_setting=values.get("CHAGENT_BOUNDARY", "").strip().lower(),
            sandbox_writable=sandbox_writable == "true",
            database_url=values.get("CHAGENT_DATABASE_URL", "postgresql:///chagent"),
            db_pool_min_size=int(values.get("CHAGENT_DB_POOL_MIN_SIZE", "1")),
            db_pool_max_size=int(values.get("CHAGENT_DB_POOL_MAX_SIZE", "4")),
            db_timeout=float(values.get("CHAGENT_DB_TIMEOUT", "5")),
            allow_host_commands=allow_commands == "true",
            max_turns=int(values.get("CHAGENT_MAX_TURNS", "32")),
            max_tool_concurrency=int(values.get("CHAGENT_MAX_TOOL_CONCURRENCY", "4")),
            memory_worker_concurrency=int(values.get("CHAGENT_MEMORY_WORKER_CONCURRENCY", "1")),
            sandbox=values.get("CHAGENT_SANDBOX", "isolated").strip().lower(),
            sandbox_network=sandbox_network == "true",
            sandbox_deny_read=values.get("CHAGENT_SANDBOX_DENY_READ", ""),
            sandbox_writable_tmp=sandbox_tmp == "true",
            approval=values.get("CHAGENT_APPROVAL", "never").strip().lower(),
            approval_allowlist=values.get("CHAGENT_APPROVAL_ALLOWLIST", ""),
            approval_timeout=float(values.get("CHAGENT_APPROVAL_TIMEOUT", "300")),
            approval_unwatched_grace=float(values.get("CHAGENT_APPROVAL_UNWATCHED_GRACE", "10")),
            max_processes=int(values.get("CHAGENT_MAX_PROCESSES", "4")),
            process_timeout=float(values.get("CHAGENT_PROCESS_TIMEOUT", "600")),
            web_access=values.get("CHAGENT_WEB_ACCESS", "off").strip().lower(),
            **_search_api_key(values),
            code_mode=code_mode == "on",
            default_mode=values.get("CHAGENT_DEFAULT_MODE", "work").strip().lower(),
        )
