"""Application-facing session operations; raw storage remains in repositories."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from uuid import UUID

from chagent.boundaries import validate_workspace
from chagent.domain.sessions import MessagePage, Session, SessionPage, TaskState, validate_mode
from chagent.domain.json_types import JsonObject
from chagent.domain.runs import StoredMessage
from chagent.storage.repositories import ConversationRepository


class SessionManager:
    def __init__(self, repository: ConversationRepository):
        self.repository = repository

    async def create(self, workspace: Path, title: str = "", *, mode: str = "work") -> Session:
        workspace = validate_workspace(workspace)
        if not workspace.is_dir():
            raise NotADirectoryError("Workspace must be a directory")
        title = self._title(title)
        identity = await self.repository.create_session(workspace, title, validate_mode(mode))
        return await self.get(identity)

    async def get(self, session_id: UUID) -> Session:
        session = await self.repository.get_session(session_id)
        validate_workspace(session.workspace)
        return session

    async def list(
        self, *, workspace: Path | None = None, include_archived: bool = False,
        archived: str | None = None, query: str | None = None,
        limit: int = 50, before: UUID | None = None,
    ) -> SessionPage:
        return await self.repository.list_sessions(
            workspace=workspace, include_archived=include_archived, archived=archived,
            query=query, limit=limit, before=before)

    async def rename(self, session_id: UUID, title: str) -> Session:
        await self.repository.rename_session(session_id, self._title(title))
        return await self.get(session_id)

    async def archive(self, session_id: UUID) -> Session:
        await self.repository.archive_session(session_id, True)
        return await self.get(session_id)

    async def restore(self, session_id: UUID) -> Session:
        await self.repository.archive_session(session_id, False)
        return await self.get(session_id)

    async def set_mode(self, session_id: UUID, mode: str) -> Session:
        await self.repository.set_session_mode(session_id, validate_mode(mode))
        return await self.get(session_id)

    async def messages(self, session_id: UUID, *, before: int | None = None, limit: int = 100) -> MessagePage:
        return await self.repository.message_page(session_id, before=before, limit=limit)

    async def message(self, session_id: UUID, message_id: UUID) -> StoredMessage | None:
        """One transcript record, used to resolve what a replacement replaces."""
        return await self.repository.message_record(session_id, message_id)

    async def task_state(self, session_id: UUID) -> TaskState:
        return await self.repository.task_state(session_id)

    async def contexts(self, session_id: UUID, *, limit: int = 10) -> list[JsonObject]:
        return await self.repository.context_snapshots(session_id, limit)

    async def update_task_state(self, session_id: UUID, state: TaskState, *, expected_revision: int) -> TaskState:
        if not isinstance(state.goal, str) or expected_revision < 0:
            raise ValueError("Invalid task goal or revision")
        for values in (state.constraints, state.decisions, state.pending, state.source_message_ids):
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError("Task state fields must be lists of strings")
        if len(json.dumps(asdict(state), ensure_ascii=False).encode("utf-8")) > 65_536:
            raise ValueError("Task state exceeds the 64 KiB limit")
        for value in state.source_message_ids:
            UUID(value)
        return await self.repository.update_task_state(session_id, state, expected_revision)

    @staticmethod
    def _title(title: str) -> str:
        if not isinstance(title, str) or len(title.strip()) > 200:
            raise ValueError("Session title must be a string of at most 200 characters")
        return title.strip()
