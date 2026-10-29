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
        )
