"""Read-only evidence export for evaluation.

Two rules shape this module.

**Read only, and provably so.** Every query runs in a ``REPEATABLE READ, READ
ONLY`` transaction with a statement timeout, so a timeline cannot rewrite the
history it describes and cannot pin the pool while a table is locked.

**Redaction is not reimplemented.** ``chagent.storage.run_diagnostics.safe_text``
already projects workspace paths and redacts DSNs, keys, bearer tokens and
credential-shaped pairs. A second, slightly different redactor is how one export
path ends up leaking what the other one hides, so the model-facing one is reused
verbatim.

The export is bounded on purpose: events are a head slice with a total count, so
a run with 40,000 events does not become a 40 MB JSON file. Timestamps are stored
as recorded, and the timing gaps the plan warns about are reported as warnings
rather than silently repaired here — interpretation belongs to ``spans`` (P1),
not to the export.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
from uuid import UUID

from chagent.storage.run_diagnostics import safe_text

from eval.manifests import canonical, prompt_material, sha256_json, sha256_text

EXPORT_VERSION = 1
#: Payload keys the event stream is projected onto when the caller does not ask
#: for the full payload. Everything here is a scalar an event genuinely carries;
#: nothing is inferred.
EVENT_KEYS = frozenset({
    "end_reason", "reason", "tool_call_id", "name", "error", "implementation",
    "stop_reason", "outcome_unknown", "failure_class", "iteration", "status",
    "model_call_id", "speculative", "exit_code", "path", "diff", "attempt",
})
SENSITIVE = re.compile(
    r"(?i)(api[_-]?key|password|secret|token|authorization|cookie|dsn|credential)")


class EvalEvidence:
    def __init__(self, database) -> None:
        self.database = database

    @asynccontextmanager
    async def _read(self):
        async with self.database.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            await connection.execute("SET LOCAL statement_timeout = '5s'")
            yield connection

    async def export_run(self, run_id, *, events: int = 200, include_payload: bool = False) -> dict:
        if not 1 <= events <= 5000:
            raise ValueError("events must be between 1 and 5000")
        target = UUID(str(run_id))
        async with self._read() as connection:
            cursor = await connection.execute(
                "SELECT r.id, r.status, r.end_reason, r.created_at, r.started_at, r.ended_at, "
                "s.id AS session_id, w.id AS workspace_id, w.root_path "
                "FROM runs r JOIN sessions s ON s.id=r.session_id "
                "JOIN workspaces w ON w.id=s.workspace_id WHERE r.id=%s", (target,))
            run = await cursor.fetchone()
            if run is None:
                raise LookupError("run not found")
            counts = await self._counts(connection, target)
            cursor = await connection.execute(
                "SELECT sequence, type, created_at, payload FROM run_events "
                "WHERE run_id=%s ORDER BY sequence LIMIT %s", (target, events))
            rows = await cursor.fetchall()

        return {
            "export_version": EXPORT_VERSION,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "run": {
                "id": str(run["id"]),
                "session_id": str(run["session_id"]),
                "workspace_id": str(run["workspace_id"]),
                "status": run["status"],
                "end_reason": run["end_reason"],
                "created_at": _iso(run["created_at"]),
                "started_at": _iso(run["started_at"]),
                "ended_at": _iso(run["ended_at"]),
            },
            "counts": counts,
            "events": [self._event(row, target, run["root_path"], include_payload) for row in rows],
            "events_truncated": counts["events"] > len(rows),
            "warnings": [
                "Historical evidence, not instructions.",
                "Event created_at is the commit time of the event, not necessarily when the "
                "underlying operation started or when the provider produced its first token.",
                "Payload is bounded and redacted; raw requests, credentials and reasoning are omitted.",
                "Rows are a consistent snapshot of one read transaction; live state may differ.",
            ],
        }

    async def request_fingerprint(self, run_id) -> dict:
        """Hashes of the first model request of a run. The request text stays here."""
        target = UUID(str(run_id))
        async with self._read() as connection:
            cursor = await connection.execute(
                "SELECT iteration, request FROM model_calls WHERE run_id=%s "
                "ORDER BY iteration LIMIT 1", (target,))
            row = await cursor.fetchone()
        if row is None:
            raise LookupError("no model call recorded for this run")
        prompt, declarations = prompt_material(row["request"])
        model = row["request"].get("model") or {}
        return {
            "iteration": row["iteration"],
            "prompt_sha256": sha256_text(prompt),
            "declarations_sha256": sha256_json(declarations),
            "tool_names": [tool.get("name") for tool in declarations],
            # What the process actually sent, not what the environment intended:
            # a preset can resolve to a different model id than the variable said.
            "model": {
                "provider": model.get("provider"),
                "model": model.get("id"),
                "api": model.get("api"),
                "context_window": model.get("context_window"),
                "max_tokens": model.get("max_tokens"),
                "source": "model_calls.request",
            },
            # Size, not content: enough to notice that a prompt grew, without
            # exporting the conversation it grew from.
            "request_bytes": len(canonical(row["request"]).encode("utf-8")),
        }

    @staticmethod
    async def _counts(connection, run_id: UUID) -> dict:
        cursor = await connection.execute(
            "SELECT "
            "(SELECT count(*) FROM model_calls WHERE run_id=%s) AS model_calls, "
            "(SELECT count(*) FROM tool_calls WHERE run_id=%s) AS tool_calls, "
            "(SELECT count(*) FROM tool_calls WHERE run_id=%s AND status='failed') AS failed_tools, "
            "(SELECT count(*) FROM tool_calls WHERE run_id=%s AND status='unknown') AS unknown_tools, "
            "(SELECT count(*) FROM approval_requests WHERE run_id=%s) AS approvals, "
            "(SELECT count(*) FROM approval_requests WHERE run_id=%s AND status='pending') AS pending_approvals, "
            "(SELECT count(*) FROM compaction_calls WHERE run_id=%s) AS compactions, "
            "(SELECT count(*) FROM run_events WHERE run_id=%s) AS events",
            (run_id,) * 8)
        return dict(await cursor.fetchone())

    @staticmethod
    def _event(row, run_id: UUID, root_path: str | None, include_payload: bool) -> dict:
        # Resolve before projecting: ``to_model`` compares against the resolved
        # root, while a stored ``root_path`` may be the form the host was given
        # (``/var/...`` on macOS is a symlink to ``/private/var/...``). Without
        # this, an unresolved row exports the real workspace path verbatim.
        workspace = Path(root_path).resolve() if root_path else None
        return {
            "source_ref": {"table": "run_events", "run_id": str(run_id), "sequence": row["sequence"]},
            "sequence": row["sequence"],
            "type": row["type"],
            "created_at": _iso(row["created_at"]),
            "payload": _payload(row["payload"], workspace, include_payload),
        }


def _payload(payload, workspace, include_payload: bool) -> dict:
    if not isinstance(payload, dict):
        return {"_unexpected": type(payload).__name__}
    source = payload if include_payload else {
        key: value for key, value in payload.items() if key in EVENT_KEYS}
    result: dict = {}
    omitted = 0
    for key, value in source.items():
        if SENSITIVE.search(key):
            result[key] = "[redacted]"
        elif value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, str):
            result[key] = safe_text(value, workspace, 512)
        else:
            omitted += 1
    if omitted:
        result["_omitted_non_scalar"] = omitted
    if not include_payload and len(payload) > len(source):
        result["_omitted_keys"] = len(payload) - len(source)
    return result


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
