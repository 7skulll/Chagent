"""Runs: start, watch, approve, cancel, and the event stream.

The event stream is the only long-lived response in the application, so the
terminal-event names and the poll interval it depends on live here rather than
next to the routes that merely read a run once.
"""

import json
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from chagent.storage.memory_jobs import MemoryJobRepository
from chagent.web.deps import Current, SequenceCursor
from chagent.web.responses import ERROR_RESPONSES
from chagent.web.runtime import WebRuntime
from chagent.web.schemas import (ActiveRunPage, ApprovalDecision, CancelRequest, Cancellation,
                                 MemoryJobState, RunAccepted, RunCreate, RunView)

router = APIRouter(prefix="/api", responses=ERROR_RESPONSES)

SSE_POLL_INTERVAL = 1.0
TERMINAL_EVENTS = {"run_completed", "run_failed", "run_cancelled", "run_interrupted"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


@router.post("/sessions/{session_id}/runs", response_model=RunAccepted, status_code=202, tags=["运行"])
async def send(session_id: UUID, value: RunCreate, current: Current):
    await current.session(session_id)  # Isolation check only; the id is enough.
    supersede_from = await _replacement_point(current, session_id, value)
    return {"run_id": await current.start(session_id, value.text or "", supersede_from=supersede_from),
            "session_id": session_id}


async def _replacement_point(current: WebRuntime, session_id: UUID, value: RunCreate) -> int | None:
    """Which transcript record a regeneration or an edit starts over from.

    The sequence, not the id: retirement is a suffix, and a message cannot be
    replaced without the turns that followed it going stale too. Both refusals
    are 400 rather than 404: the session exists, the message does not belong to
    the kind of record the request names.
    """
    target_id = value.regenerate_from or value.edit_message_id
    if target_id is None:
        return None
    record = await current.sessions.message(session_id, target_id)
    if record is None:
        raise HTTPException(404, "消息不属于这个会话")
    expected = "assistant" if value.regenerate_from is not None else "user"
    if record.message.role != expected:
        raise HTTPException(400, "重新生成针对回答，编辑针对你自己的消息")
    if record.superseded_by is not None:
        raise HTTPException(409, "这条消息已经被替换过，刷新后再试")
    return record.sequence


@router.get("/runs", response_model=ActiveRunPage, tags=["运行"])
async def active_runs(current: Current, active: bool = True):
    """Runs that have not converged yet across the opened workspaces.

    Only the active view exists: a list of every run ever would be a different
    endpoint with pagination, and nothing asks for it.
    """
    if not active:
        raise HTTPException(400, "Only active runs are listed; omit the parameter or pass active=true")
    rows = await current.runner.runs.active(roots=sorted(str(root) for root in current.roots))
    return {"items": [{"run_id": row["id"], "session_id": row["session_id"],
                        "started_at": row["started_at"], "current_tool": row["current_tool"]}
                       for row in rows]}


@router.get("/runs/{run_id}", response_model=RunView, tags=["运行"])
async def run_detail(run_id: UUID, current: Current):
    run = await current.run(run_id)
    return {**run, "file_diff": await current.runner.runs.file_diff(run_id),
            "network_observation": await current.runner.runs.network_observation(run_id)}


@router.get("/runs/{run_id}/approvals", tags=["运行"])
async def run_approvals(run_id: UUID, current: Current):
    await current.run(run_id)  # Same workspace isolation as the run/event endpoints.
    pending = await current.approvals.pending(run_id)
    return {"waiting_for": "approval" if pending else None,
            "pending": [{"id": str(item.id), "tool_name": item.tool_name, "subject": item.subject,
                         "cwd": item.cwd, "expires_at": item.expires_at.isoformat(),
                         "status": item.status,
                         # A page shows one card per batch; the ids and the order
                         # are what let it describe one decision over N calls.
                         "batch_id": str(item.batch_id) if item.batch_id else None,
                         "batch_index": item.batch_index} for item in pending]}


@router.post("/approvals/{approval_id}/decide", tags=["运行"])
async def decide_approval(approval_id: UUID, value: ApprovalDecision, current: Current):
    """The only way to approve: a local UI request bound to this workspace."""
    approval = await current.approvals.get(approval_id)
    if approval is None:
        raise HTTPException(404, "Approval does not exist")
    run = await current.run(approval.run_id)
    status = await current.approvals.decide(approval_id, approve=value.approve, note=value.note)
    if status == "conflict":
        raise HTTPException(409, "Approval is no longer pending (decided, expired or the run ended)")
    return {"id": str(approval_id), "status": status, "run_id": str(run["id"])}


@router.post("/runs/{run_id}/cancel", response_model=Cancellation, response_model_exclude_none=True,
          responses={202: {"model": Cancellation}}, tags=["运行"])
async def cancel(run_id: UUID, current: Current, response: Response, value: CancelRequest | None = None):
    run = await current.run(run_id)
    if run["status"] in TERMINAL:
        return {"accepted": False, "status": run["status"]}
    accepted = current.runner.cancel(run_id)
    if not accepted:
        latest = await current.run(run_id)
        if latest["status"] in TERMINAL:
            return {"accepted": False, "status": latest["status"]}
        raise HTTPException(409, "Run belongs to another process or needs recovery")
    async with current.runner.conversations.database.connection() as connection:
        await connection.execute("UPDATE file_outputs SET status='cancelled',payload=NULL WHERE run_id=%s AND status='pending'", (run_id,))
    response.status_code = 202
    return {"accepted": accepted}


@router.get("/runs/{run_id}/memory-job", response_model=MemoryJobState, tags=["长期记忆"])
async def memory_job(run_id: UUID, current: Current):
    await current.run(run_id)  # Same workspace isolation as the run/event endpoints.
    jobs = MemoryJobRepository(current.runner.runs.database)
    return {"job": await jobs.for_run(run_id)}


@router.get("/runs/{run_id}/events", response_class=StreamingResponse, tags=["运行"],
         responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def events(run_id: UUID, request: Request, current: Current, after: SequenceCursor = 0,
                 last_event_id: Annotated[int | None, Header(ge=0, le=9_223_372_036_854_775_807)] = None):
    identity = run_id
    await current.run(identity)
    after = last_event_id if last_event_id is not None else after

    async def stream():
        sequence = after
        heartbeat = monotonic()
        runs = current.runner.runs
        # A viewer is counted for as long as this generator runs. The broker
        # reads the count to decide whether a waiting command still has a page
        # to be approved on; ``finally`` is what keeps a dropped connection from
        # leaving a phantom audience behind.
        current.watch(identity)
        try:
            with runs.notifications.subscribe(identity) as subscription:
                while True:
                    # Capture before catchup, so commits between querying and waiting
                    # change the version and cannot be lost.
                    version = subscription.version
                    run = await runs.get(identity)
                    stop_at = await runs.terminal_sequence(identity) if run["status"] in TERMINAL else None
                    if stop_at is not None and sequence >= stop_at:
                        yield "event: end\ndata: {}\n\n"
                        return
                    batch = await runs.events(identity, sequence, limit=200)
                    for event in batch:
                        sequence = event["sequence"]
                        payload = json.dumps({"type": event["type"], "payload": event["payload"]}, ensure_ascii=False)
                        yield f"id: {sequence}\nevent: update\ndata: {payload}\n\n"
                        if event["type"] in TERMINAL_EVENTS:
                            yield "event: end\ndata: {}\n\n"
                            return
                    if run["status"] in TERMINAL and len(batch) < 200:
                        yield "event: end\ndata: {}\n\n"
                        return
                    if await request.is_disconnected():
                        return
                    if monotonic() - heartbeat > 15:
                        yield ": heartbeat\n\n"
                        heartbeat = monotonic()
                    if len(batch) < 200:
                        await subscription.wait(version, timeout=SSE_POLL_INTERVAL)
        finally:
            current.unwatch(identity)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
    })
