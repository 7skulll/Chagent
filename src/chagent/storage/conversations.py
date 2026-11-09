"""Session state and transcript reads: what one conversation owns.

Split out of :mod:`chagent.storage.repositories` on 2026-11-09. The split is
positional, not semantic: no statement, ordering or docstring changed.
"""

from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from chagent.domain.codec import message_from_dict
from chagent.domain.json_types import JsonObject
from chagent.domain.runs import StoredMessage
from chagent.domain.sessions import MessagePage, Session, SessionPage, TaskState, validate_mode
from chagent.sessions.errors import SessionBusyError, SessionNotFoundError, StateConflictError
from chagent.storage.database import Database


class ConversationRepository:
    def __init__(self, database: Database):
        self.database = database

    async def create_session(self, workspace: Path, title: str = "", mode: str = "work") -> UUID:
        root = str(workspace.expanduser().resolve())
        mode = validate_mode(mode)
        session_id = uuid4()
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "INSERT INTO workspaces (id, root_path) VALUES (%s, %s) "
                "ON CONFLICT (root_path) DO UPDATE SET root_path = EXCLUDED.root_path RETURNING id",
                (uuid4(), root),
            )
            workspace_id = (await cursor.fetchone())["id"]
            await connection.execute(
                "INSERT INTO sessions (id, workspace_id, title, mode) VALUES (%s, %s, %s, %s)",
                (session_id, workspace_id, title, mode),
            )
            await connection.execute("INSERT INTO task_states (session_id) VALUES (%s)", (session_id,))
        return session_id

    @staticmethod
    def _session(row: DictRow) -> Session:
        return Session(
            id=row["id"], workspace_id=row["workspace_id"], workspace=Path(row["root_path"]),
            title=row["title"], created_at=row["created_at"], updated_at=row["updated_at"],
            archived_at=row["archived_at"], mode=row["mode"],
        )

    async def get_session(self, session_id: UUID) -> Session:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT s.*, w.root_path FROM sessions s JOIN workspaces w ON w.id = s.workspace_id WHERE s.id = %s",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise SessionNotFoundError("Session does not exist")
            return self._session(row)

    async def list_sessions(
        self, *, workspace: Path | None = None, include_archived: bool = False,
        archived: str | None = None, query: str | None = None,
        limit: int = 50, before: UUID | None = None,
    ) -> SessionPage:
        """One page of sessions, optionally searching titles.

        ``archived`` is the browser's three-way switch; ``include_archived``
        remains the older two-way switch the command line uses. The archived
        count is returned alongside so the sidebar can label its own toggle
        without a second request.
        """
        if not 1 <= limit <= 100:
            raise ValueError("Session page limit must be between 1 and 100")
        mode = archived or ("include" if include_archived else "hide")
        if mode not in {"hide", "include", "only"}:
            raise ValueError("archived must be hide, include or only")
        clauses = []
        params = []
        count_where = ["s.archived_at IS NOT NULL"]
        count_params = []
        if workspace is not None:
            clauses.append("w.root_path = %s")
            params.append(str(workspace.expanduser().resolve()))
            count_where.append("w.root_path = %s")
            count_params.append(str(workspace.expanduser().resolve()))
        if mode == "hide":
            clauses.append("s.archived_at IS NULL")
        elif mode == "only":
            clauses.append("s.archived_at IS NOT NULL")
        text = (query or "").strip()
        if text:
            # ILIKE is case-insensitive; the escapes keep a typed % or _ literal
            # instead of letting it widen the match.
            pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("s.title ILIKE %s")
            params.append(pattern)
            count_where.append("s.title ILIKE %s")
            count_params.append(pattern)
        if before is not None:
            anchor = await self.get_session(before)
            clauses.append("(s.created_at, s.id) < (%s, %s)")
            params.extend((anchor.created_at, anchor.id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query_text = (
            "SELECT s.*, w.root_path FROM sessions s JOIN workspaces w ON w.id = s.workspace_id"
            + where + " ORDER BY s.created_at DESC, s.id DESC LIMIT %s"
        )
        async with self.database.connection() as connection:
            cursor = await connection.execute(query_text, (*params, limit + 1))
            rows = await cursor.fetchall()
            archived_count = None
            if mode != "only":
                cursor = await connection.execute(
                    "SELECT count(*) AS n FROM sessions s JOIN workspaces w ON w.id = s.workspace_id "
                    "WHERE " + " AND ".join(count_where), tuple(count_params))
                archived_count = (await cursor.fetchone())["n"]
        items = [self._session(row) for row in rows[:limit]]
        return SessionPage(items, items[-1].id if len(rows) > limit else None, archived_count)

    async def workspaces(self) -> list[dict]:
        """Known workspaces with their live session counts, newest activity first.

        The row is a hint for the picker, not a permission: opening a directory
        is what makes it addressable, and that decision lives in the web layer.
        """
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT w.id, w.root_path AS path, count(s.id) AS sessions, w.last_opened_at AS last_used "
                "FROM workspaces w LEFT JOIN sessions s ON s.workspace_id = w.id AND s.archived_at IS NULL "
                "GROUP BY w.id ORDER BY w.last_opened_at DESC, w.root_path"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def _lock_session(self, connection: AsyncConnection[DictRow], session_id: UUID, *, idle: bool = False) -> None:
        cursor = await connection.execute("SELECT archived_at FROM sessions WHERE id = %s FOR UPDATE", (session_id,))
        if await cursor.fetchone() is None:
            raise SessionNotFoundError("Session does not exist")
        if idle:
            cursor = await connection.execute(
                "SELECT 1 FROM runs WHERE session_id = %s AND status IN ('queued', 'running') LIMIT 1", (session_id,),
            )
            if await cursor.fetchone() is not None:
                raise SessionBusyError("Wait for the active run before changing session state")

    async def rename_session(self, session_id: UUID, title: str) -> None:
        async with self.database.transaction() as connection:
            await self._lock_session(connection, session_id)
            await connection.execute("UPDATE sessions SET title = %s, updated_at = now() WHERE id = %s", (title, session_id))

    async def archive_session(self, session_id: UUID, archived: bool) -> None:
        async with self.database.transaction() as connection:
            await self._lock_session(connection, session_id, idle=True)
            await connection.execute(
                "UPDATE sessions SET archived_at = CASE WHEN %s THEN COALESCE(archived_at, now()) ELSE NULL END, "
                "updated_at = now() WHERE id = %s", (archived, session_id),
            )

    async def set_session_mode(self, session_id: UUID, mode: str) -> None:
        """Switch the face later runs of this session resolve.

        Idle-locked on purpose: a run pins its face when it starts, so switching
        under it would make the prompt and the executing face disagree mid-run.
        The history keeps whatever the earlier face did; it is a record, not a
        claim about the current tools.
        """
        if mode not in {"work", "code"}:
            raise ValueError("Session mode must be work or code")
        async with self.database.transaction() as connection:
            await self._lock_session(connection, session_id, idle=True)
            await connection.execute(
                "UPDATE sessions SET mode = %s, updated_at = now() WHERE id = %s", (mode, session_id))

    async def message_page(self, session_id: UUID, *, before: int | None = None, limit: int = 100) -> MessagePage:
        if not 1 <= limit <= 200 or (before is not None and before < 1):
            raise ValueError("Invalid message page bounds")
        await self.get_session(session_id)
        query = ("SELECT id, sequence, payload, run_id, superseded_by FROM messages "
                 "WHERE session_id = %s")
        params: tuple = (session_id,)
        # Retired records stay in the transcript view on purpose: the point of
        # keeping them is being able to unfold the answer that was replaced.
        if before is not None:
            query += " AND sequence < %s"
            params += (before,)
        query += " ORDER BY sequence DESC LIMIT %s"
        async with self.database.connection() as connection:
            cursor = await connection.execute(query, (*params, limit + 1))
            rows = await cursor.fetchall()
        items = [StoredMessage(row["id"], row["sequence"], message_from_dict(row["payload"]), row["run_id"],
                              row["superseded_by"])
                 for row in reversed(rows[:limit])]
        return MessagePage(items, items[0].sequence if len(rows) > limit else None)

    async def message_record(self, session_id: UUID, message_id: UUID) -> StoredMessage | None:
        """One message of this session, or ``None`` when it is not there.

        The caller needs the sequence to retire a suffix, so this is a single
        row read rather than a page scan.
        """
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT id, sequence, payload, run_id, superseded_by FROM messages "
                "WHERE session_id = %s AND id = %s", (session_id, message_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return StoredMessage(row["id"], row["sequence"], message_from_dict(row["payload"]), row["run_id"],
                             row["superseded_by"])

    async def task_state(self, session_id: UUID) -> TaskState:
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT payload, revision FROM task_states WHERE session_id = %s", (session_id,))
            row = await cursor.fetchone()
            if row is None:
                raise SessionNotFoundError("Session task state does not exist")
            return TaskState(**row["payload"], revision=row["revision"])

    async def context_snapshots(self, session_id: UUID, limit: int = 10) -> list[JsonObject]:
        if not 1 <= limit <= 100:
            raise ValueError("Context snapshot limit must be between 1 and 100")
        await self.get_session(session_id)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT m.id, m.run_id, m.iteration, m.context_snapshot FROM model_calls m "
                "JOIN runs r ON r.id = m.run_id WHERE r.session_id = %s "
                "ORDER BY m.created_at DESC, m.id DESC LIMIT %s", (session_id, limit),
            )
            return [{"model_call_id": str(row["id"]), "run_id": str(row["run_id"]),
                     "iteration": row["iteration"], "snapshot": row["context_snapshot"]}
                    for row in await cursor.fetchall()]

    async def run_memory_references(self, session_id: UUID) -> list[dict]:
        """Which memories each run of one session was given, with their text.

        Read from the first model call of each run: its context snapshot is the
        record of what the model actually saw. The transcript does not carry
        this, because memory is deliberately not a message.
        """
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT DISTINCT ON (m.run_id) m.run_id, "
                "  m.context_snapshot->'memory_references' AS refs FROM model_calls m "
                "JOIN runs r ON r.id = m.run_id WHERE r.session_id = %s "
                "ORDER BY m.run_id, m.created_at, m.id", (session_id,),
            )
            rows = await cursor.fetchall()
            wanted: dict[UUID, list[UUID]] = {}
            for row in rows:
                identities = [UUID(str(item["id"])) for item in (row["refs"] or [])
                              if isinstance(item, dict) and item.get("id")]
                if identities:
                    wanted[row["run_id"]] = identities
            if not wanted:
                return []
            cursor = await connection.execute(
                "SELECT id, key, scope, revision, content, pinned, origin FROM memories "
                "WHERE id = ANY(%s)", ([item for group in wanted.values() for item in group],),
            )
            memories = {row["id"]: {**row, "id": str(row["id"])} for row in await cursor.fetchall()}
        # Snapshot order is recall order, which is relevance; keep it.
        return [{"run_id": run_id,
                 "memories": [memories[identity] for identity in identities if identity in memories]}
                for run_id, identities in wanted.items()]

    async def update_task_state(self, session_id: UUID, state: TaskState, expected_revision: int) -> TaskState:
        payload = asdict(state)
        payload.pop("revision")
        async with self.database.transaction() as connection:
            await self._lock_session(connection, session_id, idle=True)
            if state.source_message_ids:
                identities = [UUID(value) for value in state.source_message_ids]
                cursor = await connection.execute(
                    "SELECT id FROM messages WHERE session_id = %s AND id = ANY(%s)", (session_id, identities),
                )
                found = {row["id"] for row in await cursor.fetchall()}
                if found != set(identities):
                    raise ValueError("Task state sources must belong to this session")
            cursor = await connection.execute(
                "UPDATE task_states SET payload = %s, revision = revision + 1, updated_at = now() "
                "WHERE session_id = %s AND revision = %s RETURNING revision",
                (Jsonb(payload), session_id, expected_revision),
            )
            row = await cursor.fetchone()
            if row is None:
                raise StateConflictError("Task state changed; reload before saving")
            await connection.execute("UPDATE sessions SET updated_at = now() WHERE id = %s", (session_id,))
            return TaskState(**payload, revision=row["revision"])

    async def messages(self, session_id: UUID, current_run: UUID | None = None, *, project_artifacts: bool = False) -> list[StoredMessage]:
        """All records for inspection, or completed/current-run history for the model.

        Failed or interrupted runs remain inspectable but are mostly not
        replayed into the baseline context: they may contain unfinished
        tool-call batches. The two exceptions both end *after* a committed
        batch — ``execute_batch`` returns, results are stored and paired with
        their calls, and only then can the loop find that it is out of turns or
        that the model repeated itself. Replaying them is what stops a run that
        hit the turn limit from restarting blind: without it the next run sees
        only the recovery note, re-reads what it already read, and spends its
        budget arriving at the same place.
        """
        replayable = "('turn_limit', 'repeated_tool_call')"
        payload = ("CASE WHEN jsonb_typeof(m.payload->'artifact') = 'object' THEN "
                   "(jsonb_set(m.payload, '{content}', jsonb_path_query_array(m.payload, '$.content[*] ? (@.type == \"image\")')) - 'details') "
                   "ELSE m.payload END" if project_artifacts else "m.payload")
        query = (
            "SELECT m.id, m.sequence, " + payload + " AS payload, m.run_id, m.superseded_by FROM messages m "
            "LEFT JOIN runs r ON r.id = m.run_id WHERE m.session_id = %s "
        )
        params: tuple = (session_id,)
        if current_run is not None:
            # A replay is a linear story: a record replaced by a later run is
            # not part of it, even though the row is still readable above.
            query += ("AND m.superseded_by IS NULL "
                      "AND (m.run_id IS NULL OR r.status = 'completed' "
                      f"OR r.end_reason IN {replayable} OR r.id = %s) ")
            params = (session_id, current_run)
        query += "ORDER BY m.sequence"
        async with self.database.connection() as connection:
            cursor = await connection.execute(query, params)
            return [StoredMessage(row["id"], row["sequence"], message_from_dict(row["payload"]), row["run_id"],
                                  row["superseded_by"])
                    for row in await cursor.fetchall()]
