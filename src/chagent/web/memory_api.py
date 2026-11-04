"""Long-term memory endpoints: list, propose, edit, review and history."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter

from chagent.domain.memories import Memory, MemoryProposal
from chagent.storage.memories import MemoryRepository
from chagent.web.deps import Current, PageLimit, WorkspacePath
from chagent.web.schemas import (ErrorResponse, MemoryCreate, MemoryDetail, MemoryEdit, MemoryPage,
                                 MemoryReview, RevisionRequest)

router = APIRouter(
    prefix="/api", tags=["长期记忆"],
    responses={code: {"model": ErrorResponse} for code in (400, 403, 404, 409, 413, 415, 503)},
)


@router.get("/memories", response_model=MemoryPage)
async def memories(current: Current, status: Literal["active", "candidate", "deleted", "rejected", "all"] = "active",
                   limit: PageLimit = 50, before: UUID | None = None, workspace: WorkspacePath = None):
    repository = MemoryRepository(current.runner.conversations.database)
    return await repository.list(await current.memory_scope(workspace), status=status, limit=limit, before=before)


@router.post("/memories", response_model=Memory, status_code=201)
async def create_memory(value: MemoryCreate, current: Current, workspace: WorkspacePath = None):
    repository = MemoryRepository(current.runner.conversations.database)
    return await repository.propose(MemoryProposal(value.key, value.content, value.scope, value.pinned, "manual"),
                                    await current.memory_scope(workspace))


@router.get("/memories/{memory_id}", response_model=MemoryDetail)
async def memory_detail(memory_id: UUID, current: Current, workspace: WorkspacePath = None):
    repository = MemoryRepository(current.runner.conversations.database)
    memory = await repository.get(memory_id, await current.memory_scope(workspace))
    active = await repository.current(memory.scope, memory.workspace_id, memory.key) if memory.status == "candidate" else None
    return {"memory": memory, "conflict": active}


@router.patch("/memories/{memory_id}", response_model=Memory)
async def edit_memory(memory_id: UUID, value: MemoryEdit, current: Current, workspace: WorkspacePath = None):
    return await MemoryRepository(current.runner.conversations.database).change(
        memory_id, await current.memory_scope(workspace), expected_revision=value.expected_revision,
        content=value.content, pinned=value.pinned)


@router.delete("/memories/{memory_id}", response_model=Memory)
async def delete_memory(memory_id: UUID, value: RevisionRequest, current: Current, workspace: WorkspacePath = None):
    return await MemoryRepository(current.runner.conversations.database).change(
        memory_id, await current.memory_scope(workspace), expected_revision=value.expected_revision, action="delete")


@router.post("/memories/{memory_id}/accept", response_model=Memory)
async def accept_memory(memory_id: UUID, value: MemoryReview, current: Current, workspace: WorkspacePath = None):
    return await MemoryRepository(current.runner.conversations.database).change(
        memory_id, await current.memory_scope(workspace), expected_revision=value.expected_revision, action="accept",
        replace_revision=value.replace_revision, replace_id=value.replace_id)


@router.post("/memories/{memory_id}/reject", response_model=Memory)
async def reject_memory(memory_id: UUID, value: RevisionRequest, current: Current, workspace: WorkspacePath = None):
    return await MemoryRepository(current.runner.conversations.database).change(
        memory_id, await current.memory_scope(workspace), expected_revision=value.expected_revision, action="reject")


@router.get("/memories/{memory_id}/revisions")
async def memory_history(memory_id: UUID, current: Current, workspace: WorkspacePath = None):
    return await MemoryRepository(current.runner.conversations.database).revisions(
        memory_id, await current.memory_scope(workspace))
