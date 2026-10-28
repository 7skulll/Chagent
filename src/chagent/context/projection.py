"""Run-scoped projection of persisted messages into their model-visible form.

A projection is computed once per message instead of once per build: stripping
host metadata, substituting artifact text and estimating cost are all pure
functions of the stored message. The cache is keyed by the persisted message
identity, which never changes for a given row.
"""

from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from chagent.context.tokens import TokenCounter
from chagent.domain.artifacts import artifact_text
from chagent.domain.messages import AssistantMessage, ImageContent, Message, TextContent, ToolResultMessage
from chagent.domain.runs import StoredMessage


PROJECTION_VERSION = 1


@dataclass(frozen=True, slots=True)
class Projection:
    message: Message
    cost: int
    counter: str
    version: int = PROJECTION_VERSION


class ProjectionCache:
    """Bounded store of projected messages for one run.

    Every entry is a private copy, so a caller that mutates a request cannot
    corrupt later builds. ``retain`` drops messages that left the history.
    """

    def __init__(self, limit: int = 4096):
        if limit < 1:
            raise ValueError("Projection cache limit must be positive")
        self.limit = limit
        self._items: dict[UUID, Projection] = {}

    def __len__(self) -> int:
        return len(self._items)

    def project(self, item: StoredMessage, counter: TokenCounter) -> Projection:
        cached = self._items.get(item.id)
        if cached is not None and cached.counter == counter.name and cached.version == PROJECTION_VERSION:
            return cached
        message = self._visible(item.message)
        projection = Projection(message, counter.count([message]), counter.name)
        if len(self._items) >= self.limit:
            self._items.clear()
        self._items[item.id] = projection
        return projection

    def retain(self, identities: set[UUID]) -> None:
        for identity in list(self._items):
            if identity not in identities:
                del self._items[identity]

    @staticmethod
    def _visible(message: Message) -> Message:
        """Copy with host-only fields removed; large text stays artifact-backed."""
        visible = deepcopy(message)
        if isinstance(visible, ToolResultMessage):
            if visible.artifact is not None:
                visible.content = [TextContent(artifact_text(visible.artifact)),
                                   *(block for block in visible.content if isinstance(block, ImageContent))]
                visible.artifact = None
            # Host/UI metadata must not silently become model context.
            visible.details = None
            visible.usage = None
            visible.duration_ms = None
        if isinstance(visible, AssistantMessage):
            visible.usage = None
            visible.duration_ms = None
        return visible
