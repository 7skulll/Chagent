"""Keep a disposable test schema after a failure so its rows can still be audited.

A live-chain run creates a throwaway schema and drops it on the way out. That is
right for a passing run and wrong for a failing one: the recorded requests and
responses are the only evidence for why it failed, and they disappear with the
schema. ``--keep-schema DIR`` writes the schema name and a restore script next
to the report instead, so the post-mortem does not need a rerun.

The artifacts never contain credentials: commands read the connection string
from the environment.
"""

from datetime import datetime, timezone
import json
from pathlib import Path


def drop_command(schema: str) -> str:
    return ("psql \"${CHAGENT_DATABASE_URL:-postgresql:///chagent}\" -c "
            f"'DROP SCHEMA IF EXISTS \"{schema}\" CASCADE'")


def keep_artifacts(directory, *, schema: str, report_path, failures: list[dict],
                   created_at: datetime | None = None) -> list[Path]:
    """Write ``keep-<schema>.json`` and ``restore-<schema>.sh``; return both paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (created_at or datetime.now(timezone.utc)).isoformat()
    record = {
        "schema": schema,
        "created_at_utc": stamp,
        "report": str(report_path),
        "failures": failures,
        "audit": f"PYTHONPATH=src python scripts/report_prompt_cache.py --schema {schema}",
        "drop": drop_command(schema),
    }
    data = directory / f"keep-{schema}.json"
    script = directory / f"restore-{schema}.sh"
    data.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    script.write_text(
        "#!/bin/sh\n"
        "# Written by scripts/test_live_chain.py --keep-schema after a failed run.\n"
        "# Restores the pre-run state by dropping the schema kept for inspection.\n"
        "# The report and the failure list are in keep-<schema>.json next to this file.\n"
        "set -eu\n"
        f"{record['drop']}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return [data, script]
