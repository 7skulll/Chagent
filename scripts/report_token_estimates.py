"""Compare local token estimates with the provider's reported prompt tokens.

The byte estimate is not a tokenizer: it is roughly 4x high on English, which
means the same task compacts at different points in different languages. This
report measures every available counter against real usage already stored in
``model_calls``, so changing the counter can be checked offline instead of
spending another request.

Prompt tokens are ``usage.input + usage.cache_read``: our adapters report them
separately, and the provider's cache read is still prompt context.

Usage:
    PYTHONPATH=src python scripts/report_token_estimates.py --latest 5
    PYTHONPATH=src python scripts/report_token_estimates.py --schema chagent_live_ab12
"""

import argparse
import json
import os
import re
import statistics

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from chagent.context.tokens import (
    BpeTokenCounter, ByteTokenCounter, TokenizerUnavailable, encoding_for,
)
from chagent.domain.codec import message_from_dict
from chagent.models.base import Model, ModelRequest

DEFAULT_CONNINFO = os.environ.get("CHAGENT_DATABASE_URL", "postgresql:///chagent")
SCHEMA_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


def connect_info(conninfo: str, schema: str | None):
    if schema is None:
        return conninfo
    if not SCHEMA_PATTERN.match(schema):
        raise SystemExit(f"invalid schema name: {schema!r}")
    return make_conninfo(conninfo, options=f"-c search_path={schema}")


def request_of(row) -> ModelRequest:
    payload = row["request"]
    return ModelRequest(
        model=Model(**payload["model"]),
        messages=[message_from_dict(item) for item in payload["messages"]],
        max_tokens=payload.get("max_tokens"), temperature=payload.get("temperature"),
        purpose=payload.get("purpose", "task"),
    )


def deviation(estimate: int, actual: int) -> float | None:
    return (estimate - actual) / actual if actual else None


def build_counters(model_id: str):
    counters = [ByteTokenCounter()]
    try:
        counters.append(BpeTokenCounter(encoding_for(model_id)))
    except TokenizerUnavailable as error:
        print(f"tokenizer unavailable: {error}; reporting bytes only")
    return counters


def collect(arguments) -> list[dict]:
    try:
        with psycopg.connect(connect_info(arguments.conninfo, arguments.schema),
                             row_factory=dict_row) as connection:
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
                if rows:
                    entry = {"run_id": str(run["id"]), "calls": rows}
                    report.append(entry)
            return report
    except psycopg.errors.UndefinedTable:
        raise SystemExit(f"schema {arguments.schema!r} has no runs table") from None
    except psycopg.Error as error:
        raise SystemExit(f"report failed: {type(error).__name__}") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", default=[], help="run id prefix (repeatable)")
    parser.add_argument("--latest", type=int, default=0, help="audit the N most recent runs")
    parser.add_argument("--schema", help="read a schema kept by test_live_chain.py --keep-schema")
    parser.add_argument("--conninfo", default=DEFAULT_CONNINFO)
    parser.add_argument("--output", help="write the full report as JSON")
    arguments = parser.parse_args()

    report = collect(arguments)
    samples, skipped = [], 0
    for entry in report:
        model_id = entry["calls"][0]["request"]["model"]["id"]
        counters = build_counters(model_id)
        turns = []
        for row in entry["calls"]:
            usage = (row["response"] or {}).get("usage") or {}
            actual = (usage.get("input") or 0) + (usage.get("cache_read") or 0)
            if actual <= 0:
                skipped += 1
                continue
            request = request_of(row)
            estimates = {counter.name: counter.count(request.messages) for counter in counters}
            turns.append({"iteration": row["iteration"], "actual": actual, "estimates": estimates,
                          "deviation": {name: round(deviation(value, actual), 4)
                                        for name, value in estimates.items()}})
            samples.append((actual, estimates))
        entry["model"] = model_id
        entry["turns"] = turns
        entry.pop("calls")
        entry["counters"] = [counter.name for counter in counters]

    for entry in report:
        if not entry["turns"]:
            continue
        print(f"run {entry['run_id'][:8]}  model={entry['model']}  turns={len(entry['turns'])}")
        for turn in entry["turns"]:
            parts = "  ".join(
                f"{name} {turn['estimates'][name]} ({turn['deviation'][name]:+.0%})"
                if turn["deviation"][name] is not None else f"{name} {turn['estimates'][name]}"
                for name in entry["counters"])
            print(f"   it={turn['iteration']:<2} actual={turn['actual']:<6} {parts}")
        print()

    if not samples:
        print(f"no usable samples ({skipped} calls without provider usage)")
        return
    names = [name for name in samples[0][1]]
    print(f"samples: {len(samples)} calls; skipped without usage: {skipped}")
    for name in names:
        deviations = [abs(estimate[name] - actual) / actual
                      for actual, estimate in samples if actual and name in estimate]
        if not deviations:
            continue
        ratios = [estimate[name] / actual for actual, estimate in samples if actual and name in estimate]
        print(f"  {name:<28} median |error| {statistics.median(deviations):6.1%}"
              f"  mean ratio {statistics.mean(ratios):5.2f}x")
    if arguments.output:
        with open(arguments.output, "w", encoding="utf-8") as handle:
            json.dump({"runs": report, "samples": len(samples), "skipped": skipped}, handle,
                      ensure_ascii=False, indent=1)
        print(f"written: {arguments.output}")


if __name__ == "__main__":
    main()
