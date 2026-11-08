"""Whether a run that changed files ever checked its own result.

Measured failure: a run built a data dashboard, validated all 45 quotes and two
of the 45 history series, and ended normally. The one wrong series was in the set
it never checked, and nothing in the harness noticed — "the model stopped" was
the only condition for completing the run.

This module is deliberately narrow. It does not judge whether a check is good,
and it cannot know what "correct" means for a given task. It answers two
questions about the committed transcript only:

* did this run successfully rewrite part of the workspace?
* did any tool that *checks* the result succeed afterwards?

The runner turns "changed but never checked" into one extra turn, once per run.
That is a nudge, not a gate: a task with nothing checkable still ends normally,
and the model is asked to say what it did not verify rather than to invent a
result.
"""

from chagent.domain.messages import ToolResultMessage
from chagent.domain.runs import StoredMessage


#: Tools whose success means part of the workspace was rewritten. Shell writes
#: are deliberately absent: ``exec_command`` also runs ``ls``, so counting it
#: would fire this on every command-using run and train the reader to ignore it.
MUTATING_TOOLS = frozenset({
    "write_file", "edit_file", "apply_patch", "create_directory", "create_file",
})

#: Tools whose success is evidence *about* the result. ``site`` only counts a
#: probe answering 200; starting a website proves nothing about its content.
VERIFYING_TOOLS = frozenset({"run_tests", "site"})

#: Spoken as a trailing message, never persisted: it describes one scheduling
#: decision, and a replayed transcript carrying it would assert something false.
NUDGE = (
    "Harness note, not a new request. This run changed workspace files and no verification step "
    "succeeded: no run_tests with exit status 0, no site(probe) answering 200. Before you finish, "
    "run the smallest check that would fail if your change were wrong, and report its actual "
    "result. If nothing here can be checked, say plainly what you did not verify instead of "
    "implying it was verified."
)


def _results(history: list[StoredMessage]):
    for item in history:
        message = item.message
        if isinstance(message, ToolResultMessage):
            yield message


def mutations(history: list[StoredMessage]) -> int:
    """How many successful workspace mutations this run committed."""
    return sum(1 for message in _results(history)
               if message.tool_name in MUTATING_TOOLS and not message.is_error)


def verified(history: list[StoredMessage]) -> bool:
    """True when some check of the produced result actually succeeded."""
    for message in _results(history):
        if message.is_error or message.tool_name not in VERIFYING_TOOLS:
            continue
        if message.tool_name == "run_tests":
            return True
        # ``site`` reports a probe's status code; start/stop/logs have none.
        details = message.details if isinstance(message.details, dict) else {}
        if details.get("status_code") == 200:
            return True
    return False
