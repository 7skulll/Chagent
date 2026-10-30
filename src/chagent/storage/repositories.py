"""Concrete PostgreSQL operations for the minimal conversation/run lifecycle."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from psycopg import AsyncConnection, errors
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from chagent.context.builder import BuiltContext
from chagent.domain.codec import message_from_dict, message_to_dict
from chagent.domain.json_types import JsonObject
from chagent.domain.messages import AssistantMessage, Message, ToolCall, ToolResultMessage, UserMessage
from chagent.domain.recovery import MAX_TARGET, SIDE_EFFECT_TOOLS, RecoveryFact
from chagent.domain.runs import RunEndReason, RunStatus, StoredMessage, terminal_reason
from chagent.domain.sessions import MessagePage, Session, SessionPage, TaskState
from chagent.sessions.errors import SessionArchivedError, SessionBusyError, SessionNotFoundError, StateConflictError
from chagent.storage.database import Database
from chagent.storage.artifacts import ArtifactRepository, INLINE_BYTES
from chagent.storage.event_queue import EventItem, EventStream
from chagent.tools.base import AgentToolResult


class RunOwnershipLost(RuntimeError):
    """The current process no longer owns an active run."""


class RunBusyError(RuntimeError):
    """The session already owns an active run."""


class ConversationRepository:
    def __init__(self, database: Database):
        self.database = database

    async def create_session(self, workspace: Path, title: str = "") -> UUID:
        root = str(workspace.expanduser().resolve())
        session_id = uuid4()
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "INSERT INTO workspaces (id, root_path) VALUES (%s, %s) "
                "ON CONFLICT (root_path) DO UPDATE SET root_path = EXCLUDED.root_path RETURNING id",
                (uuid4(), root),
            )
            workspace_id = (await cursor.fetchone())["id"]
            await connection.execute(
                "INSERT INTO sessions (id, workspace_id, title) VALUES (%s, %s, %s)",
                (session_id, workspace_id, title),
            )
            await connection.execute("INSERT INTO task_states (session_id) VALUES (%s)", (session_id,))
        return session_id

    @staticmethod
    def _session(row: DictRow) -> Session:
        return Session(
            id=row["id"], workspace_id=row["workspace_id"], workspace=Path(row["root_path"]),
            title=row["title"], created_at=row["created_at"], updated_at=row["updated_at"], archived_at=row["archived_at"],
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
                "SELECT w.root_path AS path, count(s.id) AS sessions, max(s.updated_at) AS last_used "
                "FROM workspaces w LEFT JOIN sessions s ON s.workspace_id = w.id AND s.archived_at IS NULL "
                "GROUP BY w.root_path ORDER BY max(s.updated_at) DESC NULLS LAST, w.root_path"
            )
            return [{"path": row["path"], "sessions": row["sessions"]} for row in await cursor.fetchall()]

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

    async def message_page(self, session_id: UUID, *, before: int | None = None, limit: int = 100) -> MessagePage:
        if not 1 <= limit <= 200 or (before is not None and before < 1):
            raise ValueError("Invalid message page bounds")
        await self.get_session(session_id)
        query = "SELECT id, sequence, payload, run_id FROM messages WHERE session_id = %s"
        params: tuple = (session_id,)
        if before is not None:
            query += " AND sequence < %s"
            params += (before,)
        query += " ORDER BY sequence DESC LIMIT %s"
        async with self.database.connection() as connection:
            cursor = await connection.execute(query, (*params, limit + 1))
            rows = await cursor.fetchall()
        items = [StoredMessage(row["id"], row["sequence"], message_from_dict(row["payload"]), row["run_id"])
                 for row in reversed(rows[:limit])]
        return MessagePage(items, items[0].sequence if len(rows) > limit else None)

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

    async def workspace(self, session_id: UUID) -> Path:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT w.root_path FROM sessions s JOIN workspaces w ON w.id = s.workspace_id "
                "WHERE s.id = %s", (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError("Session does not exist")
            return Path(row["root_path"])

    async def messages(self, session_id: UUID, current_run: UUID | None = None, *, project_artifacts: bool = False) -> list[StoredMessage]:
        """All records for inspection, or completed/current-run history for the model.

        Failed or interrupted runs remain inspectable but are not replayed into
        the baseline context: they may contain unfinished tool-call batches.
        """
        payload = ("CASE WHEN jsonb_typeof(m.payload->'artifact') = 'object' THEN "
                   "(jsonb_set(m.payload, '{content}', jsonb_path_query_array(m.payload, '$.content[*] ? (@.type == \"image\")')) - 'details') "
                   "ELSE m.payload END" if project_artifacts else "m.payload")
        query = (
            "SELECT m.id, m.sequence, " + payload + " AS payload, m.run_id FROM messages m "
            "LEFT JOIN runs r ON r.id = m.run_id WHERE m.session_id = %s "
        )
        params: tuple = (session_id,)
        if current_run is not None:
            query += "AND (m.run_id IS NULL OR r.status = 'completed' OR r.id = %s) "
            params = (session_id, current_run)
        query += "ORDER BY m.sequence"
        async with self.database.connection() as connection:
            cursor = await connection.execute(query, params)
            return [StoredMessage(row["id"], row["sequence"], message_from_dict(row["payload"]), row["run_id"])
                    for row in await cursor.fetchall()]


class RunRepository:
    def __init__(self, database: Database):
        self.database = database
        self.notifications = database.notifications
        # Buffered high-frequency events, one stream per active run in this
        # process. Streams are never reconstructed from rows.
        self._streams: dict[UUID, EventStream] = {}

    async def check_owner(self, connection, run_id: UUID) -> DictRow:
        """The one ownership gate a write transaction needs.

        Validates token, run status and lease, and holds the run row lock. That
        lock is also what makes the ``MAX(sequence) + 1`` allocation in
        ``_insert_events`` safe, so the events written afterwards in the same
        transaction pass ``locked=True`` and cost one statement each instead of
        two. Calling this twice in one transaction buys nothing.
        """
        token = self.database.run_owners.get(run_id)
        cursor = await connection.execute(
            "SELECT id, session_id, status FROM runs WHERE id = %s AND status = 'running' "
            "AND owner_token = %s AND lease_expires_at > clock_timestamp() FOR UPDATE",
            (run_id, token),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RunOwnershipLost("Run ownership lost or lease expired")
        return row

    async def heartbeat(self, run_id: UUID, lease_seconds: float = 30) -> None:
        token = self.database.run_owners.get(run_id)
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE runs SET lease_expires_at = clock_timestamp() + make_interval(secs => %s) "
                "WHERE id = %s AND status = 'running' AND owner_token = %s "
                "AND lease_expires_at > clock_timestamp() RETURNING id",
                (lease_seconds, run_id, token),
            )
            if await cursor.fetchone() is None:
                raise RunOwnershipLost("Run ownership lost or lease expired")

    async def reap_expired(self, *, limit: int = 100) -> list[UUID]:
        """Atomically interrupt expired owned runs; never infer legacy ownership."""
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid recovery batch size")
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "SELECT id FROM runs WHERE status = 'running' AND owner_token IS NOT NULL "
                "AND lease_expires_at <= clock_timestamp() "
                "ORDER BY lease_expires_at, id LIMIT %s FOR UPDATE SKIP LOCKED", (limit,),
            )
            identities = [row["id"] for row in await cursor.fetchall()]
            for run_id in identities:
                await connection.execute(
                    "UPDATE runs SET status = 'interrupted', end_reason = 'owner_lost', "
                    "error = 'Run ownership lease expired', ended_at = now() WHERE id = %s", (run_id,),
                )
                cursor = await connection.execute(
                    "UPDATE tool_calls SET status = 'unknown', ended_at = now() "
                    "WHERE run_id = %s AND status = 'running' RETURNING call_id, name", (run_id,),
                )
                unknown = await cursor.fetchall()
                cursor = await connection.execute(
                    "UPDATE tool_calls SET status = 'cancelled', ended_at = now() "
                    "WHERE run_id = %s AND status = 'queued' RETURNING call_id, name", (run_id,),
                )
                queued = await cursor.fetchall()
                for kind, rows in (("tool_interrupted", unknown), ("tool_cancelled", queued)):
                    for row in rows:
                        await self._event(connection, run_id, kind,
                                          {"tool_call_id": row["call_id"], "name": row["name"]}, locked=True)
                for table in ("model_calls", "compaction_calls"):
                    await connection.execute(
                        f"UPDATE {table} SET status = 'interrupted', ended_at = now() "
                        "WHERE run_id = %s AND status = 'running'", (run_id,),
                    )
                await self._event(connection, run_id, "run_interrupted",
                                  {"error": "Run ownership lease expired", "end_reason": "owner_lost"}, locked=True)
        for run_id in identities:
            stream = self._streams.pop(run_id, None)
            if stream is not None:
                # The buffered tail belongs to a run that just lost its owner;
                # it must not be written after the interrupted event.
                stream.discard()
                await stream.close(flush=False)
            self.notifications.notify(run_id)
        return identities

    async def _message(
        self, connection: AsyncConnection[DictRow], session_id: UUID,
        run_id: UUID, message: Message,
    ) -> StoredMessage:
        identity, payload = uuid4(), message_to_dict(message)
        cursor = await connection.execute(
            "INSERT INTO messages (id, session_id, run_id, role, payload) VALUES (%s, %s, %s, %s, %s) RETURNING sequence",
            (identity, session_id, run_id, message.role, Jsonb(payload)),
        )
        return StoredMessage(identity, (await cursor.fetchone())["sequence"], message_from_dict(payload), run_id)

    async def _insert_events(
        self, connection: AsyncConnection[DictRow], run_id: UUID, items: list[EventItem],
    ) -> None:
        """Append events; the caller must already hold this run's row lock.

        In practice that means the transaction called ``check_owner``, or is
        the one that created or updated the run row. Without the lock two
        writers could read the same ``MAX(sequence)`` and collide on the
        ``(run_id, sequence)`` primary key; with it, one statement numbers the
        whole batch in input order.
        """
        # MAX(sequence) reads the pre-statement snapshot, so one statement can
        # number the whole batch with row_number() over the input order.
        await connection.execute(
            "INSERT INTO run_events (run_id, sequence, type, payload) "
            "SELECT %s, (SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id = %s) "
            "  + row_number() OVER (ORDER BY t.ord), t.kind, t.payload::jsonb "
            "FROM unnest(%s::text[], %s::text[]) WITH ORDINALITY AS t(kind, payload, ord)",
            (run_id, run_id, [kind for kind, _ in items],
             [json.dumps(payload, ensure_ascii=False) for _, payload in items]),
        )

    async def _event(
        self, connection: AsyncConnection[DictRow], run_id: UUID,
        kind: str, payload: JsonObject, *, locked: bool = False,
    ) -> None:
        """Append one event inside an existing transaction.

        ``locked`` says whether this transaction already holds the run row:
        ownership-gated writers pass ``True``; a writer that only adds audit
        events to a run it does not own (the memory worker, the reaper's
        successors) leaves it ``False`` and takes the lock here.
        """
        if not locked:
            await connection.execute("SELECT id FROM runs WHERE id = %s FOR UPDATE", (run_id,))
        await self._insert_events(connection, run_id, [(kind, payload)])

    def _stream(self, run_id: UUID) -> EventStream:
        stream = self._streams.get(run_id)
        if stream is None:
            async def write(items: list[EventItem]) -> None:
                async with self.database.transaction() as connection:
                    # One gate per batch: it validates the owner and holds the
                    # lock the batch insert allocates sequence numbers under.
                    await self.check_owner(connection, run_id)
                    await self._insert_events(connection, run_id, items)
                self.notifications.notify(run_id)

            stream = self._streams[run_id] = EventStream(run_id, write)
        return stream

    async def emit(self, run_id: UUID, kind: str, payload: JsonObject) -> None:
        """Queue a high-frequency event; nothing is written until a flush."""
        await self._stream(run_id).append(kind, payload)

    async def flush_events(self, run_id: UUID) -> None:
        """Commit buffered events, keeping them ordered before the next write."""
        stream = self._streams.get(run_id)
        if stream is not None:
            await stream.flush()

    def event_failure(self, run_id: UUID) -> asyncio.Future:
        """Awaited alongside the model stream: raises if a batch write failed.

        The stream is created here so a failure during the very first batch is
        observed too, not only after the first delta was appended.
        """
        return asyncio.ensure_future(self._stream(run_id).failure())

    async def close_events(self, run_id: UUID) -> None:
        stream = self._streams.pop(run_id, None)
        if stream is not None:
            await stream.close()

    async def _flush_before_terminal(self, run_id: UUID) -> None:
        """Commit buffered events before the terminal event is written.

        A batch rejected by the database would otherwise keep the run in
        ``running`` forever: those events are lost either way, so the terminal
        state still wins. Ownership loss is different and must propagate.
        """
        stream = self._streams.get(run_id)
        if stream is None:
            return
        try:
            await stream.flush()
        except RunOwnershipLost:
            raise
        except Exception:
            stream.discard()

    async def begin(self, session_id: UUID, message: UserMessage) -> UUID:
        run_id, owner = uuid4(), uuid4()
        try:
            async with self.database.transaction() as connection:
                cursor = await connection.execute(
                    "SELECT id, archived_at FROM sessions WHERE id = %s FOR UPDATE", (session_id,),
                )
                session = await cursor.fetchone()
                if session is None:
                    raise SessionNotFoundError("Session does not exist")
                if session["archived_at"] is not None:
                    raise SessionArchivedError("Restore the archived session before running it")
                await connection.execute(
                    "INSERT INTO runs (id, session_id, status, started_at, owner_token, lease_expires_at) "
                    "VALUES (%s, %s, 'running', now(), %s, clock_timestamp() + interval '30 seconds')",
                    (run_id, session_id, owner),
                )
                await self._message(connection, session_id, run_id, message)
                # This transaction created the run row, so it already holds the
                # lock the event insert needs.
                await self._event(connection, run_id, "run_started", {"session_id": str(session_id)}, locked=True)
                await connection.execute("UPDATE sessions SET updated_at = now() WHERE id = %s", (session_id,))
        except errors.UniqueViolation as error:
            if error.diag.constraint_name == "runs_one_active_per_session_idx":
                raise RunBusyError("Session already has an active run") from None
            raise
        self.database.run_owners[run_id] = owner
        self.notifications.notify(run_id)
        return run_id

    async def audit_event(self, run_id: UUID, kind: str, payload: JsonObject) -> None:
        await self.flush_events(run_id)
        async with self.database.transaction() as connection:
            await self._event(connection, run_id, kind, payload)
        self.notifications.notify(run_id)

    async def event(self, run_id: UUID, kind: str, payload: JsonObject) -> None:
        await self.flush_events(run_id)
        async with self.database.transaction() as connection:
            await self.check_owner(connection, run_id)
            await self._event(connection, run_id, kind, payload, locked=True)
        self.notifications.notify(run_id)

    async def start_model_call(self, run_id: UUID, iteration: int, built: BuiltContext) -> UUID:
        call_id = uuid4()
        await self.flush_events(run_id)
        async with self.database.transaction() as connection:
            await self.check_owner(connection, run_id)
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot) "
                "VALUES (%s, %s, %s, %s, %s)",
                (call_id, run_id, iteration, Jsonb(asdict(built.request)), Jsonb(asdict(built.snapshot))),
            )
            await self._event(connection, run_id, "model_started", {"model_call_id": str(call_id)}, locked=True)
        self.notifications.notify(run_id)
        return call_id

    async def save_response(
        self, run_id: UUID, session_id: UUID, model_call_id: UUID, message: AssistantMessage,
    ) -> StoredMessage:
        payload = message_to_dict(message)
        status = "failed" if message.stop_reason == "error" else (
            "cancelled" if message.stop_reason == "aborted" else "completed"
        )
        await self.flush_events(run_id)
        async with self.database.transaction() as connection:
            await self.check_owner(connection, run_id)
            await connection.execute(
                "UPDATE model_calls SET status = %s, response = %s, ended_at = now() "
                "WHERE id = %s AND run_id = %s", (status, Jsonb(payload), model_call_id, run_id),
            )
            record = await self._message(connection, session_id, run_id, message)
            await self._event(connection, run_id, "model_completed", {
                "model_call_id": str(model_call_id), "stop_reason": message.stop_reason,
            }, locked=True)
        self.notifications.notify(run_id)
        return record

    async def save_notice(self, run_id: UUID, session_id: UUID, message: AssistantMessage) -> StoredMessage:
        """Persist an assistant message that no model produced.

        A deterministic command still owes the transcript an answer, but there
        is no ``model_calls`` row to attach it to, so this writes the message
        alone under the same ownership gate as ``save_response``.
        """
        await self.flush_events(run_id)
        async with self.database.transaction() as connection:
            await self.check_owner(connection, run_id)
            record = await self._message(connection, session_id, run_id, message)
        self.notifications.notify(run_id)
        return record

    async def queue_tool_calls(self, run_id: UUID, model_call_id: UUID, calls: list[ToolCall]) -> list[UUID]:
        identities = [uuid4() for _ in calls]
        await self.flush_events(run_id)
        async with self.database.transaction() as connection:
            await self.check_owner(connection, run_id)
            for index, (identity, call) in enumerate(zip(identities, calls)):
                await connection.execute(
                    "INSERT INTO tool_calls (id, run_id, model_call_id, call_id, name, arguments, status, call_index) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s)",
                    (identity, run_id, model_call_id, call.id, call.name, Jsonb(call.arguments), index))
                await self._event(connection, run_id, "tool_queued", {"tool_call_id": call.id, "name": call.name},
                                  locked=True)
        self.notifications.notify(run_id)
        return identities

    async def start_tool_call(self, run_id: UUID, model_call_id: UUID, call: ToolCall, *, record_id: UUID) -> UUID:
        await self.flush_events(run_id)
        async with self.database.transaction() as connection:
            await self.check_owner(connection, run_id)
            cursor = await connection.execute(
                "UPDATE tool_calls SET status = 'running' WHERE id = %s AND run_id = %s "
                "AND model_call_id = %s AND call_id = %s AND status = 'queued' RETURNING id",
                (record_id, run_id, model_call_id, call.id))
            if await cursor.fetchone() is None:
                raise StateConflictError("Tool call is no longer queued")
            await self._event(connection, run_id, "tool_started", {
                "tool_call_id": call.id, "name": call.name,
            }, locked=True)
        self.notifications.notify(run_id)
        return record_id

    async def save_tool_result(
        self, run_id: UUID, session_id: UUID, record_id: UUID,
        result: AgentToolResult, message: ToolResultMessage,
    ) -> StoredMessage:
        await self.flush_events(run_id)
        async with self.database.transaction() as connection:
            await self.check_owner(connection, run_id)
            text = "\n".join(block.text for block in message.content if hasattr(block, "text"))
            if len(text.encode("utf-8")) > INLINE_BYTES:
                details = result.details if isinstance(result.details, dict) else {}
                message.artifact = await ArtifactRepository(self.database).create(
                    connection, session_id=session_id, run_id=run_id, tool_record_id=record_id, text=text,
                    source_truncated=bool(details.get("truncated") or details.get("output_truncated")),
                )
            await connection.execute(
                "UPDATE tool_calls SET status = %s, result = %s, ended_at = now() "
                "WHERE id = %s AND run_id = %s",
                ("unknown" if result.outcome_unknown else "failed" if result.is_error else "completed",
                 Jsonb(asdict(result)), record_id, run_id),
            )
            record = await self._message(connection, session_id, run_id, message)
            await self._event(connection, run_id, "tool_completed", {
                "tool_call_id": message.tool_call_id, "is_error": message.is_error,
                "outcome_unknown": result.outcome_unknown,
                "artifact_id": message.artifact.id if message.artifact else None,
            }, locked=True)
        self.notifications.notify(run_id)
        return record

    async def finish(self, run_id: UUID, status: RunStatus, error: str | None = None, *,
                     end_reason: RunEndReason | None = None, on_completed=None) -> None:
        end_reason = terminal_reason(status, end_reason)
        await self._flush_before_terminal(run_id)
        token = self.database.run_owners.get(run_id)
        async with self.database.transaction() as connection:
            # The terminal write is its own gate: a non-matching owner, a
            # terminal run or an expired lease updates nothing, so no separate
            # ownership probe is needed before it.
            cursor = await connection.execute(
                "UPDATE runs SET status = %s, error = %s, end_reason = %s, ended_at = now() "
                "WHERE id = %s AND status = 'running' AND owner_token = %s "
                "AND lease_expires_at > clock_timestamp() RETURNING id",
                (status, error, end_reason, run_id, token),
            )
            if await cursor.fetchone() is None:
                await self._reject_terminal_write(connection, run_id)
            cursor = await connection.execute(
                "UPDATE tool_calls SET status = 'unknown', ended_at = now() "
                "WHERE run_id = %s AND status = 'running' RETURNING call_id, name", (run_id,),
            )
            unknown = await cursor.fetchall()
            cursor = await connection.execute(
                "UPDATE tool_calls SET status = 'cancelled', ended_at = now() "
                "WHERE run_id = %s AND status = 'queued' RETURNING call_id, name", (run_id,))
            cancelled = await cursor.fetchall()
            for kind, rows in (("tool_interrupted", unknown), ("tool_cancelled", cancelled)):
                for row in rows:
                    await self._event(connection, run_id, kind,
                                      {"tool_call_id": row["call_id"], "name": row["name"]}, locked=True)
            await connection.execute(
                "UPDATE model_calls SET status = %s, ended_at = now() "
                "WHERE run_id = %s AND status = 'running'",
                ("failed" if status == "completed" else status, run_id),
            )
            await connection.execute(
                "UPDATE compaction_calls SET status = %s, ended_at = now() WHERE run_id = %s AND status = 'running'",
                ("failed" if status == "completed" else status, run_id),
            )
            # Extraction starts only after the run is terminal (queued in the
            # transaction below), so this run never has an attempt in flight.
            if status == "completed" and on_completed is not None:
                try:
                    async with connection.transaction():
                        await on_completed(connection)
                except Exception as memory_error:
                    await self._event(connection, run_id, "memory_write_failed",
                                      {"reason": type(memory_error).__name__}, locked=True)
            await self._event(connection, run_id, "run_" + status,
                              {"error": error, "end_reason": end_reason}, locked=True)
        self.notifications.notify(run_id)

    async def _reject_terminal_write(self, connection: AsyncConnection[DictRow], run_id: UUID) -> None:
        """Classify a rejected terminal update; only reached on the error path."""
        cursor = await connection.execute("SELECT status, owner_token FROM runs WHERE id = %s", (run_id,))
        row = await cursor.fetchone()
        if row is None or row["status"] != "running":
            raise RuntimeError("Run is missing or already terminal")
        raise RunOwnershipLost("Run ownership lost or lease expired")

    async def get(self, run_id: UUID) -> DictRow | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM runs WHERE id = %s", (run_id,))
            return await cursor.fetchone()

    async def events(self, run_id: UUID, after: int = 0, *, limit: int | None = None) -> list[DictRow]:
        if limit is not None and not 1 <= limit <= 1000:
            raise ValueError("Invalid event page limit")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT sequence, type, payload FROM run_events "
                "WHERE run_id = %s AND sequence > %s ORDER BY sequence LIMIT %s", (run_id, after, limit),
            )
            return await cursor.fetchall()

    async def terminal_sequence(self, run_id: UUID) -> int | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT min(sequence) AS sequence FROM run_events WHERE run_id = %s "
                "AND type IN ('run_completed', 'run_failed', 'run_cancelled', 'run_interrupted')", (run_id,))
            return (await cursor.fetchone())["sequence"]

    async def latest(self, session_id: UUID) -> DictRow | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM runs WHERE session_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
                (session_id,),
            )
            return await cursor.fetchone()

    async def active(self, *, roots: list[str] | None = None) -> list[DictRow]:
        """Runs that have not converged yet, with the tool each is running.

        ``roots`` limits the answer to workspaces this process has opened, the
        same boundary every other endpoint applies; ``None`` means no boundary
        and exists only for maintenance callers.
        """
        sql = (
            "SELECT r.id, r.session_id, r.status, r.started_at, "
            "  (SELECT t.name FROM tool_calls t WHERE t.run_id = r.id AND t.status = 'running' "
            "   ORDER BY t.created_at DESC, t.id LIMIT 1) AS current_tool "
            "FROM runs r JOIN sessions s ON s.id = r.session_id "
            "JOIN workspaces w ON w.id = s.workspace_id "
            "WHERE r.status IN ('queued', 'running')"
        )
        params: tuple = ()
        if roots is not None:
            sql += " AND w.root_path = ANY(%s)"
            params = (roots,)
        sql += " ORDER BY r.started_at, r.id"
        async with self.database.connection() as connection:
            cursor = await connection.execute(sql, params)
            return await cursor.fetchall()

    async def recovery_facts(self, session_id: UUID, *, current_run: UUID,
                             limit: int = 64) -> list[RecoveryFact]:
        """Committed side effects from runs that ended without a normal stop.

        Only failures after the latest completed run are read. Once the session
        moved on successfully, older failures are history, not context: without
        this boundary every continuation would re-inject the same stale facts.

        A ``completed`` tool call has a result the model already saw. An
        ``unknown`` one was started without a recorded outcome; it is reported as
        unknown and never as success, and nothing here is ever replayed.
        """
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "WITH boundary AS ("
                "  SELECT max(ended_at) AS since FROM runs"
                "  WHERE session_id = %s AND status = 'completed'"
                ") "
                "SELECT r.id AS run_id, r.status AS run_status, t.name, "
                "  t.status AS tool_status, t.call_index, "
                "  t.result->'details'->>'path' AS file_path, "
                "  t.result->'details'->>'sha256' AS sha256, "
                "  t.result->'details'->>'exit_code' AS exit_code, "
                "  t.arguments->>'path' AS argument_path, "
                "  t.arguments->>'cmd' AS command, "
                "  CASE WHEN jsonb_typeof(t.result->'details'->'files') = 'array' THEN "
                "    (SELECT string_agg(f.value->>'path', ', ') "
                "     FROM jsonb_array_elements(t.result->'details'->'files') AS f) END AS patch_paths, "
                "  a.id AS artifact_id "
                "FROM runs r JOIN tool_calls t ON t.run_id = r.id "
                "LEFT JOIN tool_artifacts a ON a.tool_record_id = t.id "
                "WHERE r.session_id = %s AND r.id <> %s "
                "  AND r.status IN ('failed', 'cancelled', 'interrupted') "
                "  AND r.ended_at IS NOT NULL "
                "  AND r.ended_at > COALESCE((SELECT since FROM boundary), '-infinity'::timestamptz) "
                "  AND t.status IN ('completed', 'unknown') "
                "  AND t.name = ANY(%s) "
                "ORDER BY r.ended_at, t.call_index LIMIT %s",
                (session_id, session_id, current_run, sorted(SIDE_EFFECT_TOOLS), limit),
            )
            rows = await cursor.fetchall()
        facts = []
        for row in rows:
            # A patch has no single target argument: its files live in the result.
            target = row["file_path"] or row["argument_path"] or row["patch_paths"] or row["command"]
            if not target:
                continue
            target = " ".join(str(target).split())
            if len(target) > MAX_TARGET:
                target = target[:MAX_TARGET - 1] + "…"
            if row["name"] == "exec_command":
                detail = f"exit {row['exit_code']}" if row["exit_code"] is not None else ""
            else:
                detail = f"sha256 {row['sha256'][:8]}" if row["sha256"] else ""
            facts.append(RecoveryFact(
                run_id=row["run_id"], run_status=row["run_status"], tool_name=row["name"],
                target=target, confirmed=row["tool_status"] == "completed",
                detail=detail, artifact_id=row["artifact_id"],
            ))
        return facts
