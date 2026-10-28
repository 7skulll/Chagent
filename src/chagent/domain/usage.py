"""Provider-reported token accounting, following Pi's Usage type."""

from dataclasses import dataclass


@dataclass(slots=True)
class UsageCost:
    """Reported monetary amounts; an absent cost means unknown, not free."""

    input: float
    output: float
    cache_read: float
    cache_write: float
    total: float


@dataclass(slots=True)
class Usage:
    """Input counts exclude separately reported cache reads and writes.

    Reasoning tokens are a subset of output, not an additional charge.

    ``cache_write_1h`` is a subset of ``cache_write``. Keep provider totals
    rather than recomputing them across differing accounting conventions.
    """

    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int
    cost: UsageCost | None = None
    cache_write_1h: int | None = None
    reasoning: int | None = None
