"""Ask a human before a host command runs, without holding execution resources.

The wait happens in the scheduler *before* it takes a concurrency slot, the
workspace gate or a database connection, which is what makes an unanswered
question harmless: another session keeps working, the heartbeat keeps renewing
the lease, and cancellation still lands. Each poll opens a short connection and
closes it again.

The database decides. A local prompt (CLI) and a remote decision (web) are both
written through the repository's conditional update, so the two can race without
the broker preferring one of them.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from uuid import UUID

from chagent.storage.approvals import ApprovalRepository, ApprovalRequest
from chagent.storage.repositories import RunRepository
from chagent.tools.policy import fingerprint, subject as subject_of


POLL_SECONDS = 0.25

# A refusal is a policy decision, not a failure. Saying so is the difference
# between a model that explains the block and one that retries with new flags.
DENIED = ("The host denied this command; it was not executed. This is a policy decision, not a "
          "command failure. Do not retry it with different arguments; ask the user how to proceed.")
EXPIRED = ("No one approved this command before the deadline, so it was not executed. Asking again "
           "will ask again; do not silently change the command.")
CANCELLED = "The run ended while this command was waiting for approval; it was not executed."
UNAVAILABLE = "No approver is available for this command, so it was not executed."
# Nobody is looking. This is deliberately not worded as a refusal of the
# command: the command was never judged, so retrying it with new flags is the
# wrong lesson. It is also not "expired", because the deadline did not pass.
UNWATCHED = ("No browser page was waiting for this command, so it was not executed. This is not a "
             "decision about the command itself: the user was not there to answer. Do not retry it "
             "with different arguments; ask the user whether they still want it run.")
STALE = ("The approval was no longer valid when the tool was about to start (expired, already "
         "used, or the invocation changed); it was not executed.")

ApprovalDecider = Callable[[ApprovalRequest], Awaitable[bool | None]]


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    approved: bool
    approval_id: UUID | None
    message: str = ""
    reason: str = ""


class ApprovalBroker:
    def __init__(self, approvals: ApprovalRepository, runs: RunRepository, *, timeout: float,
                 decider: ApprovalDecider | None = None, remote: bool = True,
                 poll: float = POLL_SECONDS, grace: float = 10.0,
                 audience: Callable[[UUID], bool] | None = None):
        if timeout <= 0:
            raise ValueError("Approval timeout must be positive")
        if grace < 0:
            raise ValueError("Unwatched grace must not be negative")
        self.approvals, self.runs = approvals, runs
        self.timeout, self.poll, self.grace = timeout, poll, grace
        self.decider, self.remote = decider, remote
        #: True when someone is attached to this run right now. Only consulted
        #: for remote questions: a terminal prompt has no page to close.
        self.audience = audience

    async def request(self, *, run_id: UUID, tool_record_id: UUID, tool_name: str,
                      arguments: dict, cwd: str,
                      signal: asyncio.Event | None = None, budget=None) -> ApprovalOutcome:
        request = await self.approvals.create(
            run_id=run_id, tool_record_id=tool_record_id, fingerprint=fingerprint(tool_name, arguments, cwd),
            tool_name=tool_name, cwd=cwd, subject=subject_of(tool_name, arguments), ttl=self.timeout)
        asked = False
        unwatched_since: float | None = None
        # A human deciding is not the run spending its execution budget.
        if budget is not None:
            budget.pause()
        try:
            with self.runs.notifications.subscribe(run_id) as subscription:
                while True:
                    version = subscription.version
                    if signal is not None and signal.is_set():
                        await self.approvals.cancel_pending(run_id, "run_cancelled")
                        return ApprovalOutcome(False, request.id, CANCELLED, "cancelled")
                    current = await self.approvals.get(request.id)
                    if current is None:
                        return ApprovalOutcome(False, None, UNAVAILABLE, "unavailable")
                    if current.status == "approved":
                        return ApprovalOutcome(True, current.id)
                    if current.status == "denied":
                        return ApprovalOutcome(False, current.id, DENIED, "denied")
                    if current.status == "expired":
                        return ApprovalOutcome(False, current.id, EXPIRED, "expired")
                    if current.status == "cancelled":
                        return ApprovalOutcome(False, current.id, CANCELLED, "cancelled")
                    if current.expires_at <= datetime.now(timezone.utc):
                        # The deadline is ours to enforce: a pending row past it
                        # must become expired before anyone can still approve it.
                        await self.approvals.expire_due(run_id=run_id)
                        continue
                    if self.remote and self.audience is not None and self.grace > 0:
                        # A page that reloads reconnects in a second or two, so
                        # "nobody is watching" has to persist before it counts.
                        if self.audience(run_id):
                            unwatched_since = None
                        elif unwatched_since is None:
                            unwatched_since = monotonic()
                        elif monotonic() - unwatched_since >= self.grace:
                            # The decision is a conditional update: if someone
                            # answered in the same instant, theirs wins and the
                            # loop returns that answer instead.
                            if await self.approvals.decide(current.id, approve=False, note="no viewer") == "conflict":
                                continue
                            return ApprovalOutcome(False, current.id, UNWATCHED, "unwatched")
                    if self.decider is not None and not asked:
                        asked = True
                        answer = await self.decider(current)
                        if answer is None and not self.remote:
                            # Non-interactive command line: nobody can answer.
                            await self.approvals.decide(current.id, approve=False,
                                                        note="no interactive approver")
                            continue
                        if answer is not None:
                            await self.approvals.decide(current.id, approve=answer, note="local prompt")
                            continue
                    await subscription.wait(version, timeout=self.poll)
        finally:
            if budget is not None:
                budget.resume()

    async def cancel(self, run_id: UUID, note: str = "run_ended") -> int:
        return await self.approvals.cancel_pending(run_id, note)

    async def consume(self, approval_id: UUID, *, run_id: UUID, tool_record_id: UUID, tool_name: str,
                      arguments: dict, cwd: str) -> bool:
        return await self.approvals.consume(
            approval_id, run_id=run_id, tool_record_id=tool_record_id,
            fingerprint=fingerprint(tool_name, arguments, cwd))

