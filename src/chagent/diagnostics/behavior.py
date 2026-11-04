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
    sandbox = settings.sandbox
    if sandbox != "off" and not settings.sandbox_writable:
        sandbox += "(ro)"
    if settings.sandbox != "off":
        sandbox += "/network" if settings.sandbox_network else "/no-network"
    # A browser question with nobody watching has its own deadline; 0 means the
    # absolute approval timeout is the only limit.
    grace = f"{settings.approval_unwatched_grace:g}s" if settings.approval_unwatched_grace else "off"
    line = (f"sandbox={sandbox}"
            f" approval={settings.approval}/unwatched={grace}"
            f" execution_profile={settings.execution_profile}"
            f" programmable={'on' if settings.trusted_development and settings.code_mode else 'off'}"
            f" legacy_code_switch={'on' if settings.code_mode else 'off'}"
            f" host_commands={'on' if settings.allow_host_commands else 'off'}"
            # Reads exist in every face; editing the user's files is the
            # capability an entry point may withhold, so name it explicitly.
            f" writes={'on' if workspace_writes else 'off'}"
            # The one path that leaves the machine is worth naming on the same
            # line as the one that confines it.
            f" web={settings.web_access}"
            f" compaction={'on' if policy.enabled else 'off'}"
            # A session process is the one path that outlives its tool call, so
            # its two limits belong on this line too.
            f" processes={settings.max_processes}/{settings.process_timeout:g}s"
            f"{' process_stdin=off' if settings.readonly_code_available or settings.trusted_development else ''}"
            f" max_turns={settings.max_turns}")
    # Which counter is authoritative decides when compaction triggers, so it is
    # part of the behaviour, not an implementation detail.
    return line + (f" tokenizer={tokenizer}" if tokenizer else "")


def describe_warnings(settings: Settings) -> list[str]:
    """Explain settings that make the unified web entry reject startup."""
    if settings.code_mode:
        return []
    return ["CHAGENT_CODE_MODE=off disables unified web startup; set it to on or restore the old build."]
