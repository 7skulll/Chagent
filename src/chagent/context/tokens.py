"""Replaceable token estimation; no provider-specific tokenizer dependency."""

from collections.abc import Sequence
from dataclasses import asdict
import json
from typing import Protocol

from chagent.domain.messages import Message


class TokenCounter(Protocol):
    """Estimator contract.

    ``additive`` is an opt-in promise that ``count(messages)`` equals the sum of
    ``count([message])`` for those messages. Builders use it to select turns from
    cumulative per-message costs instead of re-serializing every candidate. A
    counter that cannot promise this (for example one that models inter-message
    structure) simply omits the attribute and gets the exact, slower path.
    """

    name: str

    def count(self, messages: Sequence[Message]) -> int: ...


class ByteTokenCounter:
    """Conservative heuristic: UTF-8 JSON bytes plus per-message overhead.

    Not a provider tokenizer or a guarantee about a future wire format. The
    caller reserves an additional safety margin; real adapters can replace it.

    The per-message overhead is constant and messages do not interact, so this
    counter is exactly additive.
    """

    name = "utf8_json_bytes_estimate"
    additive = True

    def count(self, messages: Sequence[Message]) -> int:
        return sum(
            len(json.dumps(asdict(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 8
            for message in messages
        )
