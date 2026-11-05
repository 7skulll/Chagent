"""Persisted approval questions and single-use grants.

The database is the authority. A broker that waits holds no connection between
polls, a decision is a conditional update (so a race between approval and expiry
is resolved by PostgreSQL, not by two in-memory timers), and a grant is consumed
by a conditional update immediately before execution so an approval can never be
replayed for a second call.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from psycopg.rows import DictRow

from chagent.domain.json_types import JsonObject
from chagent.storage.database import Database
from chagent.storage.repositories import RunOwnershipLost, RunRepository


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: UUID
    run_id: UUID
    tool_record_id: UUID
    fingerprint: str
    tool_name: str
    cwd: str
    subject: str
    status: str
    note: str | None
    expires_at: datetime
    #: Set when this question was shown together with others in one card. Every
    #: row keeps its own fingerprint and consumption; only the decision is shared.
    batch_id: UUID | None = None
    batch_index: int | None = None
    #: When the single grant was spent. Read by tests and by the batch view; a
    #: row can be approved and never consumed, which is a distinct state.
    consumed_at: datetime | None = None

    @classmethod
    def of(cls, row: DictRow) -> "ApprovalRequest":
        return cls(row["id"], row["run_id"], row["tool_record_id"], row["fingerprint"],
                   row["tool_name"], row["cwd"], row["subject"], row["status"],
                   row["note"], row["expires_at"], row.get("batch_id"), row.get("batch_index"),
                   row.get("consumed_at"))


class ApprovalRepository:
    def __init__(self, database: Database, runs: RunRepository):
        self.database, self.runs = database, runs

    async def create(self, *, run_id: UUID, tool_record_id: UUID, fingerprint: str,
                     tool_name: str, cwd: str, subject: str, ttl: float) -> ApprovalRequest:
        """Create the question once; a second attempt returns the pending row."""
        await self.runs.flush_events(run_id)
        async with self.database.transaction() as connection:
            await self.runs.check_owner(connection, run_id)
            token = self.database.run_owners.get(run_id)
            cursor = await connection.execute(
                "INSERT INTO approval_requests (id, run_id, tool_record_id, fingerprint, tool_name, "
                "cwd, subject, owner_token, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
                "clock_timestamp() + make_interval(secs => %s)) "
                "ON CONFLICT (tool_record_id) WHERE status = 'pending' DO NOTHING "
                "RETURNING *",
                (uuid4(), run_id, tool_record_id, fingerprint, tool_name, cwd, subject, token, ttl),
            )
            row = await cursor.fetchone()
            if row is not None:
                await self.runs._event(connection, run_id, "approval_requested", {
                    "approval_id": str(row["id"]), "tool_call_id": str(tool_record_id),
                    "tool_name": tool_name, "subject": subject, "cwd": cwd,
                }, locked=True)
        self.runs.notifications.notify(run_id)
        if row is not None:
            return ApprovalRequest.of(row)
        existing = await self.pending_for_tool(tool_record_id)
        if existing is None:
            raise RuntimeError("Pending approval disappeared while creating it")
        return existing

    async def create_batch(self, *, run_id: UUID, items: list[dict], ttl: float) -> list[ApprovalRequest]:
        """Insert every question of one model turn, sharing one batch id.

        All rows are written in one transaction with the same deadline, so the
        page decision can later update them together and no row can be born
        already expired relative to its siblings.
        """
        await self.runs.flush_events(run_id)
        batch_id = uuid4()
        async with self.database.transaction() as connection:
            await self.runs.check_owner(connection, run_id)
            token = self.database.run_owners.get(run_id)
            rows = []
            for index, item in enumerate(items):
                cursor = await connection.execute(
                    "INSERT INTO approval_requests (id, run_id, tool_record_id, fingerprint, tool_name, "
                    "cwd, subject, owner_token, expires_at, batch_id, batch_index) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp() + make_interval(secs => %s), %s, %s) "
                    "ON CONFLICT (tool_record_id) WHERE status = 'pending' DO NOTHING RETURNING *",
                    (uuid4(), run_id, item["tool_record_id"], item["fingerprint"], item["tool_name"],
                     item["cwd"], item["subject"], token, ttl, batch_id, index),
                )
                row = await cursor.fetchone()
                if row is None:
                    # A retry of the same batch: reuse the pending rows already
                    # written instead of opening a second question per call.
                    cursor = await connection.execute(
                        "SELECT * FROM approval_requests WHERE tool_record_id = %s AND status = 'pending'",
                        (item["tool_record_id"],))
                    row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("Pending approval disappeared while creating its batch")
                rows.append(row)
            # One event for the whole batch: the page shows one card, and three
            # events would draw three. The per-call rows are the audit record.
            first = rows[0]
            await self.runs._event(connection, run_id, "approval_requested", {
                "approval_id": str(first["id"]), "tool_call_id": str(first["tool_record_id"]),
                "tool_name": first["tool_name"], "subject": first["subject"], "cwd": first["cwd"],
                "batch_id": str(first["batch_id"]) if first["batch_id"] else None,
                "batch_size": len(rows),
            }, locked=True)
        self.runs.notifications.notify(run_id)
        return [ApprovalRequest.of(row) for row in rows]

    async def get(self, approval_id: UUID) -> ApprovalRequest | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM approval_requests WHERE id = %s", (approval_id,))
            row = await cursor.fetchone()
        return ApprovalRequest.of(row) if row else None

    async def pending_for_tool(self, tool_record_id: UUID) -> ApprovalRequest | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM approval_requests WHERE tool_record_id = %s AND status = 'pending'",
                (tool_record_id,))
            row = await cursor.fetchone()
        return ApprovalRequest.of(row) if row else None

    async def pending(self, run_id: UUID) -> list[ApprovalRequest]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM approval_requests WHERE run_id = %s AND status = 'pending' "
                "ORDER BY created_at", (run_id,))
            return [ApprovalRequest.of(row) for row in await cursor.fetchall()]

    async def expire_due(self, *, run_id: UUID | None = None) -> int:
        """Retire questions whose deadline passed; the run may still continue."""
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE approval_requests SET status = 'expired', decided_at = clock_timestamp() "
                "WHERE status = 'pending' AND expires_at <= clock_timestamp() "
                + ("AND run_id = %s " if run_id else "") +
                "RETURNING id, run_id", ((run_id,) if run_id else ()))
            rows = await cursor.fetchall()
            for row in rows:
                await self.runs._event(connection, row["run_id"], "approval_expired",
                                       {"approval_id": str(row["id"])}, locked=False)
        for row in rows:
            self.runs.notifications.notify(row["run_id"])
        return len(rows)

    async def cancel_pending(self, run_id: UUID, note: str = "run_ended") -> int:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE approval_requests SET status = 'cancelled', note = %s, "
                "decided_at = clock_timestamp() WHERE run_id = %s AND status = 'pending' "
                "RETURNING id", (note, run_id))
            rows = await cursor.fetchall()
            for row in rows:
                await self.runs._event(connection, run_id, "approval_cancelled",
                                       {"approval_id": str(row["id"]), "reason": note}, locked=False)
        self.runs.notifications.notify(run_id)
        return len(rows)

    async def decide(self, approval_id: UUID, *, approve: bool, note: str | None = None) -> str:
        """Approve or deny a pending, unexpired question. Returns the new status.

        When the question belongs to a batch, the same decision is applied to
        every still-pending row of that batch in one statement: the page showed
        them together, so they are answered together. Rows that already expired
        stay expired — a late sibling must not be revived by this update.

        Requires the run to still be running: approving a question whose run has
        already ended would grant nothing and must not resurrect it.
        """
        status = "approved" if approve else "denied"
        async with self.database.transaction() as connection:
            target = await connection.execute(
                "SELECT batch_id FROM approval_requests WHERE id = %s", (approval_id,))
            row = await target.fetchone()
            if row is None:
                return "conflict"
            if row["batch_id"] is None:
                cursor = await connection.execute(
                    "UPDATE approval_requests a SET status = %s, note = %s, decided_at = clock_timestamp() "
                    "FROM runs r WHERE a.id = %s AND a.run_id = r.id AND a.status = 'pending' "
                    "AND a.expires_at > clock_timestamp() AND r.status = 'running' "
                    "RETURNING a.id, a.run_id, a.tool_name", (status, note, approval_id))
                decided = await cursor.fetchall()
            else:
                cursor = await connection.execute(
                    "UPDATE approval_requests a SET status = %s, note = %s, decided_at = clock_timestamp() "
                    "FROM runs r WHERE a.batch_id = %s AND a.run_id = r.id AND a.status = 'pending' "
                    "AND a.expires_at > clock_timestamp() AND r.status = 'running' "
                    "RETURNING a.id, a.run_id, a.tool_name", (status, note, row["batch_id"]))
                decided = await cursor.fetchall()
            if not decided:
                # Already answered, already expired, or the run ended: nothing
                # was changed, and the caller must not treat this as a decision.
                return "conflict"
            for decided_row in decided:
                await self.runs._event(connection, decided_row["run_id"], "approval_decided", {
                    "approval_id": str(decided_row["id"]), "decision": status, "note": note,
                }, locked=False)
        self.runs.notifications.notify(decided[0]["run_id"])
        return status

    async def batch(self, batch_id: UUID) -> list[ApprovalRequest]:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM approval_requests WHERE batch_id = %s ORDER BY batch_index", (batch_id,))
            return [ApprovalRequest.of(row) for row in await cursor.fetchall()]

    async def consume(self, approval_id: UUID, *, run_id: UUID, tool_record_id: UUID,
                      fingerprint: str | None = None, tool_name: str | None = None,
                      arguments: dict | None = None, cwd: str | None = None) -> bool:
        """Claim an approved grant exactly once, for the invocation it was issued for."""
        if fingerprint is None:
            if tool_name is None or arguments is None or cwd is None:
                raise ValueError("Approval consumption needs an exact invocation")
            from chagent.tools.policy import fingerprint as invocation_fingerprint
            fingerprint = invocation_fingerprint(tool_name, arguments, cwd)
        await self.runs.flush_events(run_id)
        async with self.database.transaction() as connection:
            try:
                await self.runs.check_owner(connection, run_id)
            except RunOwnershipLost:
                return False
            cursor = await connection.execute(
                "UPDATE approval_requests SET consumed_at = clock_timestamp() "
                "WHERE id = %s AND run_id = %s AND tool_record_id = %s AND fingerprint = %s AND status = 'approved' "
                "AND consumed_at IS NULL AND expires_at > clock_timestamp() RETURNING id",
                (approval_id, run_id, tool_record_id, fingerprint))
            if await cursor.fetchone() is None:
                return False
            await self.runs._event(connection, run_id, "approval_consumed",
                                   {"approval_id": str(approval_id)}, locked=True)
        self.runs.notifications.notify(run_id)
        return True

    async def waiting_for(self, run_id: UUID) -> JsonObject:
        """The projection the API and UI show instead of a new run status."""
        pending = await self.pending(run_id)
        if not pending:
            return {"waiting_for": None}
        first = pending[0]
        items = pending if first.batch_id is not None else [first]
        return {"waiting_for": "approval", "approval_id": str(first.id),
                "approval": {"id": str(first.id), "tool_name": first.tool_name,
                             "subject": first.subject, "cwd": first.cwd,
                             "expires_at": first.expires_at.isoformat(),
                             "batch_id": str(first.batch_id) if first.batch_id else None,
                             "items": [{"id": str(item.id), "tool_name": item.tool_name,
                                        "subject": item.subject} for item in items]}}
