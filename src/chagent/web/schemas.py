"""HTTP contracts; domain/storage classes remain independent of FastAPI."""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from chagent.domain.memories import Memory, validate_memory
from chagent.domain.runs import RunEndReason
from chagent.domain.sessions import Session


Revision = Annotated[int, Field(strict=True, ge=1)]
MemoryContent = Annotated[StrictStr, Field(min_length=1, max_length=2000)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CancelRequest(InputModel):
    pass


class SessionCreate(InputModel):
    title: Annotated[StrictStr, Field(max_length=200)] = "新会话"


class RunCreate(InputModel):
    text: Annotated[StrictStr, Field(min_length=1, max_length=16000)]

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Message must not be empty")
        return value


class MemoryCreate(InputModel):
    key: Annotated[StrictStr, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]{0,79}$")]
    content: MemoryContent
    scope: Literal["user", "workspace"] = "workspace"
    pinned: StrictBool = False

    @model_validator(mode="after")
    def valid_content(self):
        self.content = validate_memory(self.key, self.content, self.scope, self.pinned)
        return self


class RevisionRequest(InputModel):
    expected_revision: Revision


class MemoryEdit(RevisionRequest):
    content: MemoryContent | SkipJsonSchema[None] = Field(default=None, json_schema_extra=lambda schema: schema.pop("default", None))
    pinned: StrictBool | SkipJsonSchema[None] = Field(default=None, json_schema_extra=lambda schema: schema.pop("default", None))

    @field_validator("content", "pinned", mode="before")
    @classmethod
    def not_null(cls, value):
        if value is None:
            raise ValueError("An explicit null value is not allowed")
        return value

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set.intersection({"content", "pinned"}):
            raise ValueError("Provide content or pinned to edit")
        return self


class MemoryReview(RevisionRequest):
    replace_id: UUID | None = None
    replace_revision: Revision | None = None

    @model_validator(mode="after")
    def replacement_pair(self):
        if (self.replace_id is None) != (self.replace_revision is None):
            raise ValueError("Replacement ID and revision must be supplied together")
        return self


class ErrorDetail(BaseModel):
    location: list[str | int]
    message: str
    type: str


class ErrorResponse(BaseModel):
    error: str
    details: list[ErrorDetail] = Field(default_factory=list)


class AppInfo(BaseModel):
    workspace: Path
    provider: str
    model: str


class SessionPage(BaseModel):
    items: list[Session]
    next_cursor: UUID | None = None


class RunView(BaseModel):
    id: UUID
    session_id: UUID
    status: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
    error: str | None
    end_reason: RunEndReason | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    # A waiting run stays ``running``; this is a projection over approval_requests.
    waiting_for: Literal["approval"] | None = None
    approval_id: UUID | None = None
    approval: dict | None = None


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: str | None = Field(default=None, max_length=2000)


class SessionDetail(BaseModel):
    id: UUID
    workspace_id: UUID
    workspace: Path
    title: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    latest_run: RunView | None


class RunAccepted(BaseModel):
    run_id: UUID
    session_id: UUID


class Cancellation(BaseModel):
    accepted: bool
    status: str | None = None


class StoredMessageView(BaseModel):
    id: UUID
    sequence: int
    run_id: UUID | None
    message: dict[str, Any]


class MessagePage(BaseModel):
    items: list[StoredMessageView]
    next_before: int | None


class ArtifactPage(BaseModel):
    id: UUID
    char_count: int
    byte_count: int
    sha256: str
    source_truncated: bool
    text: str
    offset: int
    next_offset: int | None


class ContextView(BaseModel):
    summaries: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]


class MemoryPage(BaseModel):
    items: list[Memory]
    next_cursor: UUID | None


class MemoryDetail(BaseModel):
    memory: Memory
    conflict: Memory | None


class MemoryJobView(BaseModel):
    id: UUID
    run_id: UUID
    status: Literal["queued", "running", "completed", "failed"]
    attempts: int
    max_attempts: int
    created_at: datetime
    ended_at: datetime | None


class MemoryJobState(BaseModel):
    job: MemoryJobView | None
