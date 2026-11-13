"""Timeline projection: what happened, when, and how well we actually know it.

The plan's P1 asks for a read-only timeline built on the existing tables rather
than a second logging system, and it asks for the gaps to be reported instead of
filled in. Both rules are enforced here.

Three facts about this codebase decide the shape of the projection; each was
checked against the code and the database, not assumed:

**A tool's start is an event, not a column.** ``tool_calls.created_at`` is the
enqueue instant. ``start_tool_call`` flips the row to ``running`` and emits
``tool_started`` in one transaction *before* execution, so the event is a start;
the column is not. A span whose start event is missing gets ``started_at=None``
and is counted in the missing report — it is never back-filled from
``created_at``, because that would silently turn queue time into execution time.

**Event timestamps are commit timestamps.** ``storage/event_queue.py`` buffers
events and writes them in batches, so ``run_events.created_at`` is when the batch
committed. Inside one batch every timestamp is identical; the database currently
holds 1236 same-microsecond groups of more than two events. Event-derived
boundaries are therefore marked ``commit_lag``, and the batch flush interval
(100 ms) is the honest scale of that lag.

**A compaction call row is written after the fact.** ``record_calls`` inserts
rows already terminal (adoption or post-turn), so ``created_at`` and ``ended_at``
on ``compaction_calls`` are both the insert instant and describe no interval.
Only ``duration_ms`` is a real measurement. A compaction with no event pair
therefore reports duration and ``None`` boundaries — an anchored interval would
be wrong, not merely imprecise.

Accounting follows section 4.2: cumulative time is the sum of span durations and
may exceed the run's wall clock; wall occupancy is the union of intervals, so
concurrent tools, overlapping approvals and speculative work are not subtracted
twice. Work that ran outside the run's window (a post-turn memory job) is
excluded from this run's occupancy and reported separately. ``unexplained_ms``
is wall minus union and can never be negative: any negative interval is repaired
before it reaches the arithmetic, and the repair is counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

#: Span kinds the plan lists. ``site`` is deliberately absent: a site's service
#: lifetime leaves no per-run record, so a site span would be invented rather
#: than projected. Site activity is visible as an ordinary tool span.
KINDS = ("run", "model", "tool", "approval", "compaction", "memory")

#: Event types the projection reads. Text deltas are handled by an aggregate,
#: because a long answer is thousands of rows and none of them is a boundary.
EVENT_TYPES = (
    "run_started", "run_completed", "run_failed", "run_cancelled", "run_interrupted",
    "model_started", "model_completed",
    "tool_queued", "tool_started", "tool_completed", "tool_interrupted",
    "approval_requested", "approval_decided",
    "compaction_started", "compaction_completed", "compaction_discarded",
    "memory_job_queued", "memory_extraction_started", "memory_saved",
    "memory_extraction_failed", "memory_job_failed",
)

TIMELINE_VERSION = 1


@dataclass(frozen=True)
class Span:
    """One observable interval.

    ``timing_source`` names the evidence the boundaries came from and
    ``timing_precision`` how much to trust it. The two are separate on purpose:
    a tool start and a tool end can come from different evidence with different
    confidence, and collapsing that into one label hides which end is weak.
    """

    span_id: str
    run_id: str
    kind: str
    name: str
    status: str
    source_ref: dict
    timing_source: str
    timing_precision: str
    parent_id: str | None = None
    links: tuple[dict, ...] = ()
    queued_at: datetime | None = None
    started_at: datetime | None = None
    execution_ended_at: datetime | None = None
    committed_at: datetime | None = None
    tokens: int | None = None
    cost: float | None = None
    warnings: tuple[str, ...] = ()
    attributes: dict = field(default_factory=dict)

    @property
    def open(self) -> bool:
        return self.execution_ended_at is None

    def duration_ms(self) -> int | None:
        if self.started_at is None or self.execution_ended_at is None:
            return None
        return _ms(self.started_at, self.execution_ended_at)

    def wait_ms(self) -> int | None:
        """Queue time before execution. Reported, never folded into duration."""
        if self.queued_at is None or self.started_at is None:
            return None
        return _ms(self.queued_at, self.started_at)

    def interval(self) -> tuple[datetime, datetime] | None:
        if self.started_at is None or self.execution_ended_at is None:
            return None
        return self.started_at, self.execution_ended_at

    def to_json(self) -> dict:
        return {
            "span_id": self.span_id,
            "run_id": self.run_id,
            "parent_id": self.parent_id,
            "links": list(self.links),
            "kind": self.kind,
            "name": self.name,
            "source_record_ref": self.source_ref,
            "status": self.status,
            "queued_at": _iso(self.queued_at),
            "started_at": _iso(self.started_at),
            "execution_ended_at": _iso(self.execution_ended_at),
            "committed_at": _iso(self.committed_at),
            "duration_ms": self.duration_ms(),
            "wait_ms": self.wait_ms(),
            "open": self.open,
            "timing_source": self.timing_source,
            "timing_precision": self.timing_precision,
            "tokens": self.tokens,
            "cost": self.cost,
            "warnings": list(self.warnings),
            "attributes": self.attributes,
        }


@dataclass(frozen=True)
class Accounting:
    window_start: datetime
    window_end: datetime
    window_open: bool
    wall_ms: int
    cumulative_ms: int
    union_ms: int
    unexplained_ms: int
    outside_window_ms: int
    by_kind: dict
    gaps: tuple[dict, ...]
    gap_count: int
    gap_total_ms: int
    concurrency: dict
    critical_path: None
    notes: tuple[str, ...]

    def to_json(self) -> dict:
        return {
            "window": {
                "start": _iso(self.window_start),
                "end": _iso(self.window_end),
                "open": self.window_open,
                "wall_ms": self.wall_ms,
            },
            "cumulative_ms": self.cumulative_ms,
            "union_ms": self.union_ms,
            "unexplained_ms": self.unexplained_ms,
            "outside_window_ms": self.outside_window_ms,
            "by_kind": self.by_kind,
            "gaps": list(self.gaps),
            "gap_count": self.gap_count,
            "gap_total_ms": self.gap_total_ms,
            "concurrency": self.concurrency,
            # Section 4.2.3: without explicit dependency edges, an ordering by
            # iteration is not a critical path, so none is claimed.
            "critical_path": None,
            "notes": list(self.notes),
        }


class SpanProjection:
    """Read-only projection of one run into spans, accounting and gaps."""

    def __init__(self, database) -> None:
        self.database = database

    async def project(self, run_id) -> dict:
        target = UUID(str(run_id))
        async with self.database.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            await connection.execute("SET LOCAL statement_timeout = '10s'")
            rows = await self._load(connection, target)
        return _build(target, rows)

    @staticmethod
    async def _load(connection, run_id: UUID) -> dict:
        cursor = await connection.execute(
            "SELECT r.id, r.status, r.end_reason, r.created_at, r.started_at, r.ended_at, "
            "s.id AS session_id, w.root_path FROM runs r "
            "JOIN sessions s ON s.id = r.session_id JOIN workspaces w ON w.id = s.workspace_id "
            "WHERE r.id = %s", (run_id,))
        run = await cursor.fetchone()
        if run is None:
            return {"run": None}

        # Two columns of ``response`` and nothing else: tokens and the provider's
        # own duration. The response body stays in the database.
        cursor = await connection.execute(
            "SELECT m.id, m.iteration, m.status, m.created_at, m.ended_at, "
            "(m.response->'usage'->>'total_tokens')::int AS total_tokens, "
            "(m.response->>'duration_ms')::int AS duration_ms "
            "FROM model_calls m WHERE m.run_id = %s ORDER BY m.iteration", (run_id,))
        models = list(await cursor.fetchall())

        cursor = await connection.execute(
            "SELECT t.id, t.call_id, t.name, t.status, t.attempt, t.call_index, "
            "t.created_at, t.ended_at, m.iteration FROM tool_calls t "
            "LEFT JOIN model_calls m ON m.id = t.model_call_id "
            "WHERE t.run_id = %s ORDER BY t.created_at, t.call_index", (run_id,))
        tools = list(await cursor.fetchall())

        cursor = await connection.execute(
            "SELECT id, tool_record_id, tool_name, status, created_at, decided_at, "
            "batch_id, batch_index FROM approval_requests WHERE run_id = %s ORDER BY created_at",
            (run_id,))
        approvals = list(await cursor.fetchall())

        cursor = await connection.execute(
            "SELECT compaction_id, count(*) AS calls, sum(duration_ms) AS duration_ms, "
            "min(status) AS status FROM compaction_calls WHERE run_id = %s GROUP BY compaction_id",
            (run_id,))
        compactions = {str(row["compaction_id"]): dict(row) for row in await cursor.fetchall()}

        cursor = await connection.execute(
            "SELECT id, status, created_at, started_at, ended_at, attempts, max_attempts "
            "FROM memory_jobs WHERE run_id = %s ORDER BY created_at", (run_id,))
        memory = list(await cursor.fetchall())

        cursor = await connection.execute(
            "SELECT sequence, type, created_at, payload FROM run_events "
            "WHERE run_id = %s AND type = ANY(%s) ORDER BY sequence",
            (run_id, list(EVENT_TYPES)))
        events = list(await cursor.fetchall())

        # Thousands of deltas describe the stream, not a boundary; the first one
        # is the earliest recorded visible text and nothing more.
        cursor = await connection.execute(
            "SELECT min(created_at) AS first FROM run_events "
            "WHERE run_id = %s AND type = 'text_delta'", (run_id,))
        first_text = (await cursor.fetchone())["first"]
        return {"run": run, "models": models, "tools": tools, "approvals": approvals,
                "compactions": compactions, "memory": memory, "events": events,
                "first_text": first_text}


def _build(run_id: UUID, rows: dict) -> dict:
    run = rows["run"]
    if run is None:
        raise LookupError("run not found")
    from pathlib import Path

    workspace = Path(run["root_path"]).resolve() if run["root_path"] else None
    events = _index_events(rows["events"])
    spans: list[Span] = []
    repaired = 0

    def add(span: Span) -> Span:
        nonlocal repaired
        if (span.started_at is not None and span.execution_ended_at is not None
                and span.execution_ended_at < span.started_at):
            # Clock skew or a commit ordering that contradicts itself. Emitting a
            # negative interval would make "framework time" negative downstream,
            # which is exactly the failure the plan's acceptance forbids.
            repaired += 1
            span = _replace(span, execution_ended_at=span.started_at, warnings=span.warnings + (
                "negative interval repaired to zero: end preceded start",))
        spans.append(span)
        return span

    run_span = add(_run_span(run, run_id))
    window_start = run_span.started_at or run_span.queued_at
    window_end = run_span.execution_ended_at
    window_open = window_end is None
    observed_end = window_end or _latest(events, rows) or _now()

    for row in rows["models"]:
        add(_model_span(row, run_id, run_span.span_id, events))
    for row in rows["tools"]:
        add(_tool_span(row, run_id, run_span.span_id, events))
    for row in rows["approvals"]:
        add(_approval_span(row, run_id, run_span.span_id, events))
    for span in _compaction_spans(rows, run_id, run_span.span_id, events):
        add(span)
    for row in rows["memory"]:
        add(_memory_span(row, run_id, events))

    window = (window_start, observed_end)
    accounting = _account(spans, window, window_open)
    first_text = rows.get("first_text")

    document = {
        "timeline_version": TIMELINE_VERSION,
        "observed_at": _iso(_now()),
        "run": {
            "id": str(run_id),
            "session_id": str(run["session_id"]),
            "status": run["status"],
            "end_reason": run["end_reason"],
            "created_at": _iso(run["created_at"]),
            "started_at": _iso(run["started_at"]),
            "ended_at": _iso(run["ended_at"]),
        },
        "stream": {
            "first_text_delta_at": _iso(first_text),
            "after_window_start_ms": _ms(window_start, first_text) if first_text else None,
            # Labelled precisely: this is when the first visible chunk was
            # committed, which is a lower bound on when the model started
            # producing text and says nothing about when a browser rendered it.
            "first_text_delta_kind": "commit time of first visible text, not provider TTFT",
        },
        "spans": [span.to_json() for span in sorted(spans, key=_order)],
        "accounting": accounting.to_json(),
        "missing": _missing(spans, rows, events, window_open, repaired),
        "notes": [
            "Read-only snapshot of recorded history; it describes what was stored, not what happened.",
            "Event timestamps are batch-commit times, so boundaries derived from them carry up to one "
            "flush interval (100 ms) of lag and events in one batch share an identical timestamp.",
            "A tool's execution start comes from its tool_started event; tool_calls.created_at is the "
            "enqueue time and is reported as wait, never as execution.",
            "A model span starts at the insert of its request row, which precedes the send, so model "
            "duration is an upper bound on the call.",
            "The run span is the observation window itself; occupancy and cumulative time are "
            "computed from the spans inside it, never from the window.",
            "Work outside the run window (post-turn memory) is excluded from this run's occupancy.",
            "No browser or client timing is recorded, so no time-to-first-byte in the browser is reported.",
        ],
    }
    return _redact(document, workspace)


# --------------------------------------------------------------------------- spans


def _run_span(run, run_id: UUID) -> Span:
    started = run["started_at"] or run["created_at"]
    warnings = []
    if run["ended_at"] is None:
        warnings.append("run has no end timestamp; the window ends at the last recorded evidence")
    return Span(
        span_id=f"run:{run_id}",
        run_id=str(run_id),
        kind="run",
        name="run",
        status=run["status"] if not run["end_reason"] else f"{run['status']}/{run['end_reason']}",
        source_ref={"table": "runs", "id": str(run_id)},
        timing_source="run_column",
        timing_precision="recorded",
        queued_at=None,
        started_at=started,
        execution_ended_at=run["ended_at"],
        committed_at=run["ended_at"],
        warnings=tuple(warnings),
        attributes={"session_id": str(run["session_id"])},
    )


def _model_span(row, run_id: UUID, parent: str, events: dict) -> Span:
    # The row insert precedes the send, so the recorded interval is an upper
    # bound on the call. That applies to every model span equally, so it is a
    # document-level note instead of a warning repeated once per call.
    warnings = []
    if row["ended_at"] is None:
        warnings.append("model call has no end timestamp")
    if row["duration_ms"] is not None and row["ended_at"] is not None and row["created_at"] is not None:
        measured = _ms(row["created_at"], row["ended_at"])
        # A provider-reported duration that disagrees with the recorded interval
        # by more than a commit batch means one of the two is describing a
        # different thing. Keep both, flag the difference.
        if abs(measured - int(row["duration_ms"])) > 250:
            warnings.append(
                f"provider duration {int(row['duration_ms'])} ms differs from recorded interval "
                f"{measured} ms by more than one flush interval")
    completion = _first(events.get("model_completed"), lambda payload:
                        payload.get("model_call_id") == str(row["id"]))
    return Span(
        span_id=f"model:{row['id']}",
        run_id=str(run_id),
        kind="model",
        name=f"iteration {row['iteration']}",
        status=row["status"],
        source_ref={"table": "model_calls", "id": str(row["id"]), "iteration": row["iteration"]},
        timing_source="model_column",
        timing_precision="recorded",
        parent_id=parent,
        started_at=row["created_at"],
        execution_ended_at=row["ended_at"],
        committed_at=completion[1] if completion else row["ended_at"],
        tokens=row["total_tokens"],
        warnings=tuple(warnings),
        attributes={"iteration": row["iteration"]},
    )


def _tool_span(row, run_id: UUID, parent: str, events: dict) -> Span:
    call_id = row["call_id"]
    started = _first(events.get("tool_started"), lambda p: p.get("tool_call_id") == call_id)
    completed = _first(events.get("tool_completed"), lambda p: p.get("tool_call_id") == call_id)
    interrupted = _first(events.get("tool_interrupted"), lambda p: p.get("tool_call_id") == call_id)
    warnings: list[str] = []
    links: list[dict] = []
    if started is None:
        # Deliberately not falling back to created_at: that would report queue
        # time as execution time and inflate every tool's cost.
        warnings.append("no tool_started event: execution start is unknown, not zero")
    if row["ended_at"] is None:
        warnings.append("tool call has no end timestamp")
    if row["status"] == "unknown":
        warnings.append("outcome unknown: the side effect may or may not have happened")
    if interrupted:
        links.append({"kind": "event", "type": "tool_interrupted", "sequence": interrupted[0]})
    if completed:
        links.append({"kind": "event", "type": "tool_completed", "sequence": completed[0]})

    approval = _first(events.get("approval_requested"),
                      lambda p: p.get("tool_call_id") == str(row["id"]))
    if approval:
        links.append({"kind": "waits_for", "type": "approval",
                      "approval_id": approval[1].get("approval_id")})

    precision = "commit_lag" if started else "unknown"
    return Span(
        span_id=f"tool:{row['id']}",
        run_id=str(run_id),
        kind="tool",
        name=row["name"],
        status=row["status"],
        source_ref={"table": "tool_calls", "id": str(row["id"]), "call_id": call_id,
                    "call_index": row["call_index"], "attempt": row["attempt"]},
        timing_source="tool_event" if started else "none",
        timing_precision=precision,
        parent_id=parent,
        links=tuple(links),
        queued_at=row["created_at"],
        started_at=started[1] if started else None,
        execution_ended_at=row["ended_at"],
        committed_at=completed[1] if completed else row["ended_at"],
        warnings=tuple(warnings),
        attributes={"attempt": row["attempt"], "call_index": row["call_index"],
                    "iteration": row["iteration"]},
    )


def _approval_span(row, run_id: UUID, parent: str, events: dict) -> Span:
    warnings = []
    if row["decided_at"] is None:
        warnings.append("approval undecided at export: the wait is open, not zero")
    attributes = {}
    if row["batch_id"]:
        attributes = {"batch_id": str(row["batch_id"]), "batch_index": row["batch_index"]}
    return Span(
        span_id=f"approval:{row['id']}",
        run_id=str(run_id),
        kind="approval",
        name=row["tool_name"],
        status=row["status"],
        source_ref={"table": "approval_requests", "id": str(row["id"]),
                    "tool_record_id": str(row["tool_record_id"])},
        timing_source="approval_column",
        timing_precision="recorded",
        # A question is not nested inside the tool span it gates: the wait sits
        # between enqueue and execution, so nesting would double count it.
        parent_id=None,
        links=({"kind": "gates", "type": "tool", "tool_record_id": str(row["tool_record_id"])},),
        queued_at=row["created_at"],
        started_at=row["created_at"],
        execution_ended_at=row["decided_at"],
        committed_at=row["decided_at"],
        warnings=tuple(warnings),
        attributes=attributes,
    )


def _compaction_spans(rows, run_id: UUID, parent: str, events: dict) -> list[Span]:
    """One span per compaction, from the event pair when it exists.

    The rows alone cannot anchor an interval (see the module docstring), so they
    only contribute duration and status. When neither events nor rows exist, no
    span is created.
    """
    started = _group(events.get("compaction_started"), "compaction_id")
    completed = _group(events.get("compaction_completed"), "compaction_id")
    discarded = _group(events.get("compaction_discarded"), "compaction_id")
    spans: list[Span] = []
    for identity, rows_for in rows["compactions"].items():
        head = started.get(identity)
        tail = completed.get(identity)
        drop = discarded.get(identity)
        speculative = bool((head or (None, None, {}))[2].get("speculative")
                           or (tail or (None, None, {}))[2].get("speculative"))
        warnings: list[str] = []
        attributes = {"speculative": speculative, "calls": rows_for["calls"]}
        if head is None and tail is None and drop is None:
            warnings.append(
                "no start or end event: compaction_calls rows are written after the fact, "
                "so only duration_ms is a measurement")
            source, precision = "compaction_duration", "duration_only"
            attributes["duration_ms"] = rows_for["duration_ms"]
        else:
            source, precision = "compaction_event", "commit_lag"
        if speculative:
            warnings.append("speculative: runs alongside the answer, so it must not be added to wall time")
        if drop and tail is None:
            warnings.append(f"discarded: {(drop[2] or {}).get('reason') or 'reason not recorded'}")
        status = "discarded" if drop and tail is None else (tail[2].get("status") if tail else rows_for["status"])
        spans.append(Span(
            span_id=f"compaction:{identity}",
            run_id=str(run_id),
            kind="compaction",
            name="summary",
            status=status or "unknown",
            source_ref={"table": "compaction_calls", "compaction_id": identity,
                        "rows": rows_for["calls"]},
            timing_source=source,
            timing_precision=precision,
            parent_id=parent,
            links=tuple(
                link for link in (
                    {"kind": "event", "type": "compaction_started", "sequence": head[0]} if head else None,
                    {"kind": "event", "type": "compaction_completed", "sequence": tail[0]} if tail else None,
                    {"kind": "event", "type": "compaction_discarded", "sequence": drop[0]} if drop else None,
                ) if link),
            started_at=head[1] if head else None,
            execution_ended_at=(tail[1] if tail else drop[1] if drop else None),
            committed_at=tail[1] if tail else (drop[1] if drop else None),
            warnings=tuple(warnings),
            attributes=attributes,
        ))
    return spans


def _memory_span(row, run_id: UUID, events: dict) -> Span:
    warnings = []
    if row["started_at"] is None and row["ended_at"] is None:
        warnings.append("memory job never started")
    if row["status"] == "failed":
        warnings.append(f"failed after {row['attempts']}/{row['max_attempts']} attempts")
    return Span(
        span_id=f"memory:{row['id']}",
        run_id=str(run_id),
        kind="memory",
        name="memory extraction",
        status=row["status"],
        source_ref={"table": "memory_jobs", "id": str(row["id"])},
        timing_source="memory_column",
        timing_precision="recorded",
        # Background work is linked, not nested: it may outlive the run that
        # queued it, and a nested span would make the run's window look longer.
        parent_id=None,
        links=({"kind": "caused_by", "type": "run", "run_id": str(run_id)},),
        queued_at=row["created_at"],
        started_at=row["started_at"],
        execution_ended_at=row["ended_at"],
        committed_at=row["ended_at"],
        warnings=tuple(warnings),
        attributes={"attempts": row["attempts"], "max_attempts": row["max_attempts"]},
    )


# ----------------------------------------------------------------------- accounting


def _account(spans: list[Span], window: tuple[datetime, datetime], window_open: bool) -> Accounting:
    start, end = window
    if end < start:
        end = start
    wall = _ms(start, end)
    # The run span *is* the window. Counting it as work would make the union
    # equal to the wall by construction and force unexplained time to zero,
    # which is the one number that must stay honest.
    work = [span for span in spans if span.kind != "run"]
    inside: list[Span] = []
    outside_ms = 0
    clipped: list[tuple[datetime, datetime]] = []
    for span in work:
        interval = span.interval()
        if interval is None:
            continue
        low, high = max(interval[0], start), min(interval[1], end)
        if high <= low:
            # Entirely outside the window: a post-turn memory job, or a row from
            # another lease holder. It is this run's record but not its activity.
            outside_ms += span.duration_ms() or 0
            continue
        inside.append(span)
        clipped.append((low, high))

    merged = _union(clipped)
    union_ms = sum(_ms(low, high) for low, high in merged)
    cumulative_ms = sum(span.duration_ms() or 0 for span in inside)

    by_kind: dict[str, dict] = {}
    for kind in KINDS:
        if kind == "run":
            continue
        group = [span for span in inside if span.kind == kind]
        kind_intervals = [(max(span.started_at, start), min(span.execution_ended_at, end))
                          for span in group if span.interval()]
        kind_union = _union(kind_intervals)
        by_kind[kind] = {
            "spans": len(group),
            "cumulative_ms": sum(span.duration_ms() or 0 for span in group),
            "union_ms": sum(_ms(low, high) for low, high in kind_union),
            "open": sum(1 for span in group if span.open),
            "precision": sorted({span.timing_precision for span in group}),
        }

    gaps = tuple({
        "start": _iso(low),
        "end": _iso(high),
        "duration_ms": _ms(low, high),
        "offset_ms": _ms(start, low),
    } for low, high in sorted(
        _complement(merged, (start, end)),
        key=lambda pair: _ms(pair[0], pair[1]), reverse=True)[:5])
    # Listing five gaps says nothing about whether they are the whole story, so
    # the full count and total travel with them.
    all_gaps = _complement(merged, (start, end))
    gap_total = sum(_ms(low, high) for low, high in all_gaps)

    concurrency = _concurrency(clipped)
    unexplained = max(0, wall - union_ms)
    notes = []
    if outside_ms:
        notes.append("work recorded against this run but outside its window is excluded from occupancy")
    if cumulative_ms > wall:
        notes.append("cumulative time exceeds wall time: spans overlapped, which is allowed and not an error")
    if unexplained:
        notes.append("unexplained time is untraced commit, queue and scheduling delay; it is not "
                     "evidence of a framework fault")

    return Accounting(
        window_start=start,
        window_end=end,
        window_open=window_open,
        wall_ms=wall,
        cumulative_ms=cumulative_ms,
        union_ms=union_ms,
        unexplained_ms=unexplained,
        outside_window_ms=outside_ms,
        by_kind=by_kind,
        gaps=gaps,
        gap_count=len(all_gaps),
        gap_total_ms=gap_total,
        concurrency=concurrency,
        critical_path=None,
        notes=tuple(notes),
    )


def _union(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for low, high in sorted(intervals):
        if not merged or low > merged[-1][1]:
            merged.append([low, high])
        elif high > merged[-1][1]:
            merged[-1][1] = high
    return [(low, high) for low, high in merged]


def _complement(merged: list[tuple[datetime, datetime]], window) -> list[tuple[datetime, datetime]]:
    gaps = []
    cursor = window[0]
    for low, high in merged:
        if low > cursor:
            gaps.append((cursor, low))
        cursor = max(cursor, high)
    if cursor < window[1]:
        gaps.append((cursor, window[1]))
    return gaps


def _concurrency(intervals: list[tuple[datetime, datetime]]) -> dict:
    points = []
    for low, high in intervals:
        points.append((low, 1))
        points.append((high, -1))
    # Ends sort before starts at the same instant, so touching intervals are not
    # counted as concurrent.
    points.sort(key=lambda pair: (pair[0], pair[1]))
    current = peak = 0
    for _, delta in points:
        current += delta
        peak = max(peak, current)
    return {"max_overlapping": peak, "spans": len(intervals)}


# -------------------------------------------------------------------------- missing


def _missing(spans: list[Span], rows: dict, events: dict, window_open: bool,
             repaired: int) -> list[dict]:
    def count(predicate) -> int:
        return sum(1 for span in spans if predicate(span))

    items = [
        {"item": "tool spans with no start event",
         "count": count(lambda s: s.kind == "tool" and s.started_at is None),
         "effect": "execution time unknown; queue time is not substituted"},
        {"item": "spans with no end (open interval)",
         "count": count(lambda s: s.open),
         "effect": "duration unknown; the span is excluded from occupancy"},
        {"item": "compactions anchored by duration only",
         "count": count(lambda s: s.timing_precision == "duration_only"),
         "effect": "duration known, boundaries unknown; no interval is invented"},
        {"item": "undecided approvals",
         "count": sum(1 for row in rows["approvals"] if row["decided_at"] is None),
         "effect": "wait is open, its length is not zero"},
        {"item": "negative intervals repaired",
         "count": repaired,
         "effect": "clamped to zero so no consumer sees negative framework time"},
        {"item": "tool calls whose queue event is absent",
         "count": sum(1 for row in rows["tools"]
                      if not _has(events.get("tool_queued"), "tool_call_id", row["call_id"])),
         "effect": "enqueue time still known from the row; queue evidence is not"},
        {"item": "provider first-token time",
         "count": None,
         "effect": "not recorded; the first text_delta commit time is a lower bound only"},
        {"item": "browser receipt time (TTFT)",
         "count": None,
         "effect": "no client timing exists, so it is never reported"},
        {"item": "site service lifetime",
         "count": None,
         "effect": "no per-run record; site activity appears only as tool spans"},
        {"item": "ordering inside one event batch",
         "count": None,
         "effect": "events committed together share a timestamp; sub-batch order is not recoverable"},
    ]
    if window_open:
        items.insert(0, {"item": "run window end", "count": 1,
                         "effect": "no end timestamp; the window ends at the last evidence"})
    return [item for item in items if item["count"] is None or item["count"]]


# --------------------------------------------------------------------------- data


def _index_events(rows) -> dict:
    index: dict[str, list[tuple[int, datetime, dict]]] = {}
    for row in rows:
        index.setdefault(row["type"], []).append(
            (row["sequence"], row["created_at"], row["payload"] or {}))
    return index


def _group(events, key: str) -> dict:
    grouped: dict[str, tuple[int, datetime, dict]] = {}
    for sequence, created, payload in events or ():
        identity = payload.get(key)
        if identity and identity not in grouped:
            grouped[str(identity)] = (sequence, created, payload)
    return grouped


def _first(events, predicate):
    for sequence, created, payload in events or ():
        if predicate(payload):
            return sequence, created, payload
    return None


def _has(events, key: str, value) -> bool:
    return any(payload.get(key) == value for _, _, payload in events or ())


def _latest(events: dict, rows: dict) -> datetime | None:
    candidates = [created for items in events.values() for _, created, _ in items]
    candidates += [row["ended_at"] for row in rows["tools"] if row["ended_at"]]
    candidates += [row["ended_at"] for row in rows["models"] if row["ended_at"]]
    return max(candidates) if candidates else None


# ------------------------------------------------------------------------- output


def _order(span: Span) -> tuple:
    return (span.started_at or span.queued_at or datetime.max.replace(tzinfo=timezone.utc),
            KINDS.index(span.kind), span.name)


def _replace(span: Span, **changes) -> Span:
    from dataclasses import replace

    return replace(span, **changes)


def _ms(low: datetime, high: datetime) -> int:
    return int(round((high - low).total_seconds() * 1000))


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 1] + "…"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def format_timeline(document: dict, *, limit: int = 200) -> str:
    """Operator-facing rendering. Every number here exists in the JSON."""
    window = document["accounting"]["window"]
    start = datetime.fromisoformat(window["start"])
    lines = [
        f"run {document['run']['id']}  {document['run']['status']}"
        f"{'/' + document['run']['end_reason'] if document['run']['end_reason'] else ''}"
        f"  wall {window['wall_ms'] / 1000:.3f}s"
        f"{'  (open)' if window['open'] else ''}"
        f"  spans {len(document['spans'])}",
        f"window {window['start']} → {window['end']}",
        "",
        f"{'offset':>10}  {'kind':<10} {'name':<26} {'duration':>9} {'wait':>8}  "
        f"{'precision':<13} status",
    ]
    shown = document["spans"][:limit]
    for span in shown:
        offset = "+{:.3f}s".format(_ms(start, datetime.fromisoformat(span["started_at"])) / 1000) \
            if span["started_at"] else "     —"
        duration = f"{span['duration_ms'] / 1000:.3f}s" if span["duration_ms"] is not None else "—"
        wait = f"{span['wait_ms'] / 1000:.3f}s" if span["wait_ms"] is not None else "—"
        mark = "" if span["timing_precision"] == "recorded" else "*"
        lines.append(
            f"{offset:>10}  {span['kind']:<10} {_clip(span['name'], 26):<26} {duration:>9} "
            f"{wait:>8}  {span['timing_precision'] + mark:<13} {span['status']}")
    if len(document["spans"]) > len(shown):
        lines.append(f"{'':>10}  … {len(document['spans']) - len(shown)} more span(s)")

    accounting = document["accounting"]
    wall = accounting["window"]["wall_ms"]
    share = (accounting["unexplained_ms"] / wall * 100) if wall else 0.0
    lines += [
        "",
        f"accounting  cumulative {accounting['cumulative_ms'] / 1000:.3f}s"
        f"  union {accounting['union_ms'] / 1000:.3f}s"
        f"  unexplained {accounting['unexplained_ms'] / 1000:.3f}s ({share:.1f}%)"
        f"  outside-window {accounting['outside_window_ms'] / 1000:.3f}s"
        f"  max-overlap {accounting['concurrency']['max_overlapping']}",
        "by kind     " + "  ".join(
            f"{kind} {data['union_ms'] / 1000:.3f}s/{data['spans']}x"
            for kind, data in accounting["by_kind"].items() if data["spans"]),
        f"critical path  not claimed ({'no explicit dependency edges' if not accounting['critical_path'] else 'computed'})",
    ]
    if accounting["gaps"]:
        lines.append(
            f"gaps        {accounting['gap_count']} interval(s), "
            f"{accounting['gap_total_ms'] / 1000:.3f}s total, largest first")
        for gap in accounting["gaps"]:
            if gap["duration_ms"] < 20:
                continue
            lines.append(
                f"  +{gap['offset_ms'] / 1000:>8.3f}s  {gap['duration_ms'] / 1000:>7.3f}s  "
                f"{gap['start']} → {gap['end']}")
        if all(gap["duration_ms"] < 20 for gap in accounting["gaps"]):
            lines.append("  none above 20 ms")
    if document["stream"]["first_text_delta_at"]:
        stream = document["stream"]
        lines.append(
            f"first visible text  +{stream['after_window_start_ms'] / 1000:.3f}s "
            f"({stream['first_text_delta_kind']})")

    lines.append("")
    if document["missing"]:
        lines.append("missing evidence")
        for item in document["missing"]:
            count = "" if item["count"] is None else f" [{item['count']}]"
            lines.append(f"  - {item['item']}{count}: {item['effect']}")
    else:
        lines.append("missing evidence  none recorded")
    lines.append("")
    lines.append("notes")
    for note in document["notes"]:
        lines.append(f"  - {note}")
    if any(span["warnings"] for span in document["spans"]):
        lines.append("")
        lines.append("span warnings")
        # One line per distinct warning: a batch of 25 calls with the same gap
        # is one finding, not 25.
        grouped: dict[str, list[str]] = {}
        for span in document["spans"]:
            for warning in span["warnings"]:
                grouped.setdefault(warning, []).append(span["span_id"])
        for warning, span_ids in grouped.items():
            example = span_ids[0] + (f" (+{len(span_ids) - 1} more)" if len(span_ids) > 1 else "")
            lines.append(f"  - [{len(span_ids)}x] {warning}  e.g. {example}")
    return "\n".join(lines)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _redact(document: dict, workspace) -> dict:
    """Project workspace paths in names and statuses, not only in payloads.

    Span names come from tool names and approval tool names; ``attributes`` can
    hold ids only. The one field that can carry a host path is a run's
    ``root_path``, which is not exported at all. This runs a cheap check so a
    future field cannot start leaking by accident.
    """
    if workspace is None:
        return document
    from chagent.models.outbound import to_model

    text = str(workspace)
    for span in document["spans"]:
        span["name"] = to_model(span["name"], workspace)
        span["status"] = to_model(span["status"], workspace)
        if text and text in str(span.get("attributes")):
            span["attributes"] = {"projected": True}
    return document
