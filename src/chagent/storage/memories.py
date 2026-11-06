"""Memory records with optimistic revisions and serialized per-key writes."""

from dataclasses import asdict
import json
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from chagent.domain.memories import Memory, MemoryProposal, content_hash, validate_memory
from chagent.sessions.errors import StateConflictError
from chagent.storage.database import Database


class MemoryRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    async def _lock(connection, scope, workspace_id, key):
        namespace = f"memory:{scope}:{workspace_id if scope == 'workspace' else ''}:{key}"
        await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (namespace,))

    @staticmethod
    async def _audit(connection, memory, action):
        payload = json.loads(json.dumps(asdict(memory), default=str))
        await connection.execute("INSERT INTO memory_revisions (memory_id, revision, action, payload) VALUES (%s, %s, %s, %s)",
                                 (memory.id, memory.revision, action, Jsonb(payload)))

    async def get(self, identity: UUID, workspace_id: UUID, *, connection=None) -> Memory:
        if connection is None:
            async with self.database.connection() as conn:
                return await self.get(identity, workspace_id, connection=conn)
        cursor = await connection.execute("SELECT * FROM memories WHERE id = %s AND (scope = 'user' OR workspace_id = %s)", (identity, workspace_id))
        row = await cursor.fetchone()
        if row is None:
            raise LookupError("Memory does not exist in this workspace")
        return Memory(**row)

    async def list(self, workspace_id, *, status="active", limit=50, before=None):
        if status not in {"active", "candidate", "deleted", "rejected", "quarantined", "all"} or not 1 <= limit <= 100:
            raise ValueError("Invalid memory list bounds")
        if before is not None:
            await self.get(before, workspace_id)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM memories WHERE (scope = 'user' OR workspace_id = %s) "
                "AND (%s = 'all' OR status = %s) AND (%s::uuid IS NULL OR (created_at, id) < "
                "(SELECT created_at, id FROM memories WHERE id = %s)) ORDER BY created_at DESC, id DESC LIMIT %s",
                (workspace_id, status, status, before, before, limit + 1),
            )
            rows = await cursor.fetchall()
        return {"items": [Memory(**row) for row in rows[:limit]], "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}

    async def current(self, scope, workspace_id, key):
        async with self.database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM memories WHERE scope = %s AND workspace_id IS NOT DISTINCT FROM %s AND key = %s AND status = 'active'",
                                              (scope, workspace_id if scope == "workspace" else None, key))
            row = await cursor.fetchone()
            return Memory(**row) if row else None

    async def propose(self, proposal: MemoryProposal, workspace_id, *, connection=None) -> Memory | None:
        if connection is None:
            async with self.database.transaction() as conn:
                return await self.propose(proposal, workspace_id, connection=conn)
        content = validate_memory(proposal.key, proposal.content, proposal.scope, proposal.pinned)
        workspace = workspace_id if proposal.scope == "workspace" else None
        await self._lock(connection, proposal.scope, workspace, proposal.key)
        if proposal.origin not in {"manual", "explicit_user", "inferred"}:
            raise ValueError("Invalid memory origin")
        if proposal.source_run_id is not None:
            cursor = await connection.execute(
                "SELECT m.id, m.payload FROM messages m JOIN runs r ON r.id = m.run_id "
                "WHERE r.id = %s AND r.session_id = %s AND r.status = 'completed' AND m.role = 'user' AND m.id = ANY(%s)",
                (proposal.source_run_id, proposal.source_session_id, [UUID(value) for value in proposal.source_message_ids]),
            )
            sources = await cursor.fetchall()
            if not sources or {str(row["id"]) for row in sources} != set(proposal.source_message_ids):
                raise ValueError("Memory sources must belong to a completed run")
            if not proposal.evidence or not any(isinstance(row["payload"]["content"], str) and proposal.evidence in row["payload"]["content"] for row in sources):
                raise ValueError("Memory evidence must quote its source input")
            cursor = await connection.execute("SELECT workspace_id FROM sessions WHERE id = %s", (proposal.source_session_id,))
            if (await cursor.fetchone())["workspace_id"] != workspace_id:
                raise ValueError("Memory source workspace does not match")
        elif proposal.origin != "manual" or proposal.source_message_ids or proposal.source_session_id is not None:
            raise ValueError("Generated memories require completed sources")
        digest = content_hash(content)
        cursor = await connection.execute(
            "SELECT * FROM memories WHERE scope = %s AND workspace_id IS NOT DISTINCT FROM %s AND key = %s "
            "AND content_hash = %s ORDER BY created_at DESC LIMIT 1", (proposal.scope, workspace, proposal.key, digest),
        )
        same = await cursor.fetchone()
        cursor = await connection.execute(
            "SELECT * FROM memories WHERE scope = %s AND workspace_id IS NOT DISTINCT FROM %s AND key = %s AND status = 'active'",
            (proposal.scope, workspace, proposal.key),
        )
        active = await cursor.fetchone()
        if same and same["status"] == "candidate" and proposal.origin != "inferred" and active is None:
            cursor = await connection.execute(
                "UPDATE memories SET status = 'active', revision = revision + 1, pinned = %s, origin = %s, "
                "source_session_id = %s, source_run_id = %s, source_message_ids = %s, evidence = %s, updated_at = now() WHERE id = %s RETURNING *",
                (proposal.pinned, proposal.origin, proposal.source_session_id, proposal.source_run_id,
                 Jsonb(proposal.source_message_ids), proposal.evidence, same["id"]),
            )
            promoted = Memory(**(await cursor.fetchone()))
            await self._audit(connection, promoted, "explicit_accept")
            return promoted
        if same and (proposal.origin == "inferred" or same["status"] in {"active", "candidate"}):
            return Memory(**same) if same["status"] in {"active", "candidate"} else None
        if active and proposal.origin == "manual":
            raise StateConflictError("Memory key already exists; edit its current revision")
        status = "candidate" if proposal.origin == "inferred" or active else "active"
        cursor = await connection.execute(
            "INSERT INTO memories (id, scope, workspace_id, key, content, content_hash, status, pinned, origin, "
            "source_session_id, source_run_id, source_message_ids, evidence, conflict_with) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (uuid4(), proposal.scope, workspace, proposal.key, content, digest, status, proposal.pinned,
             proposal.origin, proposal.source_session_id, proposal.source_run_id, Jsonb(proposal.source_message_ids),
             proposal.evidence, active["id"] if active else None),
        )
        memory = Memory(**(await cursor.fetchone()))
        await self._audit(connection, memory, "propose" if status == "candidate" else "create")
        return memory

    async def change(self, identity, workspace_id, *, expected_revision, content=None, pinned=None,
                     action="edit", replace_revision=None, replace_id=None):
        if action not in {"edit", "accept", "reject", "delete"} or not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValueError("Invalid memory operation")
        if replace_revision is not None and (not isinstance(replace_revision, int) or isinstance(replace_revision, bool)):
            raise ValueError("Invalid replacement revision")
        async with self.database.transaction() as connection:
            memory = await self.get(identity, workspace_id, connection=connection)
            await self._lock(connection, memory.scope, memory.workspace_id, memory.key)
            memory = await self.get(identity, workspace_id, connection=connection)
            if memory.revision != expected_revision:
                raise StateConflictError("Memory changed; reload before saving")
            if memory.status not in {"active", "candidate"}:
                raise StateConflictError("Memory is no longer editable")
            if action in {"accept", "reject"} and memory.status != "candidate":
                raise ValueError("Only candidates can be accepted or rejected")
            if content is not None:
                memory.content = validate_memory(memory.key, content, memory.scope, memory.pinned)
            if pinned is not None:
                if not isinstance(pinned, bool):
                    raise ValueError("Pinned must be boolean")
                memory.pinned = pinned
            status = {"accept": "active", "reject": "rejected", "delete": "deleted"}.get(action, memory.status)
            if action == "accept":
                cursor = await connection.execute(
                    "SELECT * FROM memories WHERE scope = %s AND workspace_id IS NOT DISTINCT FROM %s AND key = %s AND status = 'active'",
                    (memory.scope, memory.workspace_id, memory.key),
                )
                existing = await cursor.fetchone()
                if existing:
                    if replace_revision != existing["revision"] or replace_id != existing["id"]:
                        raise StateConflictError("Conflicting memory changed; review the current version")
                    await connection.execute("UPDATE memories SET status = 'rejected', revision = revision + 1, updated_at = now() WHERE id = %s", (existing["id"],))
                    replaced = await self.get(existing["id"], workspace_id, connection=connection)
                    await self._audit(connection, replaced, "replaced")
            cursor = await connection.execute(
                "UPDATE memories SET content = %s, content_hash = %s, pinned = %s, status = %s, revision = revision + 1, updated_at = now() "
                "WHERE id = %s RETURNING *", (memory.content, content_hash(memory.content), memory.pinned, status, identity),
            )
            changed = Memory(**(await cursor.fetchone()))
            await self._audit(connection, changed, action)
            return changed

    async def revisions(self, identity, workspace_id):
        async with self.database.connection() as connection:
            await self.get(identity, workspace_id, connection=connection)
            cursor = await connection.execute("SELECT revision, action, payload, created_at FROM memory_revisions WHERE memory_id = %s ORDER BY revision DESC LIMIT 100", (identity,))
            return await cursor.fetchall()

    async def recall(self, workspace_id, terms, limit=8, *, workspace_slots=None):
        workspace_slots = max(1, limit - 2) if workspace_slots is None else workspace_slots
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "WITH ranked AS (SELECT m.id, m.scope, m.pinned, m.updated_at, score.hits, "
                "row_number() OVER (PARTITION BY m.scope ORDER BY score.hits DESC, m.pinned DESC, m.updated_at DESC, m.id DESC) AS scope_rank "
                "FROM memories m "
                "CROSS JOIN LATERAL (SELECT count(*) AS hits FROM unnest(%s::text[]) term "
                "WHERE strpos(lower(m.key || ' ' || m.content), term) > 0) score "
                "WHERE m.status = 'active' AND (m.scope = 'user' OR m.workspace_id = %s) "
                "AND NOT (m.scope = 'user' AND EXISTS (SELECT 1 FROM memories w WHERE w.workspace_id = %s AND w.status = 'active' AND w.key = m.key)) "
                "AND (m.pinned OR m.scope = 'user' OR score.hits > 0 "
                "OR EXISTS (SELECT 1 FROM memories u WHERE u.scope = 'user' AND u.status = 'active' AND u.key = m.key))) "
                "SELECT m.* FROM ranked r JOIN memories m ON m.id = r.id "
                "ORDER BY CASE WHEN r.scope = 'workspace' AND r.hits > 0 AND r.scope_rank <= %s THEN 0 "
                "WHEN r.scope = 'user' AND r.hits > 0 THEN 1 "
                "WHEN r.scope = 'workspace' AND r.hits > 0 THEN 2 "
                "WHEN r.scope = 'workspace' THEN 3 "
                "WHEN r.pinned THEN 4 ELSE 5 END, r.hits DESC, r.pinned DESC, "
                "r.updated_at DESC, r.id DESC LIMIT %s", (terms, workspace_id, workspace_id, workspace_slots, limit),
            )
            return [Memory(**row) for row in await cursor.fetchall()]

    async def usage(self, identity: UUID, workspace_id: UUID) -> dict:
        """Count runs that actually received this memory, using durable snapshots."""
        await self.get(identity, workspace_id)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS injection_count, max(created_at) AS last_injected_at "
                "FROM model_calls WHERE iteration = 0 "
                "AND (context_snapshot->'memory_references') @> "
                "jsonb_build_array(jsonb_build_object('id', %s::text))", (str(identity),),
            )
            result = dict(await cursor.fetchone())
            cursor = await connection.execute(
                "SELECT count(*) AS correction_count FROM memory_revisions "
                "WHERE memory_id = %s AND action IN ('edit', 'replaced', 'delete')", (identity,),
            )
            result["correction_count"] = (await cursor.fetchone())["correction_count"]
            return result
