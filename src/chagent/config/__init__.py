"""Environment-based application configuration."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import os

from psycopg.conninfo import conninfo_to_dict

from chagent.execution import ExecutionAxes, resolve


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
    allow_host_commands: bool = False
    max_turns: int = 32
    max_tool_concurrency: int = 4
    memory_worker_concurrency: int = 1
    # Legacy setting accepted only as "off"; the deployment supplies the boundary.
    sandbox: str = "off"
    sandbox_network: bool = False
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
    #: Index large, late-needed tools instead of declaring them every request.
    deferred_tools: bool = True
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
    #: Where the process runs. ``configured`` is the in-host diagnostic profile;
    #: only ``containerized`` can carry command execution.
    execution_profile: str = "configured"
    container_declared: bool = False
    boundary_setting: str = ""

    @property
    def axes(self) -> ExecutionAxes:
        """The resolved boundary/capability/gate, derived in one place."""
        return resolve(
            execution_profile=self.execution_profile, boundary=self.boundary_setting,
            code_mode=self.code_mode, allow_host_commands=self.allow_host_commands,
            approval=self.approval, sandbox=self.sandbox,
            container_declared=self.container_declared,
            workspace_writable=self.sandbox_writable, network=self.sandbox_network,
        )

    @property
    def boundary(self) -> str:
        return self.axes.boundary

    @property
    def capability(self) -> str:
        return self.axes.capability

    @property
    def gate(self) -> str:
        return self.axes.gate

    def __post_init__(self) -> None:
        # One validation for the execution axes; the individual switches are
        # inputs to it, not a second copy of the rules. Touch it first so an
        # unusable profile fails before the unrelated connection checks.
        self.axes
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
        if not isinstance(self.deferred_tools, bool):
            raise ValueError("CHAGENT_DEFERRED_TOOLS must be true or false")
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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if env is None else env
        # A removed switch that silently keeps being read is a lie about what
        # the process will do. Every retired name fails here by design.
        for name in ("CHAGENT_DEFAULT_MODE",):
            if values.get(name):
                raise ValueError(f"{name} has been removed; sessions no longer choose a tool face")
        allow_commands = values.get("CHAGENT_ALLOW_HOST_COMMANDS", "false").lower()
        if allow_commands not in {"true", "false"}:
            raise ValueError("CHAGENT_ALLOW_HOST_COMMANDS must be true or false")
        code_mode = values.get("CHAGENT_CODE_MODE", "on").strip().lower()
        if code_mode not in {"on", "off"}:
            raise ValueError("CHAGENT_CODE_MODE must be on or off")
        sandbox_network = values.get("CHAGENT_SANDBOX_NETWORK", "false").lower()
        if sandbox_network not in {"true", "false"}:
            raise ValueError("CHAGENT_SANDBOX_NETWORK must be true or false")
        sandbox_writable = values.get("CHAGENT_SANDBOX_WRITABLE", "true").lower()
        if sandbox_writable not in {"true", "false"}:
            raise ValueError("CHAGENT_SANDBOX_WRITABLE must be true or false")
        container_declared = values.get("CHAGENT_CONTAINER_DECLARED", "false").lower()
        if container_declared not in {"true", "false"}:
            raise ValueError("CHAGENT_CONTAINER_DECLARED must be true or false")
        deferred_tools = values.get("CHAGENT_DEFERRED_TOOLS", "true").lower()
        if deferred_tools not in {"true", "false"}:
            raise ValueError("CHAGENT_DEFERRED_TOOLS must be true or false")
        return cls(
            execution_profile=values.get("CHAGENT_EXECUTION_PROFILE", "configured").strip().lower(),
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
            sandbox=values.get("CHAGENT_SANDBOX", "off").strip().lower(),
            sandbox_network=sandbox_network == "true",
            approval=values.get("CHAGENT_APPROVAL", "never").strip().lower(),
            approval_allowlist=values.get("CHAGENT_APPROVAL_ALLOWLIST", ""),
            approval_timeout=float(values.get("CHAGENT_APPROVAL_TIMEOUT", "300")),
            approval_unwatched_grace=float(values.get("CHAGENT_APPROVAL_UNWATCHED_GRACE", "10")),
            max_processes=int(values.get("CHAGENT_MAX_PROCESSES", "4")),
            process_timeout=float(values.get("CHAGENT_PROCESS_TIMEOUT", "600")),
            web_access=values.get("CHAGENT_WEB_ACCESS", "off").strip().lower(),
            deferred_tools=deferred_tools == "true",
            **_search_api_key(values),
            code_mode=code_mode == "on",
        )
