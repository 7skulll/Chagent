"""HTTP contracts; domain/storage classes remain independent of FastAPI."""

from datetime import date, datetime
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


class HostLogin(InputModel):
    token: Annotated[StrictStr, Field(min_length=32, max_length=128)]


class SessionCreate(InputModel):
    title: Annotated[StrictStr, Field(max_length=200)] = "新会话"
    # Absent means the workspace the server was started in.
    workspace: Annotated[StrictStr, Field(min_length=1, max_length=4096)] | None = None
    #: Absent means the build's default mode. ``code`` in a work-only build is
    #: refused with 409 rather than downgraded: the caller must know what it got.


class SessionEdit(InputModel):
    """Session metadata; every field is optional but at least one is required.

    ``title`` empty means "no title", not "leave it alone": an explicit null is
    rejected so a missing field and a cleared one stay different requests.
    ``mode`` switches which tool face later runs of this session resolve; it is
    refused while a run is active, because that run already pinned its face.
    """

    title: Annotated[StrictStr, Field(max_length=200)] | None = None
    archived: StrictBool | None = None
    #: Absent means "leave the mode alone". A build with no code face refuses
    #: code with 409 rather than silently downgrading it to work.

    @field_validator("title", "archived", mode="before")
    @classmethod
    def not_null(cls, value):
        if value is None:
            raise ValueError("An explicit null value is not allowed")
        return value

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set.intersection({"title", "archived"}):
            raise ValueError("Provide title or archived to edit")
        return self


class RunCreate(InputModel):
    """One way of asking for a run: a new message, an edit, or a regeneration.

    ``text`` is optional only because a regeneration has nothing new to say. The
    two replacement fields name a message that already exists in the session;
    the server retires everything from there on and re-asks from that point.
    """

    text: Annotated[StrictStr, Field(max_length=16000)] | None = None
    #: Accepted only to be refused when it disagrees with the session: a run
    #: inherits the mode, and switching it mid-conversation would make the tool
    #: declarations in the prompt differ from the face that executes.
    #: Regenerate the answer to the turn that produced this assistant message.
    regenerate_from: UUID | None = None
    #: Replace this user message with ``text`` and run from there.
    edit_message_id: UUID | None = None

    @model_validator(mode="after")
    def one_request(self):
        if self.regenerate_from is not None and self.edit_message_id is not None:
            raise ValueError("Regenerate and edit are different requests; send one")
        if self.regenerate_from is None and not (self.text or "").strip():
            raise ValueError("Message must not be empty")
        if self.edit_message_id is not None and not (self.text or "").strip():
            raise ValueError("An edited message must not be empty")
        return self


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
    code_mode: bool
    programmable: bool = False
    #: The three execution axes; see ``chagent.execution``.
    boundary: str = "none"
    capability: str = "read-only"
    gate: str = "none"


class WorkspaceView(BaseModel):
    id: UUID | None = None
    path: Path
    sessions: int
    last_used: datetime | None = None
    available: bool = True
    reason: str | None = None


class WorkspacePage(BaseModel):
    items: list[WorkspaceView]


class FileOperationView(BaseModel):
    """One recorded write to a workspace, from whichever surface performed it.

    ``source`` names the surface (``agent`` for a tool, ``manual`` for the in-page
    editor) and ``operation`` the verb it used, so the two are never conflated:
    a rename from the file panel and an ``edit_file`` call look different here.
    """

    id: int
    source: str
    operation: str
    path: str
    created_at: datetime


class FileOperationPage(BaseModel):
    items: list[FileOperationView]


class WorkspaceCreate(InputModel):
    path: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    touch: StrictBool = True


class DirectoryEntry(BaseModel):
    """One child directory of a browse target, already checked for opening."""

    name: str
    path: Path
    available: bool = True
    reason: str | None = None
    #: Already registered, so the page can show ``opened`` next to ``sessions``.
    opened: bool = False


class DirectoryListing(BaseModel):
    """What the in-page picker may walk: one bounded, server-side tree.

    A browser cannot hand the server a path it has no way to know, so the
    server publishes exactly one tree and the page navigates inside it.
    """

    path: Path
    root: Path
    parent: Path | None = None
    entries: list[DirectoryEntry]
    next_after: str | None = None


class ConfigView(BaseModel):
    """What the running process will actually do, not what the environment said.

    Secrets are reduced to ``set``/``missing``: the page is read-only and must
    never become a way to read a key back out of the server.
    """

    provider: str
    model: str
    run_timeout: float
    run_wall_timeout: float | None = None
    boundary: str = "none"
    capability: str = "read-only"
    gate: str = "none"
    controller: str = "configured"
    sandbox_network: bool
    workspace_writable: bool
    process_stdin: bool
    approval: str
    approval_timeout: float
    approval_grace: float
    container_idle_seconds: int = 1800
    container_state: str | None = None
    image_stale: bool | None = None
    host_commands: bool
    host_execution: bool = False
    host_workspace_path: str = ""
    web_access: str
    web_key: Literal["set", "missing"]
    compaction: bool
    tokenizer: str
    max_turns: int
    max_tool_concurrency: int
    max_processes: int
    process_timeout: float
    tools: int
    code_mode: bool
    programmable: bool = False
    behavior: str


class SessionPage(BaseModel):
    items: list[Session]
    next_cursor: UUID | None = None
    #: Archived sessions of the same workspace, for the sidebar's toggle label.
    archived_count: int | None = None


class UsageTotals(BaseModel):
    runs: int
    model_calls: int
    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int
    #: ``cache_read / (input + cache_read)``; null when the denominator is 0, so
    #: the page can print "—" instead of a hollow 0%.
    cache_hit_rate: float | None


class UsageRow(BaseModel):
    run_id: UUID
    started_at: datetime | None
    model_calls: int
    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int


class UsageView(BaseModel):
    items: list[UsageRow]
    totals: UsageTotals


class DailyUsageRow(BaseModel):
    day: date
    model_calls: int
    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int


class DailyUsageView(BaseModel):
    days: int
    items: list[DailyUsageRow]
    totals: UsageTotals


class ActiveRunView(BaseModel):
    run_id: UUID
    session_id: UUID
    started_at: datetime | None
    #: The tool this run is executing right now, if any.
    current_tool: str | None


class ActiveRunPage(BaseModel):
    items: list[ActiveRunView]


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
    file_diff: dict | None = None
    network_observation: dict | None = None


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
    mode: Literal["work", "code"]
    latest_run: RunView | None


class RunAccepted(BaseModel):
    run_id: UUID
    session_id: UUID


class MemorySourceView(BaseModel):
    """One memory row a run was given, as the transcript shows it under 依据."""

    id: str
    key: str
    scope: Literal["user", "workspace"]
    revision: int
    content: str
    pinned: bool
    origin: str


class RunMemorySources(BaseModel):
    run_id: UUID
    memories: list[MemorySourceView]


class SessionSources(BaseModel):
    """What each run of a session cited outside its own text.

    Web and document sources live in the tool results, which the history view
    already carries; only memory is not a message, so the transcript loads this
    once per session and stamps each answer with its own 依据.
    """

    runs: list[RunMemorySources]


class Cancellation(BaseModel):
    accepted: bool
    status: str | None = None


class StoredMessageView(BaseModel):
    id: UUID
    sequence: int
    run_id: UUID | None
    #: Non-null means a later run replaced this record; the transcript keeps it
    #: for reading, the model no longer replays it.
    superseded_by: UUID | None = None
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


class MemoryUsageView(BaseModel):
    injection_count: int
    last_injected_at: datetime | None
    correction_count: int


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
