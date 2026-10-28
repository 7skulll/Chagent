"""Long-term facts, distinct from conversation summaries and task state."""

from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import math
import os
import re
from uuid import UUID


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    enabled: bool = True
    extract: bool = True
    max_tokens: int = 2048
    max_items: int = 8
    timeout: float = 20

    def __post_init__(self):
        if not 1 <= self.max_tokens <= 16000 or not 1 <= self.max_items <= 20:
            raise ValueError("Invalid memory budget")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Invalid memory extraction timeout")

    @classmethod
    def from_env(cls, env=None):
        values = os.environ if env is None else env
        flags = [values.get(name, "true").lower() for name in ("CHAGENT_MEMORY_ENABLED", "CHAGENT_MEMORY_EXTRACT")]
        if any(flag not in {"true", "false"} for flag in flags):
            raise ValueError("Memory flags must be true or false")
        try:
            return cls(flags[0] == "true", flags[1] == "true", int(values.get("CHAGENT_MEMORY_TOKENS", "2048")),
                       int(values.get("CHAGENT_MEMORY_ITEMS", "8")), float(values.get("CHAGENT_MEMORY_TIMEOUT", "20")))
        except (ValueError, OverflowError):
            raise ValueError("Invalid memory configuration") from None


@dataclass(slots=True)
class MemoryProposal:
    key: str
    content: str
    scope: str = "workspace"
    pinned: bool = False
    origin: str = "inferred"
    source_session_id: UUID | None = None
    source_run_id: UUID | None = None
    source_message_ids: list[str] = field(default_factory=list)
    evidence: str = ""


@dataclass(slots=True)
class Memory:
    id: UUID
    scope: str
    workspace_id: UUID | None
    key: str
    content: str
    content_hash: str
    status: str
    revision: int
    pinned: bool
    origin: str
    source_session_id: UUID | None
    source_run_id: UUID | None
    source_message_ids: list[str]
    evidence: str
    conflict_with: UUID | None
    created_at: datetime
    updated_at: datetime


def validate_memory(key, content, scope, pinned=False):
    if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", key):
        raise ValueError("Memory key must use 1–80 lowercase letters, digits, dots, hyphens or underscores")
    if not isinstance(content, str) or not content.strip() or len(content) > 2000 or "\x00" in content:
        raise ValueError("Memory content must contain 1–2000 characters")
    if not isinstance(scope, str) or scope not in {"user", "workspace"} or not isinstance(pinned, bool):
        raise ValueError("Invalid memory scope or pinned flag")
    if re.search(r"(?:api[_ -]?key|password|secret|access[_ -]?token|密码|密钥)\s*[:=：]\s*\S+|\bsk-[A-Za-z0-9_-]{8,}", content, re.I):
        raise ValueError("Credentials cannot be saved as memory")
    return content.strip()


def content_hash(content):
    return sha256(content.strip().encode("utf-8")).hexdigest()
