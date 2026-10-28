"""Concrete PostgreSQL operations for the minimal conversation/run lifecycle."""

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
from chagent.domain.runs import RunEndReason, RunStatus, StoredMessage, terminal_reason
from chagent.domain.sessions import MessagePage, Session, SessionPage, TaskState
from chagent.sessions.errors import SessionArchivedError, SessionBusyError, SessionNotFoundError, StateConflictError
from chagent.storage.database import Database
from chagent.storage.artifacts import ArtifactRepository, INLINE_BYTES
from chagent.tools.base import AgentToolResult


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
        limit: int = 50, before: UUID | None = None,
    ) -> SessionPage:
        if not 1 <= limit <= 100:
            raise ValueError("Session page limit must be between 1 and 100")
        clauses = []
        params = []
        if workspace is not None:
            clauses.append("w.root_path = %s")
            params.append(str(workspace.expanduser().resolve()))
        if not include_archived:
            clauses.append("s.archived_at IS NULL")
        if before is not None:
            anchor = await self.get_session(before)
            clauses.append("(s.created_at, s.id) < (%s, %s)")
            params.extend((anchor.created_at, anchor.id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = (
            "SELECT s.*, w.root_path FROM sessions s JOIN workspaces w ON w.id = s.workspace_id"
            + where + " ORDER BY s.created_at DESC, s.id DESC LIMIT %s"
        )
        async with self.database.connection() as connection:
            cursor = await connection.execute(query, (*params, limit + 1))
            rows = await cursor.fetchall()
        items = [self._session(row) for row in rows[:limit]]
        return SessionPage(items, items[-1].id if len(rows) > limit else None)

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

    async def _event(
        self, connection: AsyncConnection[DictRow], run_id: UUID,
        kind: str, payload: JsonObject,
    ) -> None:
        # Lock the run so concurrent writers cannot allocate the same sequence.
        await connection.execute("SELECT id FROM runs WHERE id = %s FOR UPDATE", (run_id,))
        await connection.execute(
            "INSERT INTO run_events (run_id, sequence, type, payload) "
            "SELECT %s, COALESCE(MAX(sequence), 0) + 1, %s, %s "
            "FROM run_events WHERE run_id = %s",
            (run_id, kind, Jsonb(payload), run_id),
        )

    async def begin(self, session_id: UUID, message: UserMessage) -> UUID:
        run_id = uuid4()
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
                    "INSERT INTO runs (id, session_id, status, started_at) VALUES (%s, %s, 'running', now())",
                    (run_id, session_id),
                )
                await self._message(connection, session_id, run_id, message)
                await self._event(connection, run_id, "run_started", {"session_id": str(session_id)})
                await connection.execute("UPDATE sessions SET updated_at = now() WHERE id = %s", (session_id,))
        except errors.UniqueViolation as error:
            if error.diag.constraint_name == "runs_one_active_per_session_idx":
                raise RunBusyError("Session already has an active run") from None
            raise
        return run_id

    async def event(self, run_id: UUID, kind: str, payload: JsonObject) -> None:
        async with self.database.transaction() as connection:
            await self._event(connection, run_id, kind, payload)

    async def start_model_call(self, run_id: UUID, iteration: int, built: BuiltContext) -> UUID:
        call_id = uuid4()
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO model_calls (id, run_id, iteration, request, context_snapshot) "
                "VALUES (%s, %s, %s, %s, %s)",
                (call_id, run_id, iteration, Jsonb(asdict(built.request)), Jsonb(asdict(built.snapshot))),
            )
            await self._event(connection, run_id, "model_started", {"model_call_id": str(call_id)})
        return call_id

    async def save_response(
        self, run_id: UUID, session_id: UUID, model_call_id: UUID, message: AssistantMessage,
    ) -> StoredMessage:
        payload = message_to_dict(message)
        status = "failed" if message.stop_reason == "error" else (
            "cancelled" if message.stop_reason == "aborted" else "completed"
        )
        async with self.database.transaction() as connection:
            await connection.execute(
                "UPDATE model_calls SET status = %s, response = %s, ended_at = now() "
                "WHERE id = %s AND run_id = %s", (status, Jsonb(payload), model_call_id, run_id),
            )
            record = await self._message(connection, session_id, run_id, message)
            await self._event(connection, run_id, "model_completed", {
                "model_call_id": str(model_call_id), "stop_reason": message.stop_reason,
            })
        return record

    async def queue_tool_calls(self, run_id: UUID, model_call_id: UUID, calls: list[ToolCall]) -> list[UUID]:
        identities = [uuid4() for _ in calls]
        async with self.database.transaction() as connection:
            for index, (identity, call) in enumerate(zip(identities, calls)):
                await connection.execute(
                    "INSERT INTO tool_calls (id, run_id, model_call_id, call_id, name, arguments, status, call_index) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s)",
                    (identity, run_id, model_call_id, call.id, call.name, Jsonb(call.arguments), index))
                await self._event(connection, run_id, "tool_queued", {"tool_call_id": call.id, "name": call.name})
        return identities

    async def start_tool_call(self, run_id: UUID, model_call_id: UUID, call: ToolCall, *, record_id: UUID) -> UUID:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE tool_calls SET status = 'running' WHERE id = %s AND run_id = %s "
                "AND model_call_id = %s AND call_id = %s AND status = 'queued' RETURNING id",
                (record_id, run_id, model_call_id, call.id))
            if await cursor.fetchone() is None:
                raise StateConflictError("Tool call is no longer queued")
            await self._event(connection, run_id, "tool_started", {
                "tool_call_id": call.id, "name": call.name,
            })
        return record_id

    async def save_tool_result(
        self, run_id: UUID, session_id: UUID, record_id: UUID,
        result: AgentToolResult, message: ToolResultMessage,
    ) -> StoredMessage:
        async with self.database.transaction() as connection:
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
            })
        return record

    async def finish(self, run_id: UUID, status: RunStatus, error: str | None = None, *,
                     end_reason: RunEndReason | None = None, on_completed=None) -> None:
        end_reason = terminal_reason(status, end_reason)
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE runs SET status = %s, error = %s, end_reason = %s, ended_at = now() "
                "WHERE id = %s AND status = 'running' RETURNING id", (status, error, end_reason, run_id),
            )
            if await cursor.fetchone() is None:
                raise RuntimeError("Run is missing or already terminal")
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
                    await self._event(connection, run_id, kind, {"tool_call_id": row["call_id"], "name": row["name"]})
            await connection.execute(
                "UPDATE model_calls SET status = %s, ended_at = now() "
                "WHERE run_id = %s AND status = 'running'",
                ("failed" if status == "completed" else status, run_id),
            )
            await connection.execute(
                "UPDATE compaction_calls SET status = %s, ended_at = now() WHERE run_id = %s AND status = 'running'",
                ("failed" if status == "completed" else status, run_id),
            )
            await connection.execute(
                "UPDATE memory_extractions SET status = %s, ended_at = now() WHERE run_id = %s AND status = 'running'",
                ("failed" if status == "completed" else status, run_id),
            )
            if status == "completed" and on_completed is not None:
                try:
                    async with connection.transaction():
                        await on_completed(connection)
                except Exception as memory_error:
                    await self._event(connection, run_id, "memory_write_failed", {"reason": type(memory_error).__name__})
            await self._event(connection, run_id, "run_" + status, {"error": error, "end_reason": end_reason})

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

    async def latest(self, session_id: UUID) -> DictRow | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM runs WHERE session_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
                (session_id,),
            )
            return await cursor.fetchone()
