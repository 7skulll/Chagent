"""Request dependencies shared by every router.

Keeping ``Current`` here is what lets a router module import the dependency
without importing the application that mounts it, so ``web.app`` and its
routers stay a one-way tree instead of an import cycle.
"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Query, Request

from chagent.storage.repositories import ConversationRepository
from chagent.web.runtime import WebRuntime
from chagent.web.schemas import WorkspaceCreate


def runtime(request: Request) -> WebRuntime:
    return request.app.state.runtime


Current = Annotated[WebRuntime, Depends(runtime)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
MessageLimit = Annotated[int, Query(ge=1, le=200)]
#: A workspace comes from the client on every call that needs one; the startup
#: directory is the default, and anything else must be opened first.
WorkspacePath = Annotated[str | None, Query(max_length=4096)]
SequenceCursor = Annotated[int, Query(ge=0, le=9_223_372_036_854_775_807)]


async def open_workspace(value: WorkspaceCreate, current: WebRuntime):
    """Open one directory; from then on requests may name it as their workspace."""
    path = current.open_workspace(value.path)
    identity = await current.workspace_id(path)
    if value.touch:
        async with current.runner.conversations.database.connection() as connection:
            await connection.execute("UPDATE workspaces SET last_opened_at = now() WHERE id = %s", (identity,))
    repository = ConversationRepository(current.runner.conversations.database)
    return next((row for row in await repository.workspaces() if Path(row["path"]) == path),
                {"path": path, "sessions": 0})
