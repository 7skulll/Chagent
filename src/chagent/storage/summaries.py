"""Append-only summary checkpoints and independently audited model calls."""

from dataclasses import asdict
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from chagent.domain.codec import message_from_dict, message_to_dict
from chagent.domain.runs import StoredMessage
from chagent.context.builder import BasicContextBuilder
from chagent.domain.json_types import JsonObject
from chagent.domain.messages import AssistantMessage
from chagent.domain.summaries import SessionSummary
from chagent.domain.usage import Usage, UsageCost
from chagent.models.base import ModelRequest
from chagent.sessions.errors import StateConflictError
from chagent.storage.database import Database
from chagent.storage.repositories import RunRepository


class SummaryRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _summary(row) -> SessionSummary:
        values = dict(row)
        if values["usage"] is not None:
            usage = dict(values["usage"])
            if usage.get("cost") is not None:
                usage["cost"] = UsageCost(**usage["cost"])
            values["usage"] = Usage(**usage)
        return SessionSummary(**values)

    async def latest(self, session_id: UUID, current_run: UUID | None = None) -> SessionSummary | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM session_summaries s WHERE session_id = %s AND "
                "(source_run_id IS NULL OR source_run_id = %s OR EXISTS "
                "(SELECT 1 FROM runs r WHERE r.id = s.source_run_id AND r.status = 'completed')) "
                "ORDER BY revision DESC LIMIT 1", (session_id, current_run),
            )
            row = await cursor.fetchone()
            return self._summary(row) if row else None

    async def next_revision(self, session_id: UUID) -> int:
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM session_summaries WHERE session_id = %s", (session_id,))
            return (await cursor.fetchone())["revision"]

    async def list(self, session_id: UUID, limit: int = 10) -> list[SessionSummary]:
        if not 1 <= limit <= 100:
            raise ValueError("Summary limit must be between 1 and 100")
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM session_summaries WHERE session_id = %s ORDER BY revision DESC LIMIT %s", (session_id, limit),
            )
            return [self._summary(row) for row in await cursor.fetchall()]

    async def save(self, summary: SessionSummary, run_id: UUID, *, completed_event: JsonObject | None = None) -> SessionSummary:
        async with self.database.transaction() as connection:
            await RunRepository(self.database).check_owner(connection, run_id)
            await connection.execute("SELECT id FROM sessions WHERE id = %s FOR UPDATE", (summary.session_id,))
            cursor = await connection.execute(
                "SELECT id, revision, covered_through_sequence, source_run_id FROM session_summaries s "
                "WHERE session_id = %s AND (source_run_id IS NULL OR source_run_id = %s OR EXISTS "
                "(SELECT 1 FROM runs r WHERE r.id = s.source_run_id AND r.status = 'completed')) "
                "ORDER BY revision DESC LIMIT 1", (summary.session_id, run_id),
            )
            previous = await cursor.fetchone()
            cursor = await connection.execute("SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM session_summaries WHERE session_id = %s", (summary.session_id,))
            revision = (await cursor.fetchone())["revision"]
            if (previous["id"] if previous else None) != summary.previous_summary_id or summary.revision != revision:
                raise StateConflictError("Summary changed while compaction was running")
            boundary = previous["covered_through_sequence"] if previous else 0
            if summary.covered_through_sequence <= boundary:
                raise ValueError("Summary boundary must advance")
            cursor = await connection.execute("SELECT session_id, status FROM runs WHERE id = %s FOR UPDATE", (run_id,))
            run = await cursor.fetchone()
            if run is None or run["session_id"] != summary.session_id or run["status"] != "running":
                raise StateConflictError("Compaction run is no longer active")
            cursor = await connection.execute(
                "SELECT m.id, m.run_id, m.sequence, m.role, m.payload, r.status FROM messages m LEFT JOIN runs r ON r.id = m.run_id "
                "WHERE m.session_id = %s AND m.sequence > %s AND m.sequence <= %s AND m.role <> 'system' ORDER BY m.sequence",
                (summary.session_id, boundary, summary.covered_through_sequence),
            )
            rows = [row for row in await cursor.fetchall() if (row["run_id"] is None or row["status"] == "completed" or row["run_id"] == run_id)
                    and not (row["run_id"] == run_id and row["role"] == "user")]
            if not rows or rows[-1]["sequence"] != summary.covered_through_sequence or [str(row["id"]) for row in rows] != summary.source_message_ids:
                raise StateConflictError("Summary sources changed or contain uncompleted runs")
            BasicContextBuilder._validate([StoredMessage(row["id"], row["sequence"], message_from_dict(row["payload"]), row["run_id"]) for row in rows])
            active = any(row["run_id"] == run_id for row in rows) or (previous is not None and previous["source_run_id"] == run_id)
            if summary.source_run_id != (run_id if active else None):
                raise ValueError("Summary active-run provenance is inconsistent")
            if active:
                cursor = await connection.execute("SELECT id FROM messages WHERE run_id = %s AND role = 'user' ORDER BY sequence LIMIT 1", (run_id,))
                original_input = await cursor.fetchone()
                if original_input is None or summary.protected_input_id != original_input["id"]:
                    raise ValueError("Active summary must retain its original input")
            elif summary.protected_input_id is not None:
                raise ValueError("Historical summary has an unexpected protected input")
            # Completed historical runs remain whole; active runs can cut only closed exchanges.
            cursor = await connection.execute(
                "SELECT 1 FROM messages WHERE session_id = %s AND run_id = ANY(%s) AND sequence > %s LIMIT 1",
                (summary.session_id, list({row["run_id"] for row in rows if row["run_id"] is not None and row["run_id"] != run_id}), summary.covered_through_sequence),
            )
            if await cursor.fetchone():
                raise ValueError("Summary must preserve complete runs")
            cursor = await connection.execute(
                "SELECT id, role FROM messages WHERE session_id = %s AND sequence > %s AND role <> 'system' "
                "AND (run_id IS NULL OR run_id = %s OR run_id IN (SELECT id FROM runs WHERE status = 'completed')) "
                "ORDER BY sequence LIMIT 1", (summary.session_id, summary.covered_through_sequence, run_id),
            )
            kept = await cursor.fetchone()
            if kept is None or kept["id"] != summary.first_kept_message_id or kept["role"] not in {"user", "assistant"}:
                raise StateConflictError("Summary kept boundary changed")
            await connection.execute(
                "INSERT INTO session_summaries (id, session_id, revision, previous_summary_id, covered_through_sequence, "
                "first_kept_message_id, content, model, provider, prompt_version, compaction_id, source_message_ids, usage, source_run_id, protected_input_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (summary.id, summary.session_id, summary.revision, summary.previous_summary_id,
                 summary.covered_through_sequence, summary.first_kept_message_id, summary.content, summary.model,
                 summary.provider, summary.prompt_version, summary.compaction_id, Jsonb(summary.source_message_ids),
                 Jsonb(asdict(summary.usage)) if summary.usage else None, summary.source_run_id, summary.protected_input_id),
            )
            if completed_event is not None:
                # The run row is already locked: checkpoint and activation event commit together.
                await connection.execute(
                    "INSERT INTO run_events (run_id, sequence, type, payload) "
                    "SELECT %s, COALESCE(MAX(sequence), 0) + 1, 'compaction_completed', %s "
                    "FROM run_events WHERE run_id = %s", (run_id, Jsonb(completed_event), run_id),
                )
        if completed_event is not None:
            self.database.notifications.notify(run_id)
        return summary

    async def start_call(self, compaction_id: UUID, run_id: UUID, iteration: int, request: ModelRequest) -> UUID:
        identity = uuid4()
        async with self.database.transaction() as connection:
            await RunRepository(self.database).check_owner(connection, run_id)
            await connection.execute(
                "INSERT INTO compaction_calls (id, compaction_id, run_id, iteration, request) VALUES (%s, %s, %s, %s, %s)",
                (identity, compaction_id, run_id, iteration, Jsonb(asdict(request))),
            )
        return identity

    async def finish_call(self, identity: UUID, status: str, response: AssistantMessage | None,
                          duration_ms: int, error: str | None = None):
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("Invalid compaction call status")
        async with self.database.transaction() as connection:
            cursor = await connection.execute("SELECT run_id FROM compaction_calls WHERE id = %s", (identity,))
            row = await cursor.fetchone()
            if row is None:
                raise StateConflictError("Compaction call missing")
            await RunRepository(self.database).check_owner(connection, row["run_id"])
            await connection.execute(
                "UPDATE compaction_calls SET status = %s, response = %s, duration_ms = %s, error = %s, ended_at = now() "
                "WHERE id = %s AND status = 'running'",
                (status, Jsonb(message_to_dict(response)) if response else None, duration_ms, error, identity),
            )
