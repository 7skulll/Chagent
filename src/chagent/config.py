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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if env is None else env
        allow_commands = values.get("CHAGENT_ALLOW_HOST_COMMANDS", "false").lower()
        if allow_commands not in {"true", "false"}:
            raise ValueError("CHAGENT_ALLOW_HOST_COMMANDS must be true or false")
        return cls(
            database_url=values.get("CHAGENT_DATABASE_URL", "postgresql:///chagent"),
            db_pool_min_size=int(values.get("CHAGENT_DB_POOL_MIN_SIZE", "1")),
            db_pool_max_size=int(values.get("CHAGENT_DB_POOL_MAX_SIZE", "4")),
            db_timeout=float(values.get("CHAGENT_DB_TIMEOUT", "5")),
            allow_host_commands=allow_commands == "true",
            max_turns=int(values.get("CHAGENT_MAX_TURNS", "32")),
        )
