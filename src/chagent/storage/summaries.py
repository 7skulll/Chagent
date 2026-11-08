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
from chagent.storage.repositories import RunOwnershipLost, RunRepository


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

    async def save(self, summary: SessionSummary, run_id: UUID, *, completed_event: JsonObject | None = None,
                   post_run_token: UUID | None = None) -> SessionSummary:
        async with self.database.transaction() as connection:
            # One gate: it validates the owner, locks the run row and returns
            # the columns the checkpoint needs, so the run is not read twice.
            # ``post_run_token`` selects the gate for the writer that finished the
            # run and is compacting it afterwards; the checks below are identical.
            runs = RunRepository(self.database)
            run = (await runs.check_owner(connection, run_id) if post_run_token is None
                   else await runs.check_finished_owner(connection, run_id, post_run_token))
            if run["session_id"] != summary.session_id:
                raise StateConflictError("Compaction run belongs to another session")
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

    async def record_calls(self, run_id: UUID, records, *, post_run_token: UUID | None = None) -> None:
        """Insert summarization calls that were captured off the critical path.

        The rows are written already terminal, because by adoption time they are.
        The same ownership gate as ``start_call`` decides whether they may exist,
        so a lost lease cannot attach audit rows to a run someone else owns. A
        post-turn pass names its own token instead, because by then the run is
        completed — the calls still belong to it, and their cost stays visible.
        """
        records = [record for record in records if record.response is not None or record.error is not None]
        if not records:
            return
        token = post_run_token if post_run_token is not None else self.database.run_owners.get(run_id)
        # One writer, expressed for whichever state the run is in. The fragment is
        # a constant: no caller input reaches it.
        gate = ("r.status = 'completed' AND r.owner_token = %s" if post_run_token is not None
                else "r.status = 'running' AND r.owner_token = %s AND r.lease_expires_at > clock_timestamp()")
        async with self.database.transaction() as connection:
            for record in records:
                cursor = await connection.execute(
                    "INSERT INTO compaction_calls (id, compaction_id, run_id, iteration, status, request, response, error, duration_ms, ended_at) "
                    "SELECT %s, %s, r.id, %s, %s, %s, %s, %s, %s, now() FROM runs r "
                    "WHERE r.id = %s AND " + gate,
                    (uuid4(), record.compaction_id, record.iteration, record.status, Jsonb(asdict(record.request)),
                     Jsonb(message_to_dict(record.response)) if record.response else None,
                     record.error, record.duration_ms, run_id, token),
                )
                if cursor.rowcount == 0:
                    raise RunOwnershipLost("Run ownership lost or lease expired")

    async def start_call(self, compaction_id: UUID, run_id: UUID, iteration: int, request: ModelRequest) -> UUID:
        identity = uuid4()
        token = self.database.run_owners.get(run_id)
        async with self.database.transaction() as connection:
            # Conditional insert: the run row decides whether the call may
            # exist, so nothing has to be read before writing.
            cursor = await connection.execute(
                "INSERT INTO compaction_calls (id, compaction_id, run_id, iteration, request) "
                "SELECT %s, %s, r.id, %s, %s FROM runs r WHERE r.id = %s AND r.status = 'running' "
                "AND r.owner_token = %s AND r.lease_expires_at > clock_timestamp()",
                (identity, compaction_id, iteration, Jsonb(asdict(request)), run_id, token),
            )
            if cursor.rowcount == 0:
                raise RunOwnershipLost("Run ownership lost or lease expired")
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
            token = self.database.run_owners.get(row["run_id"])
            cursor = await connection.execute(
                "UPDATE compaction_calls c SET status = %s, response = %s, duration_ms = %s, error = %s, ended_at = now() "
                "FROM runs r WHERE r.id = c.run_id AND c.id = %s AND c.status = 'running' "
                "AND r.status = 'running' AND r.owner_token = %s AND r.lease_expires_at > clock_timestamp() "
                "RETURNING c.id",
                (status, Jsonb(message_to_dict(response)) if response else None, duration_ms, error, identity, token),
            )
            if await cursor.fetchone() is None:
                await self._reject_call_write(connection, identity)

    async def _reject_call_write(self, connection, identity: UUID) -> None:
        """Classify a rejected call update; only reached on the error path."""
        cursor = await connection.execute(
            "SELECT c.status AS call_status, r.status AS run_status FROM compaction_calls c "
            "LEFT JOIN runs r ON r.id = c.run_id WHERE c.id = %s", (identity,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise StateConflictError("Compaction call missing")
        if row["call_status"] != "running":
            raise StateConflictError("Compaction call is no longer running")
        raise RunOwnershipLost("Run ownership lost or lease expired")
