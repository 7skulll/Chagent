"""One-line summaries of what a run will actually do, for startup output.

Ambient variables beat ``--env-file`` entries, so the resolved model pair and the
behavioural switches are printed before a run starts. A stale export then shows
up as a wrong line instead of as a mystery later.
"""

from chagent.config import Settings
from chagent.domain.summaries import CompactionPolicy


def describe(settings: Settings, compaction: CompactionPolicy | None = None,
             tokenizer: str | None = None, workspace_writes: bool = True) -> str:
    policy = compaction if compaction is not None else CompactionPolicy.from_env()
    axes = settings.axes
    # A browser question with nobody watching has its own deadline; 0 means the
    # absolute approval timeout is the only limit.
    grace = f"{settings.approval_unwatched_grace:g}s" if settings.approval_unwatched_grace else "off"
    line = (f"{axes.describe()} approval={settings.approval}/unwatched={grace}"
            f" programmable={'on' if axes.programmable else 'off'}"
            f" legacy_code_switch={'on' if settings.code_mode else 'off'}"
            f" host_commands={'on' if settings.allow_host_commands else 'off'}"
            # Reads exist in every face; editing the user's files is the
            # capability an entry point may withhold, so name it explicitly.
            f" writes={'on' if workspace_writes else 'off'}"
            # The one path that leaves the machine is worth naming on the same
            # line as the one that confines it.
            f" web={settings.web_access}"
            f" compaction={'on' if policy.enabled else 'off'}/{policy.max_compactions_per_run}per-run"
            # The completion condition is behaviour too: a run that changed files
            # without checking them ends differently under each value.
            f" verify={settings.require_verification}"
            # A session process is the one path that outlives its tool call, so
            # its two limits belong on this line too.
            f" processes={settings.max_processes}/{settings.process_timeout:g}s"
            f"{' process_stdin=on' if axes.programmable else ''}"
            f" max_turns={settings.max_turns}")
    # Which counter is authoritative decides when compaction triggers, so it is
    # part of the behaviour, not an implementation detail.
    return line + (f" tokenizer={tokenizer}" if tokenizer else "")


def describe_warnings(settings: Settings) -> list[str]:
    """Explain settings that make the unified web entry reject startup."""
    if settings.code_mode:
        return []
    return ["CHAGENT_CODE_MODE=off disables unified web startup; set it to on or restore the old build."]
