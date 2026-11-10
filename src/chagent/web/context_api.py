"""Context, artifacts and generated files: what a run produced and was given.

Separate from the run endpoints because these answer "show me the evidence"
rather than "move the run forward" — they never mutate a run.
"""

from dataclasses import asdict
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response

from chagent.storage.artifacts import ArtifactRepository
from chagent.storage.summaries import SummaryRepository
from chagent.web.deps import Current
from chagent.web.responses import ERROR_RESPONSES
from chagent.web.schemas import ArtifactPage, ContextView, SessionSources

router = APIRouter(prefix="/api", responses=ERROR_RESPONSES)


@router.get("/sessions/{session_id}/files", tags=["上下文与产物"])
async def generated_files(session_id: UUID, current: Current):
    await current.session(session_id)
    async with current.runner.conversations.database.connection() as connection:
        outputs = await connection.execute("SELECT id,workspace_id,output_path AS path,size,status FROM file_outputs WHERE session_id=%s AND status='committed' ORDER BY created_at DESC", (session_id,))
        disk = [{**row, "name": Path(row["path"]).name} for row in await outputs.fetchall()]
        cursor = await connection.execute(
            "SELECT id, run_id, name, octet_length(content) AS size, created_at FROM generated_files "
            "WHERE session_id = %s ORDER BY created_at DESC, id DESC", (session_id,))
        return {"items": disk + await cursor.fetchall()}


@router.get("/sessions/{session_id}/files/{file_id}", tags=["上下文与产物"])
async def generated_file_download(session_id: UUID, file_id: UUID, current: Current):
    await current.session(session_id)
    async with current.runner.conversations.database.connection() as connection:
        cursor = await connection.execute("SELECT name, content FROM generated_files WHERE session_id = %s AND id = %s", (session_id, file_id))
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, "File does not exist in this session")
    return Response(content=row["content"].encode("utf-8"), media_type="application/octet-stream",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["name"], safe=""),
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post('/sessions/{session_id}/files/{file_id}/export', tags=['上下文与产物'])
async def export_legacy(session_id: UUID, file_id: UUID, current: Current):
    from chagent.outputs import OutputService
    session = await current.session(session_id)
    async with current.runner.conversations.database.connection() as connection:
        cursor = await connection.execute('SELECT name,content FROM generated_files WHERE id=%s AND session_id=%s', (file_id,session_id))
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(404, '历史产物不存在')
    return await OutputService(current.runner.conversations.database).submit(session.workspace_id, session.workspace,
        row['name'],row['content'].encode('utf-8'),source='legacy-export',session_id=session_id)


@router.get("/sessions/{session_id}/artifacts/{artifact_id}", response_model=ArtifactPage, tags=["上下文与产物"])
async def artifact_page(session_id: UUID, artifact_id: UUID, current: Current,
                        offset: Annotated[int, Query(ge=0, le=2_147_483_646)] = 0,
                        limit: Annotated[int, Query(ge=1, le=1000)] = 1000):
    await current.session(session_id)
    try:
        return await ArtifactRepository(current.runner.conversations.database).read(session_id, artifact_id, offset=offset, limit=limit)
    except LookupError:
        raise HTTPException(404, "Artifact does not exist in this session") from None


@router.get("/sessions/{session_id}/context", response_model=ContextView, tags=["上下文与产物"])
async def context_detail(session_id: UUID, current: Current):
    await current.session(session_id)
    summaries = await SummaryRepository(current.runner.conversations.database).list(session_id, limit=10)
    versions = []
    for summary in summaries:
        source = await current.runner.runs.get(summary.source_run_id) if summary.source_run_id else None
        versions.append({**asdict(summary), "source_run_status": source["status"] if source else None,
                         "replayable": source is None or source["status"] in {"running", "completed"}})
    return {"summaries": versions, "snapshots": await current.sessions.contexts(session_id, limit=3)}


@router.get("/sessions/{session_id}/sources", response_model=SessionSources, tags=["上下文与产物"])
async def session_sources(session_id: UUID, current: Current):
    """The memories each run of this session was given, for its 依据 section.

    One request per session rather than one per answer: memory is not a message,
    so the transcript cannot rebuild it from what it already has.
    """
    await current.session(session_id)
    return {"runs": await current.runner.conversations.run_memory_references(session_id)}
