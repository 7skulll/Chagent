"""Run-scoped projection of persisted messages into their model-visible form.

A projection is computed once per message instead of once per build: stripping
host metadata, substituting artifact text, capping an oversized tool result and
estimating cost are all pure functions of the stored message. The cache is keyed
by the persisted message identity, which never changes for a given row.
"""

from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from chagent.context.tokens import TokenCounter
from chagent.context.truncate import truncate_middle
from chagent.domain.artifacts import artifact_text
from chagent.domain.context import DEFAULT_MAX_TOOL_RESULT_TOKENS
from chagent.domain.messages import AssistantMessage, ImageContent, Message, TextContent, ToolResultMessage
from chagent.domain.runs import StoredMessage


PROJECTION_VERSION = 2


@dataclass(frozen=True, slots=True)
class Projection:
    """A message's model-visible form plus its share of the input budget.

    ``cost`` is only meaningful for counters that promise additivity; for any
    other counter it is 0 and the builder re-renders to count exactly.
    ``omitted_chars`` records how much of a tool result the cap removed, so the
    snapshot can say that the model saw an excerpt rather than the whole result.
    """

    message: Message
    cost: int
    counter: str
    omitted_chars: int = 0
    version: int = PROJECTION_VERSION


class ProjectionCache:
    """Bounded store of projected messages for one run.

    Every entry is a private copy, so a caller that mutates a request cannot
    corrupt later builds.
    """

    def __init__(self, limit: int = 4096, *,
                 max_tool_result_tokens: int = DEFAULT_MAX_TOOL_RESULT_TOKENS):
        if limit < 1:
            raise ValueError("Projection cache limit must be positive")
        if max_tool_result_tokens < 1:
            raise ValueError("Tool result token limit must be positive")
        self.limit = limit
        self.max_tool_result_tokens = max_tool_result_tokens
        self._items: dict[UUID, Projection] = {}

    def __len__(self) -> int:
        return len(self._items)

    def project(self, item: StoredMessage, counter: TokenCounter) -> Projection:
        cached = self._items.get(item.id)
        if cached is not None and cached.counter == counter.name and cached.version == PROJECTION_VERSION:
            return cached
        message, omitted = self._visible(item.message)
        # Only additive counters consume a per-message cost; counting for the
        # others would serialize every message for a number nobody reads.
        cost = counter.count([message]) if getattr(counter, "additive", False) else 0
        projection = Projection(message, cost, counter.name, omitted)
        if len(self._items) >= self.limit:
            self._items.clear()
        self._items[item.id] = projection
        return projection

    def _visible(self, message: Message) -> tuple[Message, int]:
        """Copy with host-only fields removed; large text stays artifact-backed."""
        visible = deepcopy(message)
        omitted = 0
        if isinstance(visible, ToolResultMessage):
            if visible.artifact is not None:
                # The reference already carries a bounded excerpt; nothing to cap.
                visible.content = [TextContent(artifact_text(visible.artifact)),
                                   *(block for block in visible.content if isinstance(block, ImageContent))]
                visible.artifact = None
            else:
                visible.content, omitted = self._capped(visible.content)
            # Host/UI metadata must not silently become model context.
            visible.details = None
            visible.usage = None
            visible.duration_ms = None
        if isinstance(visible, AssistantMessage):
            visible.usage = None
            visible.duration_ms = None
        return visible, omitted

    def _capped(self, content) -> tuple[list, int]:
        """Cap the whole result's text, keeping image blocks as they are."""
        text = "\n".join(block.text for block in content if isinstance(block, TextContent))
        kept, omitted = truncate_middle(text, self.max_tool_result_tokens)
        if not omitted:
            return content, 0
        return [TextContent(kept), *(block for block in content if isinstance(block, ImageContent))], omitted
