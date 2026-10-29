"""Environment-based application configuration."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import os

from psycopg.conninfo import conninfo_to_dict


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
    # Host-command confinement. ``off`` keeps the pre-sandbox behaviour exactly.
    sandbox: str = "off"
    sandbox_network: bool = False
    sandbox_deny_read: str = ""
    sandbox_writable_tmp: bool = True
    # Human approval for host commands. ``never`` keeps the pre-approval
    # behaviour exactly; ``on-failure`` is intentionally unsupported.
    approval: str = "never"
    approval_allowlist: str = ""
    approval_timeout: float = 300.0
    # Long-running host commands started with ``yield_time_ms``: how many one
    # run may hold at once, and the wall clock that bounds each of them.
    max_processes: int = 4
    process_timeout: float = 600.0
    # Outbound HTTP. ``off`` keeps the tool surface at twelve tools; ``tavily``
    # opens web_search and web_fetch together. The tools run inside the agent
    # process, so the sandbox does not apply to them and this switch is the only
    # gate on leaving the machine.
    web_access: str = "off"
    tavily_api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
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
        if self.sandbox not in {"off", "workspace-write"}:
            raise ValueError("CHAGENT_SANDBOX must be off or workspace-write")
        if self.approval == "on-failure":
            raise ValueError("CHAGENT_APPROVAL=on-failure is not supported yet")
        if self.approval not in {"never", "on-request", "untrusted"}:
            raise ValueError("CHAGENT_APPROVAL must be never, on-request or untrusted")
        if not math.isfinite(self.approval_timeout) or self.approval_timeout <= 0:
            raise ValueError("CHAGENT_APPROVAL_TIMEOUT must be finite and positive")
        if isinstance(self.max_processes, bool) or not isinstance(self.max_processes, int) \
                or not 1 <= self.max_processes <= 16:
            raise ValueError("CHAGENT_MAX_PROCESSES must be an integer between 1 and 16")
        if not math.isfinite(self.process_timeout) or self.process_timeout <= 0:
            raise ValueError("CHAGENT_PROCESS_TIMEOUT must be finite and positive")
        if self.web_access not in {"off", "tavily"}:
            raise ValueError("CHAGENT_WEB_ACCESS must be off or tavily")
        # Fail loudly here rather than registering a tool that fails on its
        # first call: an enabled-but-keyless switch is a configuration mistake.
        if self.web_access == "tavily" and not self.tavily_api_key.strip():
            raise ValueError("CHAGENT_WEB_ACCESS=tavily requires CHAGENT_TAVILY_API_KEY")
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
        allow_commands = values.get("CHAGENT_ALLOW_HOST_COMMANDS", "false").lower()
        if allow_commands not in {"true", "false"}:
            raise ValueError("CHAGENT_ALLOW_HOST_COMMANDS must be true or false")
        sandbox_network = values.get("CHAGENT_SANDBOX_NETWORK", "false").lower()
        if sandbox_network not in {"true", "false"}:
            raise ValueError("CHAGENT_SANDBOX_NETWORK must be true or false")
        sandbox_tmp = values.get("CHAGENT_SANDBOX_WRITABLE_TMP", "true").lower()
        if sandbox_tmp not in {"true", "false"}:
            raise ValueError("CHAGENT_SANDBOX_WRITABLE_TMP must be true or false")
        return cls(
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
            sandbox_deny_read=values.get("CHAGENT_SANDBOX_DENY_READ", ""),
            sandbox_writable_tmp=sandbox_tmp == "true",
            approval=values.get("CHAGENT_APPROVAL", "never").strip().lower(),
            approval_allowlist=values.get("CHAGENT_APPROVAL_ALLOWLIST", ""),
            approval_timeout=float(values.get("CHAGENT_APPROVAL_TIMEOUT", "300")),
            max_processes=int(values.get("CHAGENT_MAX_PROCESSES", "4")),
            process_timeout=float(values.get("CHAGENT_PROCESS_TIMEOUT", "600")),
            web_access=values.get("CHAGENT_WEB_ACCESS", "off").strip().lower(),
            tavily_api_key=values.get("CHAGENT_TAVILY_API_KEY", "").strip(),
        )
