"""Show the resolved configuration without contacting a provider.

The startup ``Resolved:`` line names the model pair. This expands it to every
behavioural switch that changes what a run may do, plus the one case that used
to be invisible: a value in the shell that wins over ``.env``.

``.env`` is used as a default layer and the process environment still wins,
which is what ``uv run --env-file`` does in practice. Every case where the
environment wins is listed as shadowed, so an export that is not in the file
cannot silently change behaviour.
"""

import argparse
import json
import os
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from chagent.config import Settings
from chagent.config import dotenv
from chagent.context.tokens import select_counter
from chagent.diagnostics.behavior import describe as describe_behavior
from chagent.domain.summaries import CompactionPolicy
from chagent.models.settings import ModelConfigurationError, ModelSettings


def collect(env: dict[str, str], file_env: dict[str, str], env_file: Path) -> tuple[dict, list[str]]:
    """Resolved values by section; problems are reported instead of raised."""
    sections: dict[str, object] = {}
    problems = []
    try:
        settings = Settings.from_env(env)
    except ValueError as error:
        problems.append(f"settings: {error}")
        settings = None
    try:
        compaction = CompactionPolicy.from_env(env)
    except ValueError as error:
        problems.append(f"compaction: {error}")
        compaction = None
    try:
        model = ModelSettings.from_env(env)
        sections["model"] = model.describe()
        sections["budgets"] = (f"context_window={model.context_window} max_tokens={model.max_tokens} "
                               f"request_timeout={model.request_timeout:g} run_timeout={model.run_timeout:g} "
                               f"retries={model.retry_attempts}")
        # Which counter is in effect decides when compaction triggers; a fallback
        # here is invisible in every other output.
        sections["tokenizer"] = select_counter(model.model(), env).name
    except ModelConfigurationError as error:
        problems.append(f"model: {error}")
    if settings is not None:
        sections["behavior"] = (describe_behavior(settings, compaction) if compaction is not None
                               else "unavailable")
        sections["execution"] = (f"profile={settings.execution_profile} {settings.axes.describe()} "
                                 f"programmable={str(settings.axes.programmable).lower()} "
                                 f"legacy_code_switch={str(settings.code_mode).lower()}")
        sections["sandbox"] = (f"network={str(settings.sandbox_network).lower()} "
                               f"workspace_writable={str(settings.sandbox_writable).lower()} "
                               f"container_declared={str(settings.container_declared).lower()}")
        sections["memory"] = f"worker_concurrency={settings.memory_worker_concurrency}"
        sections["run_wall"] = f"timeout={settings.run_wall_timeout:g}s (includes approvals and tools)"
        sections["processes"] = (f"max_per_run={settings.max_processes} "
                                 f"wall_timeout={settings.process_timeout:g}s")
        # The key itself never appears here; only whether one is present, and
        # which variable supplied it — the fallback is easy to forget otherwise.
        key = "missing"
        if settings.search_api_key.strip():
            source = settings.search_api_key_source
            key = f"set ({source})" if source else "set"
        sections["web"] = f"provider={settings.web_access} key={key}"
        sections["database"] = (f"dbname={conninfo_to_dict(settings.database_url).get('dbname')} "
                               f"pool={settings.db_pool_min_size}..{settings.db_pool_max_size} "
                               f"timeout={settings.db_timeout:g}s")
    if compaction is not None:
        sections["compaction"] = (f"enabled={str(compaction.enabled).lower()} trigger={compaction.trigger_ratio:g} "
                                  f"keep_recent={compaction.keep_recent_ratio:g} "
                                  f"summary_tokens={compaction.max_summary_tokens} "
                                  f"max_calls={compaction.max_calls} timeout={compaction.timeout:g} "
                                  f"max_per_run={compaction.max_compactions_per_run}")
    sections["env_file"] = (f"{env_file.resolve()} ({len(file_env)} keys)" if file_env
                            else f"none ({env_file})")
    sections["shadowed"] = dotenv.describe(dotenv.shadowed(env, file_env))
    return sections, problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("show",), default="show")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--json", action="store_true", help="Print the resolved values as JSON")
    args = parser.parse_args()
    process_env = dict(os.environ)
    file_env = dotenv.read(args.env_file) if args.env_file.exists() else {}
    # File values are defaults; the environment still wins, as uv does it.
    env = {**file_env, **process_env}
    sections, problems = collect(env, file_env, args.env_file)
    if args.json:
        print(json.dumps({**sections, "problems": problems}, ensure_ascii=False, indent=2))
    else:
        for name, value in sections.items():
            if name == "shadowed":
                lines = value or ["none"]
                for index, line in enumerate(lines):
                    label = "shadowed" if index == 0 else ""
                    print(f"{label:<10}: {line}")
                continue
            print(f"{name:<10}: {value}")
        for problem in problems:
            print(f"{'problem':<10}: {problem}")
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
