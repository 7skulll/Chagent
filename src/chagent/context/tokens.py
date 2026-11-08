"""One coarse token estimate over the wire body, corrected later by the provider.

The estimate is deliberately not a tokenizer. A real encoder needs a dependency
that may be missing (the container image has no extras), may fail to load its
vocabulary without network, and differs per provider anyway. Instead this module
does two things and nothing more:

1. :func:`estimate_tokens` turns a piece of wire text into a coarse token count.
2. :class:`HeuristicTokenCounter` applies it per message and stays additive, so
   turn selection stays linear.

Accuracy is not the point. :class:`chagent.domain.context.ObservedUsage` anchors
the next turn's estimate on the prompt size the provider actually reported, so
this number only has to be good enough for (a) the first request of a run, (b)
the messages appended since the last response, and (c) comparing candidate
turns against each other. Those are all "same ballpark" decisions.

Calibration (measured with the encoders available on 2026-11-06, 6 samples):

=============  ==================  ==================  ===============
sample         cl100k tokens        o200k tokens        ascii B/token
=============  ==================  ==================  ===============
en prose       201                 201                 4.48
python code    366                 366                 4.70
zh prose       780                 480                 —
json (en)      170                 170                 3.02
json (zh)      164                 154                 1.82
=============  ==================  ==================  ===============

Non-ASCII characters measured 0.75-1.44 tokens each, so this module charges one
token per non-ASCII character: it is close for cl100k_base, a deliberate
underestimate for o200k_base, and never the 3.4x overestimate that counting raw
UTF-8 bytes as tokens produced.
"""

from dataclasses import asdict
import json
from typing import Protocol

from chagent.domain.messages import Message, SystemMessage
from chagent.models.base import Model
from chagent.models.transcript import openai_message, openai_tools


#: ASCII characters per token. English prose and code measured 4.5-4.7; dense
#: JSON measured 3.0, because structure (`{"role":"toolResult",...}`) costs
#: tokens that carry no bytes of payload. 4 is the middle of that band.
ASCII_BYTES_PER_TOKEN = 4

#: Tokens per non-ASCII character. Measured 0.75 (o200k Chinese) to 1.44
#: (cl100k Chinese code comments). One token per character keeps CJK from being
#: charged as if it were English, which is the failure this replaced.
WIDE_CHAR_TOKENS = 1

#: Role markers and separators the chat template adds around each message.
MESSAGE_OVERHEAD = 10

#: Wrapper tokens the provider adds around a tool schema. Carried over from the
#: five-call deepseek-flash calibration (9 tools); it is a chat-completions
#: figure, not a universal one.
TOOL_DECLARATION_OVERHEAD = 40


class TokenCounter(Protocol):
    """Estimator contract.

    ``additive`` is an opt-in promise that ``count(messages)`` equals the sum of
    ``count([message])`` for those messages. Builders use it to select turns from
    cumulative per-message costs instead of re-serializing every candidate. A
    counter that cannot promise this (for example one that models inter-message
    structure) simply omits the attribute and gets the exact, slower path.
    """

    name: str

    def count(self, messages: list[Message]) -> int: ...


def wire_text(message: Message) -> str:
    """The message in the shape the provider actually receives.

    Anything provider-specific and absent from the chat-completions body —
    thinking blocks, host metadata, the ``timestamp`` — costs nothing here,
    because it costs nothing there either. Tool declarations are included because
    the adapter really sends them inside the system message.
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


def estimate_tokens(text: str) -> int:
    """Coarse token count for one piece of wire text."""
    if not text:
        return 0
    if text.isascii():
        return -(-len(text) // ASCII_BYTES_PER_TOKEN)
    # ``encode("ascii", "ignore")`` drops exactly the non-ASCII characters, so
    # its length is both the ASCII character count and the ASCII byte count.
    ascii_bytes = len(text.encode("ascii", "ignore"))
    wide_chars = len(text) - ascii_bytes
    return -(-ascii_bytes // ASCII_BYTES_PER_TOKEN) + wide_chars * WIDE_CHAR_TOKENS


class HeuristicTokenCounter:
    """Chars-and-bytes heuristic; additive over messages, so selection is linear."""

    name = "wire_tokens_ascii4_wide1"
    additive = True

    def count(self, messages: list[Message]) -> int:
        total = 0
        for message in messages:
            total += estimate_tokens(wire_text(message)) + MESSAGE_OVERHEAD
            if isinstance(message, SystemMessage) and message.tools_added:
                total += TOOL_DECLARATION_OVERHEAD * len(message.tools_added)
        return total


def select_counter(model: Model) -> TokenCounter:
    """One estimate for every model; the provider's own count corrects it later.

    ``model`` is accepted so call sites do not have to know that the choice has
    stopped depending on the model family, and so a future encoder can be
    selected here without touching them.
    """
    return HeuristicTokenCounter()
