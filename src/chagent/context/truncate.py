"""Middle truncation of one oversized tool result for its model-visible form.

Selection can only drop whole user turns, because a turn that keeps its tool
call without its result is not a context the provider accepts. That leaves one
single tool result able to decide whether an entire earlier turn survives. This
module caps the individual item instead, in tokens rather than bytes, so the
same limit means the same thing for English output and Chinese output.

The stored result is never changed: this runs on the projection, and the marker
tells the model how to ask for the rest. Head and tail are kept because failures
read from the top and summaries/exit codes read from the bottom.
"""

from chagent.context.tokens import estimate_tokens


MARKER = ("\n\n[... {omitted} characters omitted from the middle of this tool result. "
          "The stored result is unchanged; request a narrower range (offset/limit on a file, "
          "a smaller max_output_chars, or a more specific path) to read the omitted part ...]\n\n")

#: How far to look for a line break when snapping a cut, so the head does not end
#: mid-line and the tail does not start mid-line.
LINE_SNAP_CHARS = 200


def truncate_middle(text: str, max_tokens: int) -> tuple[str, int]:
    """Return ``(text, omitted_chars)`` with at most ``max_tokens`` estimated tokens.

    The returned text is unchanged (and ``omitted_chars`` is 0) when it already
    fits, so callers can use it unconditionally.
    """
    if max_tokens < 1:
        raise ValueError("Tool result token limit must be positive")
    if estimate_tokens(text) <= max_tokens:
        return text, 0
    budget = max_tokens - estimate_tokens(MARKER.format(omitted=len(text)))
    budget = max(1, budget)
    # Largest k such that the kept head and tail fit the remaining budget.
    low, high = 0, len(text) // 2
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle] + text[-middle:]) <= budget:
            low = middle
        else:
            high = middle - 1
    keep = low
    head = text[:keep]
    tail = text[-keep:] if keep else ""
    if head and (newline := head.rfind("\n", max(0, len(head) - LINE_SNAP_CHARS))) != -1:
        head = head[:newline]
    if tail and (newline := tail.find("\n", 0, LINE_SNAP_CHARS)) != -1:
        tail = tail[newline + 1:]
    omitted = len(text) - len(head) - len(tail)
    if omitted <= 0:
        return text, 0
    return head + MARKER.format(omitted=omitted) + tail, omitted
