"""State one scheduling fact the model cannot otherwise know: which turn this is.

The model sees one request per turn and nothing in it says how many turns are
left. Two failures follow. It cannot plan a long task against a horizon it
cannot see, and it cannot decide to converge — the loop simply stops when
``range(max_turns)`` is exhausted, wherever the task happens to be.

This is a normal trailing user message rather than a system section. The system
prefix is the provider's cache prefix: rewriting it every turn would invalidate
the entire cached request, while a message at the tail only affects the tail.
It is also never persisted. It describes one scheduling decision, and a
replayed transcript carrying stale counters would assert something false about
the run reading it.
"""

from chagent.domain.messages import UserMessage

CONVERGE_AT = 0.5
FINISH_AT = 0.8

#: Every note starts with this, so a recorded request can be searched for
#: scheduling notes and a test can assert one is present without matching the
#: whole sentence.
PREFIX = "[turn "


def turn_note(iteration: int, max_turns: int) -> str:
    """The note for a zero-based ``iteration`` of a ``max_turns`` loop."""
    if iteration < 0 or max_turns < 1:
        raise ValueError("Turn note needs a nonnegative iteration and a positive limit")
    turn = min(iteration + 1, max_turns)
    ratio = turn / max_turns
    remaining = max_turns - turn
    label = f"{PREFIX}{turn}/{max_turns}]"
    left = (f"{remaining} turn remains" if remaining == 1 else f"{remaining} turns remain")
    # It sits after the last tool result, so it must say plainly that it is not
    # a new instruction: the model is meant to continue its own task, not to
    # answer this line.
    if ratio >= FINISH_AT:
        return (f"{label} Harness progress note, not a new request. {left}. "
                "Stop exploring: make the smallest change that finishes the task, verify once, "
                "then state plainly what is done and what is not.")
    if ratio >= CONVERGE_AT:
        return (f"{label} Harness progress note, not a new request. {left}. "
                "Prefer fewer, larger steps, keep the workspace runnable, and do not re-read "
                "what you already read.")
    return f"{label} Harness progress note, not a new request. {left}."


def is_turn_note(message) -> bool:
    """True for a scheduling note, so a reader can find the real last message.

    The note is a user message because that is what the provider has room for
    at the end of a request, but it is not something the user said. Anything
    that decides from "the last user message" — a test double, a fixture —
    has to look past it.
    """
    return (isinstance(message, UserMessage) and isinstance(message.content, str)
            and message.content.startswith(PREFIX))


def last_request_message(messages):
    """The message to answer: the last one that is not a scheduling note."""
    for message in reversed(messages):
        if not is_turn_note(message):
            return message
    if not messages:
        raise ValueError("Request has no messages")
    return messages[-1]
