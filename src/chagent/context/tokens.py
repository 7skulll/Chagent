"""Replaceable token estimation; no provider-specific tokenizer dependency."""

from collections.abc import Sequence
from dataclasses import asdict
import json
from typing import Protocol

from chagent.domain.messages import Message


class TokenCounter(Protocol):
    name: str

    def count(self, messages: Sequence[Message]) -> int: ...


class ByteTokenCounter:
    """Conservative heuristic: UTF-8 JSON bytes plus per-message overhead.

    Not a provider tokenizer or a guarantee about a future wire format. The
    caller reserves an additional safety margin; real adapters can replace it.
    """

    name = "utf8_json_bytes_estimate"

    def count(self, messages: Sequence[Message]) -> int:
        return sum(
            len(json.dumps(asdict(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 8
            for message in messages
        )
