"""One-line summaries of what a run will actually do, for startup output.

Ambient variables beat ``--env-file`` entries, so the resolved model pair and the
behavioural switches are printed before a run starts. A stale export then shows
up as a wrong line instead of as a mystery later.
"""

from chagent.config import Settings
from chagent.domain.summaries import CompactionPolicy


def describe(settings: Settings, compaction: CompactionPolicy | None = None,
             tokenizer: str | None = None) -> str:
    policy = compaction if compaction is not None else CompactionPolicy.from_env()
    sandbox = settings.sandbox
    if sandbox != "off":
        sandbox += "/network" if settings.sandbox_network else "/no-network"
    line = (f"sandbox={sandbox}"
            f" host_commands={'on' if settings.allow_host_commands else 'off'}"
            f" compaction={'on' if policy.enabled else 'off'}"
            f" max_turns={settings.max_turns}")
    # Which counter is authoritative decides when compaction triggers, so it is
    # part of the behaviour, not an implementation detail.
    return line + (f" tokenizer={tokenizer}" if tokenizer else "")
