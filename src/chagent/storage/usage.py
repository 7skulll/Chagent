"""Token spend exactly as the provider reported it, grouped for reading.

Every model call keeps the provider's ``usage`` block verbatim in
``model_calls.response``. A call that never produced one is still a call and
contributes nothing to the token sums: absent is not zero, and guessing it would
turn an audit fact into a number nobody can defend. Money is deliberately not
computed here -- ``UsageCost`` is never populated, so any amount would be an
estimate dressed up as a measurement.
"""

from uuid import UUID

from chagent.storage.database import Database


TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "total_tokens")

#: Sums are read out of the stored JSON, so a missing or non-numeric field must
#: not fail the whole page. ``NULLIF`` keeps the cast honest and the COALESCE
#: turns the aggregate's NULL into the only defensible answer: nothing counted.
_SUM = ", ".join(
    f"COALESCE(SUM(NULLIF(m.response->'usage'->>'{field}', '')::bigint), 0) AS {field}"
    for field in TOKEN_FIELDS
)


class UsageRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def totals(rows: list[dict]) -> dict:
        """Add the grouped rows up; the caller never sums in the browser."""
        summed = {field: sum(row[field] for row in rows) for field in TOKEN_FIELDS}
        denominator = summed["input"] + summed["cache_read"]
        return {
            "runs": sum(row.get("runs", 1) for row in rows),
            "model_calls": sum(row["model_calls"] for row in rows),
            **summed,
            "cache_hit_rate": (summed["cache_read"] / denominator) if denominator else None,
        }

    async def session(self, session_id: UUID, *, limit: int = 50) -> dict:
        """Per-run rows for one session, newest first, plus the session total."""
        if not 1 <= limit <= 200:
            raise ValueError("Usage page limit must be between 1 and 200")
        grouped = (
            "SELECT r.id AS run_id, r.started_at, count(m.id) AS model_calls, " + _SUM +
            " FROM runs r LEFT JOIN model_calls m ON m.run_id = r.id WHERE r.session_id = %s "
            "GROUP BY r.id, r.started_at"
        )
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                grouped + " ORDER BY r.started_at DESC NULLS LAST, r.id DESC LIMIT %s",
                (session_id, limit))
            items = [{"run_id": row["run_id"], "started_at": row["started_at"], **self._row(row)}
                     for row in await cursor.fetchall()]
            cursor = await connection.execute(
                "SELECT count(m.id) AS model_calls, " + _SUM +
                " FROM runs r LEFT JOIN model_calls m ON m.run_id = r.id WHERE r.session_id = %s",
                (session_id,))
            overall = await cursor.fetchone()
        return {
            "items": items,
            "totals": self._totals_of(overall, runs=await self._run_count(session_id)),
        }

    async def daily(self, *, days: int = 7, roots: list[str] | None = None) -> dict:
        """One row per calendar day over the last ``days`` days, newest first."""
        if not 1 <= days <= 90:
            raise ValueError("Usage window must be between 1 and 90 days")
        clause = " AND w.root_path = ANY(%s)" if roots is not None else ""
        params = (days - 1,) if roots is None else (days - 1, roots)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT date_trunc('day', r.started_at)::date AS day, count(DISTINCT r.id) AS runs, "
                "count(m.id) AS model_calls, " + _SUM +
                " FROM runs r JOIN sessions s ON s.id = r.session_id "
                " JOIN workspaces w ON w.id = s.workspace_id "
                " LEFT JOIN model_calls m ON m.run_id = r.id "
                " WHERE r.started_at >= date_trunc('day', now()) - make_interval(days => %s)" + clause +
                " GROUP BY 1 ORDER BY 1 DESC", params)
            items = [{"day": row["day"], **self._row(row)} for row in await cursor.fetchall()]
        return {"days": days, "items": items, "totals": self.totals(items)}

    async def _run_count(self, session_id: UUID) -> int:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS n FROM runs WHERE session_id = %s", (session_id,))
            return (await cursor.fetchone())["n"]

    @staticmethod
    def _row(row) -> dict:
        return {"model_calls": row["model_calls"],
                **{field: int(row[field]) for field in TOKEN_FIELDS}}

    @classmethod
    def _totals_of(cls, row, *, runs: int) -> dict:
        grouped = [cls._row(row)]
        totals = cls.totals(grouped)
        totals["runs"] = runs
        return totals
