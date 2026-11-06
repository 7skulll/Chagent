"""Which failures may be tried again, and what a retry actually means.

The model's only record of a gated call is its tool result, so a failure that is
not named is a failure it will guess about. Measured: without a name, a denied
write gets retried with different arguments and an unknown outcome gets retried
as if nothing had happened.

Five kinds cover the ways a call can end without a result, plus three approval
edge cases. ``retryable`` answers one question — "would running this again be a
reasonable next step?" — not "is this the model's fault":

==================  =========  ==================================================
kind                retryable  why
==================  =========  ==================================================
denied              no         a person decided against this change
unwatched           no         nobody was there; ask, do not re-ask the same thing
expired             yes        a new attempt opens a new question
cancelled           no         the run ended
unavailable         no         no approver exists in this configuration
stale               no         the ticket no longer matched the invocation
execution_failed    yes        the tool ran and said what went wrong
outcome_unknown     no         it may already have happened; inspect first
==================  =========  ==================================================

Distinct from ``tools/progress.py``: that counts consecutive identical calls to
stop a loop. This classifies one finished call, and the number it travels with
(``attempt``) is the audit count of the same invocation in the same run.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Failure:
    code: str
    retryable: bool
    #: One sentence for the model. Only used where the content itself has to say
    #: it; elsewhere the code and the flag ride in details.
    advice: str


FAILURES: dict[str, Failure] = {
    "denied": Failure("denied", False,
                      "This was a decision about the request itself; asking again with the same "
                      "intent will be refused again."),
    "unwatched": Failure("unwatched", False,
                         "Nobody was looking at the page, so this was never judged. Ask the user "
                         "whether they still want it."),
    "expired": Failure("expired", True,
                       "The question timed out. A new attempt asks a new question and cannot reuse "
                       "the expired one."),
    "cancelled": Failure("cancelled", False, "The run ended before an answer; nothing was executed."),
    "unavailable": Failure("unavailable", False,
                           "No approver is configured here, so the request cannot be answered."),
    "stale": Failure("stale", False,
                     "The approval was no longer valid for this invocation when it was about to run."),
    "execution_failed": Failure("execution_failed", True,
                                "The tool ran and reported an error; read it and change the input."),
    "outcome_unknown": Failure("outcome_unknown", False,
                               "The call started and its outcome could not be confirmed."),
}

#: Spoken in the result content, because the model must not skim past it. A
#: blind retry here can duplicate a side effect that already happened.
UNKNOWN_OUTCOME_NOTE = (
    "Result unknown: this call started but its outcome could not be confirmed. The effect may "
    "already have happened. Inspect the file or process before deciding; do not retry this call "
    "as if nothing happened."
)


def of(code: str | None) -> Failure | None:
    return FAILURES.get(code) if code else None


def annotate(details: dict | None, code: str, attempt: int | None = None) -> dict:
    """The fields every finished-without-approval result carries about itself."""
    failure = of(code)
    merged = dict(details) if isinstance(details, dict) else {}
    merged["failure_class"] = code
    if failure is not None:
        merged["retryable"] = failure.retryable
    if attempt is not None:
        merged["attempt"] = attempt
    return merged
