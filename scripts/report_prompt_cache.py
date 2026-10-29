"""Score prompt-cache reuse per run from the recorded provider payloads.

Providers cache a byte-identical prefix. This script answers three questions
for any run, without calling a provider:

1. how much of each prompt the provider served from cache (``hit``);
2. how much of it was *reusable*: the overlap with the previous request;
3. if a request could not reuse its predecessor, which section broke it.

The computation lives in ``chagent.diagnostics.cache`` so the live-chain script
reports the same numbers without duplicating the rules. Sections are compared in
prompt order (system text, tool declarations, then messages one by one), because
that is the order a prefix cache sees.

Usage:
    PYTHONPATH=src python scripts/report_prompt_cache.py [--run PREFIX | --latest N]
    PYTHONPATH=src python scripts/report_prompt_cache.py --schema chagent_live_ab12

``--schema`` reads a schema kept by ``test_live_chain.py --keep-schema``.
"""

import argparse
import json
import os
import re

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from chagent.diagnostics.cache import audit

DEFAULT_CONNINFO = os.environ.get("CHAGENT_DATABASE_URL", "postgresql:///chagent")
SCHEMA_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


def connect_info(conninfo: str, schema: str | None):
    if schema is None:
        return conninfo
    if not SCHEMA_PATTERN.match(schema):
        raise SystemExit(f"invalid schema name: {schema!r}")
    return make_conninfo(conninfo, options=f"-c search_path={schema}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", default=[], help="run id prefix (repeatable)")
    parser.add_argument("--latest", type=int, default=0, help="audit the N most recent runs")
    parser.add_argument("--schema", help="read a schema kept by test_live_chain.py --keep-schema")
    parser.add_argument("--conninfo", default=DEFAULT_CONNINFO, help="connection info (default CHAGENT_DATABASE_URL or postgresql:///chagent)")
    parser.add_argument("--output", help="write the full report as JSON")
    arguments = parser.parse_args()

    try:
        with psycopg.connect(connect_info(arguments.conninfo, arguments.schema), row_factory=dict_row) as connection:
            if arguments.run:
                filters, params = [], []
                for prefix in arguments.run:
                    filters.append("r.id::text LIKE %s")
                    params.append(prefix + "%")
                runs = connection.execute(
                    "SELECT id FROM runs r WHERE " + " OR ".join(filters) + " ORDER BY r.started_at",
                    params,
                ).fetchall()
            else:
                runs = connection.execute(
                    "SELECT id FROM runs r ORDER BY r.started_at DESC LIMIT %s",
                    (arguments.latest or 5,),
                ).fetchall()
            report = []
            for run in runs:
                rows = connection.execute(
                    "SELECT m.iteration, m.request, m.response FROM model_calls m "
                    "WHERE m.run_id = %s AND m.response IS NOT NULL ORDER BY m.iteration",
                    (run["id"],),
                ).fetchall()
                if not rows:
                    continue
                summary = audit(rows)
                summary["run_id"] = str(run["id"])
                report.append(summary)
    except psycopg.errors.UndefinedTable:
        raise SystemExit(f"schema {arguments.schema!r} has no runs table: mistyped name, or already restored?") from None
    except psycopg.Error as error:
        # Never echo conninfo back: it can carry a password.
        raise SystemExit(f"audit failed: {type(error).__name__}") from None

    for entry in report:
        hit_total = "—" if entry["hit"] is None else f"{entry['hit']:.1%}"
        reusable = "—" if entry["reusable"] is None else f"{entry['reusable']:.1%}"
        print(f"run {entry['run_id'][:8]}  turns={len(entry['turns'])}  "
              f"prompt={entry['prompt_tokens']}  cached={entry['cached_tokens']}  "
              f"hit={hit_total}  reusable={reusable}  breaks={len(entry['breaks'])}")
        for turn in entry["turns"]:
            share = "—" if turn["reusable"] is None else f"{turn['reusable']:.1%}"
            hit = "—" if turn["cache_hit"] is None else f"{turn['cache_hit']:.1%}"
            print(f"   it={turn['iteration']:<2} msgs={turn['messages']:<3} prompt={turn['prompt_tokens']:<6} "
                  f"cached={turn['cached_tokens']:<6} hit={hit:<7} reusable={share:<7}"
                  + (f" break at {turn['break']}" if turn["break"] else ""))
        print()

    prompt = sum(entry["prompt_tokens"] for entry in report)
    cached = sum(entry["cached_tokens"] for entry in report)
    shared = sum(entry["shared_bytes"] for entry in report)
    total = sum(entry["total_bytes"] for entry in report)
    clean = sum(1 for entry in report if not entry["breaks"])
    if not prompt:
        print("no calls recorded")
        return
    print(f"total: {len(report)} runs, prompt={prompt}, cached={cached}, hit={cached / prompt:.1%}"
          + (f", reusable={shared / total:.1%}" if total else ""))
    print(f"runs whose every turn reused the previous prefix: {clean}/{len(report)}")
    for entry in report:
        for broken in entry["breaks"]:
            print(f"break: run {entry['run_id'][:8]} iteration {broken['iteration']} "
                  f"section {broken['section']} ({broken['lost_bytes']} bytes not reusable)")
    if arguments.output:
        with open(arguments.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
        print(f"written: {arguments.output}")


if __name__ == "__main__":
    main()
