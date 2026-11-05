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

# A write is not a command, and the refusal has to read that way: nothing was
# executed, the file is untouched, and resending the same content is not the
# answer. Kept next to the command wording so the two cannot drift apart.
WRITE_REFUSALS = {
    "denied": ("The user declined this change, so the file was not modified. This is a decision about "
               "the edit, not a tool failure. Do not resend the same change; ask what should be different."),
    "expired": ("No one approved this change before the deadline, so the file was not modified. Asking "
                "again will ask again; do not slip the same content through a different path."),
    "unwatched": ("No browser page was waiting for this change, so the file was not modified. The user "
                  "was not there to answer, which says nothing about the edit itself; ask whether they "
                  "still want it."),
    "cancelled": "The run ended while this change was waiting for approval; the file was not modified.",
    "unavailable": "No approver is available for this change, so the file was not modified.",
    "stale": ("The approval was no longer valid when the edit was about to start (expired, already "
              "used, or the content changed); the file was not modified."),
}

ApprovalDecider = Callable[[ApprovalRequest], Awaitable[bool | None]]

# Who answered, written into ``note`` for audit. These are not words anyone
# said, so they never reach the model as a remark from the user.
INTERNAL_NOTES = frozenset({"local prompt", "no viewer", "no interactive approver"})


def public_note(note: str | None) -> str:
    """The user's own words about the decision, or nothing.

    A note is also how the broker records *who* answered; that is audit data,
    not something the user said, and the model must not read it as one.
    """
    text = (note or "").strip()
    return "" if text in INTERNAL_NOTES else text


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    approved: bool
    approval_id: UUID | None
    message: str = ""
    reason: str = ""
    # What the user wrote with their decision. It travels with the grant
    # because the model's only record of the exchange is the tool result: a
    # model that is not told the page answered reports that nothing was asked
    # (measured in the C5 acceptance, where every run had a real approval card).
    note: str = ""


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
                      signal: asyncio.Event | None = None, budget=None,
                      subject: str | None = None) -> ApprovalOutcome:
        request = await self.approvals.create(
            run_id=run_id, tool_record_id=tool_record_id, fingerprint=fingerprint(tool_name, arguments, cwd),
            tool_name=tool_name, cwd=cwd, subject=subject or subject_of(tool_name, arguments),
            ttl=self.timeout)
        asked = False
        unwatched_since: float | None = None
        # A human deciding is not the run spending its execution budget, and the
        # wait is already bounded by ``self.timeout``: the run budget's pause
        # ceiling must not cut a correct wait short.
        if budget is not None:
            budget.pause(capped=False)
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
                        note = public_note(current.note)
                        return ApprovalOutcome(True, current.id, note=note)
                    if current.status == "denied":
                        return ApprovalOutcome(False, current.id, DENIED, "denied",
                                               public_note(current.note))
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

    async def request_batch(self, *, run_id: UUID, items: list[dict],
                            signal: asyncio.Event | None = None, budget=None) -> list[ApprovalOutcome]:
        """Ask one question about several writes, and answer it for all of them.

        ``items`` is ``[{tool_record_id, tool_name, arguments, cwd, subject}]``,
        one entry per call the model wants to make in this turn. Each row keeps
        its own fingerprint and is consumed separately; the decision is shared,
        because a page that showed three files and got one answer must not apply
        it to two of them.

        The outcome is per row: a sibling that expired before the decision stays
        refused as expired, which is the only state that can differ inside one
        batch.
        """
        requests = await self.approvals.create_batch(
            run_id=run_id, ttl=self.timeout,
            items=[{**item, "fingerprint": fingerprint(item["tool_name"], item["arguments"], item["cwd"])}
                   for item in items])
        batch_id = requests[0].batch_id
        asked = False
        unwatched_since: float | None = None
        if budget is not None:
            budget.pause(capped=False)
        try:
            with self.runs.notifications.subscribe(run_id) as subscription:
                while True:
                    version = subscription.version
                    rows = await self.approvals.batch(batch_id)
                    if signal is not None and signal.is_set():
                        await self.approvals.cancel_pending(run_id, "run_cancelled")
                        return [ApprovalOutcome(False, row.id, CANCELLED, "cancelled") for row in rows]
                    pending = [row for row in rows if row.status == "pending"]
                    if not pending:
                        return [self._outcome(row) for row in rows]
                    if pending[0].expires_at <= datetime.now(timezone.utc):
                        await self.approvals.expire_due(run_id=run_id)
                        continue
                    if self.remote and self.audience is not None and self.grace > 0:
                        if self.audience(run_id):
                            unwatched_since = None
                        elif unwatched_since is None:
                            unwatched_since = monotonic()
                        elif monotonic() - unwatched_since >= self.grace:
                            if await self.approvals.decide(pending[0].id, approve=False,
                                                           note="no viewer") == "conflict":
                                continue
                            continue
                    if self.decider is not None and not asked:
                        asked = True
                        answer = await self.decider(pending[0])
                        if answer is None and not self.remote:
                            await self.approvals.decide(pending[0].id, approve=False,
                                                        note="no interactive approver")
                            continue
                        if answer is not None:
                            await self.approvals.decide(pending[0].id, approve=answer, note="local prompt")
                            continue
                    await subscription.wait(version, timeout=self.poll)
        finally:
            if budget is not None:
                budget.resume()

    @staticmethod
    def _outcome(row: ApprovalRequest) -> ApprovalOutcome:
        note = public_note(row.note)
        if row.status == "approved":
            return ApprovalOutcome(True, row.id, note=note)
        if row.status == "denied":
            return ApprovalOutcome(False, row.id, DENIED, "denied", note)
        if row.status == "expired":
            return ApprovalOutcome(False, row.id, EXPIRED, "expired")
        if row.status == "cancelled":
            return ApprovalOutcome(False, row.id, CANCELLED, "cancelled")
        return ApprovalOutcome(False, row.id, UNAVAILABLE, "unavailable")

    async def cancel(self, run_id: UUID, note: str = "run_ended") -> int:
        return await self.approvals.cancel_pending(run_id, note)

    async def consume(self, approval_id: UUID, *, run_id: UUID, tool_record_id: UUID, tool_name: str,
                      arguments: dict, cwd: str) -> bool:
        return await self.approvals.consume(
            approval_id, run_id=run_id, tool_record_id=tool_record_id,
            fingerprint=fingerprint(tool_name, arguments, cwd))

