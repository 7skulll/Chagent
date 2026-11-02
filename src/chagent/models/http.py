"""Bounded SSE transport and credential-safe provider failure reporting."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
from time import monotonic_ns, time_ns

import httpx

from chagent.domain.messages import AssistantMessage
from chagent.models.base import ModelRequest
from chagent.models.events import AssistantMessageEvent, ErrorEvent, StartEvent
from chagent.models.settings import ModelSettings


class ModelProtocolError(ValueError):
    """A provider failure, with what the retry loop observed.

    ``details`` exists so the surfaced reason and any audit record describe the
    same facts (how many attempts, which status) instead of re-parsing text.
    """

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details: dict = details or {}


def retryable_status(status: int) -> bool:
    """Only these are worth repeating.

    A 429 is a transient throttle and a 5xx is the provider's own problem; both
    may answer differently a second later. A 4xx answers the same forever, so
    retrying it would only delay the diagnosis.
    """
    return status == 429 or 500 <= status <= 599


# Backoff grows geometrically from the configured first delay (0.5s, 1.5s, 4.5s).
RETRY_BACKOFF_FACTOR = 3.0


@dataclass(slots=True)
class SSEEvent:
    event: str
    data: str


async def sse_events(response: httpx.Response) -> AsyncIterator[SSEEvent]:
    data = []
    event = "message"
    event_size = total = 0
    # aiter_lines alone can buffer an unbounded single line, so enforce limits
    # before decoding and splitting lines.
    pending = b""
    first_line = True
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > 8_388_608:
            raise ModelProtocolError("Provider stream exceeds the 8 MiB limit")
        pending += chunk
        while b"\n" in pending:
            raw, pending = pending.split(b"\n", 1)
            line = raw.rstrip(b"\r").decode("utf-8")
            if first_line:
                line = line.removeprefix("\ufeff")
                first_line = False
            event_size += len(raw)
            if event_size > 1_048_576:
                raise ModelProtocolError("Provider SSE event exceeds the 1 MiB limit")
            if not line:
                if data:
                    yield SSEEvent(event, "\n".join(data))
                data, event, event_size = [], "message", 0
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif line.startswith("event:"):
                event = line[6:].strip()
        if len(pending) > 1_048_576:
            raise ModelProtocolError("Provider stream line exceeds the 1 MiB limit")
    if pending.strip() or data:
        raise ModelProtocolError("Provider stream ended inside an SSE event")


def event_object(event: SSEEvent) -> dict:
    value = json.loads(event.data)
    if not isinstance(value, dict):
        raise ModelProtocolError("Expected an object in provider stream")
    if value.get("error") is not None or value.get("type") == "error":
        raise ModelProtocolError("Provider reported a streaming error")
    return value


def finalize_arguments(raw: str) -> tuple[dict, str | None]:
    try:
        def reject_constant(value):
            raise ValueError("Non-finite JSON constant")
        arguments = json.loads(raw, parse_constant=reject_constant)
        if isinstance(arguments, dict):
            return arguments, None
    except (ValueError, TypeError):
        pass
    return {}, "Tool arguments are not a complete JSON object; reissue the call"


class HTTPModelClient:
    def __init__(self, settings: ModelSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object):
        await self.aclose()

    def payload(self, request: ModelRequest) -> dict:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    @property
    def endpoint(self) -> str:
        raise NotImplementedError

    async def decode(self, response: httpx.Response, partial: AssistantMessage) -> AsyncIterator[AssistantMessageEvent]:
        raise NotImplementedError
        yield  # pragma: no cover

    def _failure(self, response: httpx.Response, retries: int = 0) -> ModelProtocolError:
        hints = {401: "API key rejected", 403: "Access denied", 404: "Model or endpoint not found",
                 429: "Rate limit or quota exceeded"}
        status = response.status_code
        reason = f"HTTP {status}: {hints.get(status, 'Provider request failed')}"
        if retries:
            reason += f" (after {retries} {'retry' if retries == 1 else 'retries'})"
        # The body is never read into the reason: provider error payloads can
        # echo credentials or request content.
        return ModelProtocolError(reason, details={"status": status, "retries": retries,
                                                   "attempts": retries + 1})

    async def stream(self, request: ModelRequest) -> AsyncIterator[AssistantMessageEvent]:
        started = monotonic_ns()
        partial = AssistantMessage([], request.model.api, request.model.provider, request.model.id,
                                   time_ns() // 1_000_000, "pending")
        try:
            if request.model.provider != self.settings.provider or request.model.api != self.settings.api:
                raise ModelProtocolError("Request provider does not match configured credential")
            if request.model.endpoint is not None and request.model.endpoint != self.settings.base_url:
                raise ModelProtocolError("Request endpoint does not match configured credential")
            payload = self.payload(request)
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(self.settings.request_timeout),
                    transport=self.transport, follow_redirects=False,
                )
            retries = 0
            while True:
                async with self._client.stream("POST", self.endpoint, headers=self.headers(), json=payload) as response:
                    if not 200 <= response.status_code < 300:
                        # Nothing has been yielded yet, so repeating the request
                        # cannot duplicate a partial answer.
                        if retryable_status(response.status_code) and retries < self.settings.retry_attempts:
                            retries += 1
                            await asyncio.sleep(self.settings.retry_delay * RETRY_BACKOFF_FACTOR ** (retries - 1))
                            continue
                        raise self._failure(response, retries)
                    if "text/event-stream" not in response.headers.get("content-type", "").lower():
                        raise ModelProtocolError("Expected a server-sent event response")
                    yield StartEvent(partial)
                    async for event in self.decode(response, partial):
                        if event.type in {"done", "error"}:
                            partial.duration_ms = (monotonic_ns() - started) // 1_000_000
                        yield event
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            partial.stop_reason = "error"
            if isinstance(error, ModelProtocolError):
                reason = str(error)
            elif isinstance(error, httpx.TimeoutException):
                reason = "Provider request timed out"
            elif isinstance(error, httpx.RequestError):
                reason = "Provider network request failed"
            else:
                reason = f"Invalid provider response ({type(error).__name__})"
            partial.error_message = f"{self.settings.provider}: {reason}".replace(self.settings.api_key, "[redacted]")
            partial.duration_ms = (monotonic_ns() - started) // 1_000_000
            yield ErrorEvent(reason="error", error=partial)
