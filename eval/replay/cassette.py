"""Stored responses replayed against matching requests, stopping at the first drift.

A cassette records what the provider answered, keyed by a fingerprint of what was
asked. The fingerprint covers the parts a provider actually sees — instructions,
tool declarations, the message sequence, and the sampling parameters — and drops
the parts it does not: ``run_id``, the workspace path, message timestamps, usage
and provider-assigned response ids. Hashing those would make every replay diverge
for reasons that cannot change an answer.

What is *not* dropped is deliberate: a changed system section, a changed tool
declaration, or an extra message all change the fingerprint, and the replay stops
with ``CassetteDivergence`` naming the component that differs. It does not skip to
the next recorded response and it does not quietly call a real provider — either
would turn a drift into a plausible-looking pass. There is no such fallback path
in this module, which is the only way to promise there is none.

Two honest limits, written here rather than discovered later:

* Only the final message is stored. Text deltas were never persisted, so replay
  emits ``start`` and the terminating event, not a reconstruction of the stream.
  A check that needs deltas must record them separately.
* Response text is redacted on the way in with the same routine the diagnostics
  export uses. That is the right thing to keep on disk, and it means a *live*
  run whose history is rebuilt from this file will diverge on the second request.
  A cassette replays the transcript it was recorded from; it does not seed a new
  one.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from pathlib import Path
from uuid import UUID

from chagent.domain.codec import message_from_dict, message_to_dict
from chagent.domain.messages import Message
from chagent.models.base import Model, ModelRequest
from chagent.models.events import DoneEvent, ErrorEvent, StartEvent
from chagent.storage.run_diagnostics import safe_text

from eval.manifests import prompt_material, sha256_json, sha256_text

CASSETTE_VERSION = 2
#: Whole responses are the point, so the redactor's usual 2048 is raised; it is
#: still a bound, and hitting it is recorded rather than silent.
TEXT_LIMIT = 65536
#: Message fields a provider never sees. Dropping them is what makes a replay
#: insensitive to when it runs, not to what was asked.
NON_SEMANTIC = frozenset({
    "timestamp", "usage", "response_id", "response_model", "duration_ms",
    "error_message", "details", "artifact",
})
KINDS = ("response", "stream_error", "cancelled")


class CassetteInvalid(ValueError):
    """A cassette file does not satisfy the published shape."""


class CassetteExhausted(RuntimeError):
    """The run asked for one more response than the cassette holds."""


class CassetteDivergence(RuntimeError):
    """A request no longer matches the recorded one. Replay stops here."""

    def __init__(self, index: int, drifts: list[dict]) -> None:
        self.index = index
        self.drifts = tuple(drifts)
        first = drifts[0] if drifts else {"component": "unknown", "expected": None, "actual": None}
        super().__init__(
            f"request {index} diverges at {first['component']}: "
            f"recorded {first['expected']!r}, replaying {first['actual']!r}")

    def to_json(self) -> dict:
        return {"index": self.index, "drifts": list(self.drifts)}


@dataclass(frozen=True)
class Entry:
    kind: str
    fingerprint: dict
    response: dict | None
    status: str = "completed"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise CassetteInvalid(f"unknown entry kind: {self.kind!r}")
        if not isinstance(self.response, dict):
            raise CassetteInvalid("a recorded response needs a response object")
        _validate_fingerprint(self.fingerprint)

    def to_json(self) -> dict:
        _validate_fingerprint(self.fingerprint)
        return {"kind": self.kind, "status": self.status, "fingerprint": self.fingerprint,
                "response": self.response}


@dataclass(frozen=True)
class Cassette:
    entries: tuple[Entry, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        if not self.entries:
            raise CassetteInvalid("cassette has no entries")
        for entry in self.entries:
            _validate_fingerprint(entry.fingerprint)

    def save(self, path: Path) -> Path:
        document = {
            "cassette_version": CASSETTE_VERSION,
            "entries": [entry.to_json() for entry in self.entries],
            "warnings": list(self.warnings),
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "Cassette":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CassetteInvalid(f"{path}: {error}") from None
        if not isinstance(document, dict) or document.get("cassette_version") != CASSETTE_VERSION:
            raise CassetteInvalid(f"{path}: unsupported cassette_version")
        entries = []
        raw_entries = document.get("entries")
        if not isinstance(raw_entries, list):
            raise CassetteInvalid("entries must be an array")
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise CassetteInvalid(f"{path}: entry is not an object")
            fingerprint = raw.get("fingerprint")
            if not isinstance(fingerprint, dict):
                raise CassetteInvalid(f"{path}: entry has no fingerprint")
            entries.append(Entry(str(raw.get("kind", "response")), fingerprint,
                                 raw.get("response"), str(raw.get("status", "completed"))))
        if not entries:
            raise CassetteInvalid(f"{path}: no entries")
        return cls(tuple(entries), tuple(str(item) for item in document.get("warnings") or ()))

    def to_json(self) -> dict:
        return {"cassette_version": CASSETTE_VERSION,
                "entries": [entry.to_json() for entry in self.entries],
                "warnings": list(self.warnings)}


def fingerprint(request: dict) -> dict:
    """The provider-visible identity of one request, as hashes."""
    instructions, declarations = prompt_material(request)
    model = request.get("model") if isinstance(request.get("model"), dict) else {}
    messages = [_semantic_message(message) for message in request.get("messages") or ()
                if isinstance(message, dict)]
    message_hashes = [sha256_json(message) for message in messages]
    components = {
        "prompt_sha256": sha256_text(instructions),
        "declarations_sha256": sha256_json(declarations),
        "tool_names": [tool.get("name") for tool in declarations],
        "messages": len(messages),
        "message_sha256": message_hashes,
        "model": {"provider": model.get("provider"), "id": model.get("id"), "api": model.get("api"),
                  "max_tokens": model.get("max_tokens"),
                  "endpoint_sha256": sha256_text(model["endpoint"]) if model.get("endpoint") else None},
        "max_tokens": request.get("max_tokens"),
        "temperature": request.get("temperature"),
        # A stored request may omit the default, so absence and "task" describe
        # the same call and must not produce two different fingerprints.
        "purpose": request.get("purpose") or "task",
    }
    return {**components, "sha256": sha256_json(components)}


def _validate_fingerprint(value) -> None:
    """Reject incomplete or inconsistent metadata before any response is reused."""
    fields = {"prompt_sha256", "declarations_sha256", "tool_names", "messages",
              "message_sha256", "model", "max_tokens", "temperature", "purpose", "sha256"}

    def digest(item):
        return isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) is not None

    if not isinstance(value, dict) or set(value) != fields:
        raise CassetteInvalid("incomplete fingerprint fields")
    if any(not digest(value[key]) for key in ("prompt_sha256", "declarations_sha256", "sha256")):
        raise CassetteInvalid("fingerprint hashes must be SHA256 hex digests")
    hashes = value["message_sha256"]
    if (type(value["messages"]) is not int or value["messages"] < 0
            or not isinstance(hashes, list) or len(hashes) != value["messages"]
            or not all(digest(item) for item in hashes)):
        raise CassetteInvalid("message hash count or digest is invalid")
    if not isinstance(value["tool_names"], list) or not all(isinstance(item, str) for item in value["tool_names"]):
        raise CassetteInvalid("tool_names must be an array of strings")
    model = value["model"]
    if not isinstance(model, dict) or set(model) != {"provider", "id", "api", "max_tokens", "endpoint_sha256"}:
        raise CassetteInvalid("incomplete model fingerprint")
    if any(not isinstance(model[key], str) or not model[key] for key in ("provider", "id", "api")):
        raise CassetteInvalid("model identity must contain non-empty strings")
    for number in (value["max_tokens"], model["max_tokens"]):
        if number is not None and (type(number) is not int or number < 0):
            raise CassetteInvalid("token limits must be non-negative integers or null")
    temperature = value["temperature"]
    if temperature is not None and (type(temperature) not in (int, float) or not math.isfinite(temperature)):
        raise CassetteInvalid("temperature must be a finite number or null")
    if value["purpose"] not in ("task", "summary", "memory"):
        raise CassetteInvalid("invalid request purpose")
    if model["endpoint_sha256"] is not None and not digest(model["endpoint_sha256"]):
        raise CassetteInvalid("endpoint fingerprint must be a SHA256 digest")
    components = {key: item for key, item in value.items() if key != "sha256"}
    if value["sha256"] != sha256_json(components):
        raise CassetteInvalid("fingerprint aggregate hash disagrees with its components")


def compare_fingerprints(recorded: dict, current: dict) -> list[dict]:
    """Name every component that drifted, most structural first."""
    _validate_fingerprint(recorded)
    _validate_fingerprint(current)
    drifts = []
    for component in ("prompt_sha256", "declarations_sha256", "tool_names", "model",
                      "max_tokens", "temperature", "purpose", "messages"):
        if recorded.get(component) != current.get(component):
            drifts.append({"component": component, "expected": recorded.get(component),
                           "actual": current.get(component)})
    if recorded.get("messages") == current.get("messages"):
        for index, (want, have) in enumerate(zip(recorded.get("message_sha256") or (),
                                                 current.get("message_sha256") or ())):
            if want != have:
                drifts.append({"component": f"message[{index}]", "expected": want, "actual": have})
                break
    return drifts


def record(rows, *, workspace: Path | None = None) -> Cassette:
    """Build a cassette from stored ``model_calls`` rows, in iteration order."""
    entries = []
    skipped = 0
    truncated = 0
    for row in sorted(rows, key=lambda item: item.get("iteration") or 0):
        response = row.get("response")
        if not isinstance(response, dict):
            skipped += 1
            continue
        body, cut = _redact_response(response, workspace)
        truncated += int(cut)
        status = str(row.get("status") or "completed")
        kind = "response"
        if status == "cancelled" or body.get("stop_reason") == "aborted":
            kind = "cancelled"
        elif status == "failed" or body.get("stop_reason") == "error":
            kind = "stream_error"
        request = row.get("request") if isinstance(row.get("request"), dict) else {}
        entries.append(Entry(kind, fingerprint(request), body, status))
    if not entries:
        raise CassetteInvalid("no recorded responses in this run")
    warnings = [
        "Response text is redacted on the way in; the stored requests themselves are not exported.",
        "Only the final message is stored: replay emits start and the terminator, not text deltas.",
        "A live run that rebuilds history from this file diverges at the second request by design.",
    ]
    if skipped:
        warnings.append(f"{skipped} model call(s) had no stored response and are not in the cassette")
    if truncated:
        warnings.append(f"{truncated} response text block(s) hit the {TEXT_LIMIT}-character bound "
                        "and were cut")
    return Cassette(tuple(entries), tuple(warnings))


class ReplayClient:
    """A ``ModelClient`` that answers from a cassette and refuses to improvise."""

    def __init__(self, cassette: Cassette) -> None:
        self.cassette = cassette
        self.index = 0
        self.messages: list[Message] = []

    async def stream(self, request: ModelRequest):
        if self.index >= len(self.cassette.entries):
            raise CassetteExhausted(
                f"cassette holds {len(self.cassette.entries)} response(s); "
                f"request {self.index + 1} has nothing to replay")
        entry = self.cassette.entries[self.index]
        current = fingerprint(_as_request_document(request))
        drifts = compare_fingerprints(entry.fingerprint, current)
        if drifts:
            raise CassetteDivergence(self.index, drifts)
        self.index += 1
        message = message_from_dict(entry.response or {})
        self.messages.append(message)
        yield StartEvent(partial=message)
        if entry.kind == "stream_error":
            yield ErrorEvent(reason="error", error=message)
        elif entry.kind == "cancelled":
            yield ErrorEvent(reason="aborted", error=message)
        else:
            reason = message.stop_reason
            yield DoneEvent(reason=reason if reason in ("stop", "length", "toolUse") else "stop",
                            message=message)

    @property
    def consumed(self) -> int:
        return self.index


async def verify(cassette: Cassette, requests) -> dict:
    """Replay a recorded request sequence and stop at the first divergence."""
    client = ReplayClient(cassette)
    matched = 0
    terminal: list[dict] = []
    stopped = None
    for index, request in enumerate(requests):
        try:
            async for event in client.stream(request):
                if isinstance(event, DoneEvent):
                    terminal.append({"index": index, "kind": "done", "reason": event.reason,
                                     "message": message_to_dict(event.message)})
                elif isinstance(event, ErrorEvent):
                    terminal.append({"index": index, "kind": "error", "reason": event.reason,
                                     "message": message_to_dict(event.error)})
        except CassetteDivergence as divergence:
            stopped = {"index": index, **divergence.to_json()}
            break
        except CassetteExhausted as error:
            stopped = {"index": index, "drifts": [{"component": "calls",
                                                   "expected": len(cassette.entries),
                                                   "actual": index + 1}],
                       "detail": str(error)}
            break
        matched += 1
    return {
        "cassette_version": CASSETTE_VERSION,
        "entries": len(cassette.entries),
        "requests": len(requests),
        "matched": matched,
        "stopped": stopped,
        "terminal": terminal,
        "ok": stopped is None and matched == len(cassette.entries),
        "warnings": list(cassette.warnings),
    }


def format_verify(document: dict) -> str:
    lines = [f"cassette {document['entries']} entr(ies), {document['requests']} request(s), "
             f"{document['matched']} matched"]
    if document["stopped"]:
        stopped = document["stopped"]
        lines.append(f"stopped at request {stopped['index']}")
        for drift in stopped.get("drifts") or ():
            lines.append(f"  {drift['component']}: {drift['expected']!r} != {drift['actual']!r}")
    for note in document["warnings"]:
        lines.append("note: " + note)
    lines.append("ok" if document["ok"] else "not ok")
    return "\n".join(lines) + "\n"


class CassetteSource:
    """Read-only load of recorded requests and responses for one run."""

    def __init__(self, database) -> None:
        self.database = database

    async def rows(self, run_id) -> list[dict]:
        return await self._read(run_id)

    async def requests(self, run_id) -> list[ModelRequest]:
        rows = await self._read(run_id)
        return [request_from_row(row) for row in rows]

    async def workspace(self, run_id) -> Path | None:
        target = UUID(str(run_id))
        async with self.database.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor = await connection.execute(
                "SELECT w.root_path FROM runs r JOIN sessions s ON s.id=r.session_id "
                "JOIN workspaces w ON w.id=s.workspace_id WHERE r.id=%s", (target,))
            row = await cursor.fetchone()
        return Path(row["root_path"]) if row and row["root_path"] else None

    async def _read(self, run_id) -> list[dict]:
        target = UUID(str(run_id))
        async with self.database.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            await connection.execute("SET LOCAL statement_timeout = '10s'")
            cursor = await connection.execute(
                "SELECT iteration, status, request, response FROM model_calls "
                "WHERE run_id=%s ORDER BY iteration", (target,))
            rows = [dict(row) for row in await cursor.fetchall()]
        if not rows:
            raise LookupError("no model calls recorded for this run")
        return rows


def request_from_row(row: dict) -> ModelRequest:
    """Rebuild the request object a replay client would receive."""
    request = row.get("request") if isinstance(row.get("request"), dict) else {}
    model = request.get("model") if isinstance(request.get("model"), dict) else {}
    return ModelRequest(
        model=Model(id=str(model.get("id") or "unknown"), provider=str(model.get("provider") or ""),
                    api=str(model.get("api") or ""),
                    context_window=int(model.get("context_window") or 0),
                    max_tokens=int(model.get("max_tokens") or 0),
                    endpoint=model.get("endpoint")),
        messages=[message_from_dict(message) for message in request.get("messages") or ()],
        max_tokens=request.get("max_tokens"),
        temperature=request.get("temperature"),
        purpose=request.get("purpose") or "task",
    )


def _as_request_document(request: ModelRequest) -> dict:
    return {
        "model": {"provider": request.model.provider, "id": request.model.id,
                  "api": request.model.api, "max_tokens": request.model.max_tokens,
                  "endpoint": request.model.endpoint},
        "messages": [message_to_dict(message) for message in request.messages],
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "purpose": request.purpose,
    }


def _semantic_message(message: dict) -> dict:
    """What the provider sees: non-null, non-bookkeeping fields.

    ``None`` is dropped as well as the fields above, because a stored request may
    omit a default that ``asdict`` writes back explicitly. Both spellings describe
    the same message to a provider, and treating them as different would make the
    replay diverge on serialisation rather than on behaviour.
    """
    return {key: value for key, value in message.items()
            if key not in NON_SEMANTIC and value is not None}


def _redact_response(response: dict, workspace: Path | None) -> tuple[dict, bool]:
    cut = False

    def redact(value):
        nonlocal cut
        if isinstance(value, str):
            text = safe_text(value, workspace, TEXT_LIMIT)
            cut = cut or text.endswith("…[truncated]")
            return text
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        return value

    body = redact(response)
    return body, cut


__all__ = [
    "CASSETTE_VERSION", "Cassette", "CassetteDivergence", "CassetteExhausted",
    "CassetteInvalid", "CassetteSource", "Entry", "NON_SEMANTIC", "ReplayClient",
    "compare_fingerprints", "fingerprint", "format_verify", "record",
    "request_from_row", "verify",
]
