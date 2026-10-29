"""Replaceable token counting: a real BPE encoder when available, bytes otherwise.

The byte estimate is not a tokenizer. It is roughly 4x too high on English and
close on Chinese, so the *same* task compacts at different points in different
languages: English gets summarized while the code the model just read is still
useful, and the model reads it again. That is a correctness problem, not a
cosmetic one.

The BPE counter is optional: ``tiktoken`` loads its vocabulary from a cache or
the network, and a machine without either must still run. When it is unavailable
the builder keeps working with bytes, and the snapshot records *both* numbers
under their own names, so the two are never mixed in one figure.
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict
import json
import os
import sys
from typing import Protocol

from chagent.domain.messages import Message, SystemMessage
from chagent.models.base import Model
from chagent.models.transcript import openai_message, openai_tools


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


class TokenizerUnavailable(RuntimeError):
    """The environment cannot provide a real tokenizer; callers fall back."""


def wire_text(message: Message) -> str:
    """The message in the shape the provider actually receives.

    Both counters measure this same text, so they stay comparable. It models the
    chat-completions adapter (the default for every preset except Anthropic);
    anything provider-specific and absent from that body — thinking blocks,
    host metadata, the ``timestamp`` — costs nothing here, because it costs
    nothing there either.

    Measured against real usage, serializing the dataclass instead of this body
    is what made the byte estimate 3.4x too high: the extra keys and the missing
    ``{"type":"function"}`` wrappers both push in the wrong direction.
    """
    if isinstance(message, SystemMessage):
        body = {"role": "system", "content": message.text()}
        tools = message.tools_added or []
        if tools:
            body["tools"] = openai_tools(tools)
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    try:
        return json.dumps(openai_message(message), ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        # An image tool result cannot be sent by this adapter at all; price the
        # dataclass so the budget still has a number, and let the adapter raise.
        return json.dumps(asdict(message), ensure_ascii=False, separators=(",", ":"))


class ByteTokenCounter:
    """Conservative fallback: UTF-8 JSON bytes plus per-message overhead.

    The per-message overhead is constant and messages do not interact, so this
    counter is exactly additive.
    """

    name = "utf8_json_bytes_estimate"
    additive = True

    def count(self, messages: Sequence[Message]) -> int:
        return sum(len(wire_text(message).encode("utf-8")) + 8 for message in messages)


class BpeTokenCounter:
    """A real encoder from the model's family, via the optional ``tiktoken``.

    The per-message constant covers role markers and separators that the JSON
    serialization does not spend tokens on; it keeps the counter additive so
    turn selection stays linear.
    """

    additive = True
    # Wrapper tokens the chat template adds around each message.
    MESSAGE_OVERHEAD = 4
    # Per-declaration boilerplate the provider adds around a tool schema that
    # our JSON body does not model. Calibrated on 5 real deepseek-flash calls
    # with 9 tools: without it, a first-turn prompt reads 15-17% short, while
    # later turns (where the same declarations are amortized over more messages)
    # read 2-8% short. It is a chat-completions figure, not a universal one.
    TOOL_DECLARATION_OVERHEAD = 40

    def __init__(self, encoding: str = "cl100k_base"):
        try:
            import tiktoken
        except ImportError as error:
            raise TokenizerUnavailable("tiktoken is not installed") from error
        try:
            self._encoder = tiktoken.get_encoding(encoding)
        except Exception as error:  # cache miss without network, or a bad name
            raise TokenizerUnavailable(f"tiktoken cannot load {encoding}: {type(error).__name__}") from error
        self.encoding = encoding
        self.name = f"tiktoken/{encoding}"

    def count(self, messages: Sequence[Message]) -> int:
        total = 0
        for message in messages:
            # disallowed_special=() keeps ordinary text that looks like a
            # special token from raising; the provider would tokenize it too.
            total += len(self._encoder.encode(wire_text(message), disallowed_special=())) + self.MESSAGE_OVERHEAD
            if isinstance(message, SystemMessage) and message.tools_added:
                total += self.TOOL_DECLARATION_OVERHEAD * len(message.tools_added)
        return total


# OpenAI-compatible families that use a larger vocabulary. Everything else is
# closer to cl100k; the choice only has to be within the acceptance band, and it
# is recorded by name in the snapshot either way.
O200K_PREFIXES = ("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4")


def encoding_for(model_id: str) -> str:
    lowered = model_id.strip().lower()
    return "o200k_base" if lowered.startswith(O200K_PREFIXES) else "cl100k_base"


def select_counter(model: Model, env: Mapping[str, str] | None = None) -> TokenCounter:
    """Pick a counter for this model; fall back to bytes and say so once.

    ``CHAGENT_TOKENIZER`` overrides the choice: ``bytes`` forces the fallback,
    any other value is treated as a ``tiktoken`` encoding name.
    """
    values = os.environ if env is None else env
    choice = values.get("CHAGENT_TOKENIZER", "").strip().lower()
    if choice in {"bytes", "off", "none"} or model.api == "fake":
        return ByteTokenCounter()
    try:
        return BpeTokenCounter(choice or encoding_for(model.id))
    except TokenizerUnavailable as error:
        # Visible on stderr, never in the transcript: the snapshot already says
        # which counter was used.
        print(f"Tokenizer: bytes (fallback; {error})", file=sys.stderr)
        return ByteTokenCounter()
