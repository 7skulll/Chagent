"""Score prompt-cache reuse per run from the recorded provider payloads.

Providers cache a byte-identical prefix. This script answers three questions
for any run, without calling a provider:

1. how much of each prompt the provider served from cache;
2. how much of it was *reusable*: the overlap with the previous request;
3. if a request could not reuse its predecessor, which section broke it.

Sections are compared in prompt order (system text, tool declarations, then
messages one by one), because that is the order a prefix cache sees. A run is
at its ceiling when ``cached`` tracks ``reusable``: the only uncached tokens are
the ones that had never been sent before.

Usage:
    PYTHONPATH=src python scripts/report_prompt_cache.py [--run PREFIX | --latest N]
"""

import argparse
import json

import psycopg
from psycopg.rows import dict_row

from chagent.domain.codec import message_from_dict
from chagent.models.base import Model, ModelRequest
from chagent.models.transcript import openai_payload

CONNINFO = "postgresql:///chagent"


def payload_of(row) -> dict:
    """Rebuild exactly what the chat-completions adapter sends for this call."""
    request = row["request"]
    return openai_payload(ModelRequest(
        model=Model(**request["model"]),
        messages=[message_from_dict(item) for item in request["messages"]],
        max_tokens=request.get("max_tokens"), temperature=request.get("temperature"),
        purpose=request.get("purpose", "task"),
    ))


def sections(payload: dict) -> list[tuple[str, str]]:
    """The request in prompt order, as labelled chunks."""
    chunks = [("system", payload.get("system") or "")]
    chunks.append(("tools", json.dumps(payload.get("tools"), ensure_ascii=False, sort_keys=True)))
    for index, message in enumerate(payload.get("messages") or []):
        chunks.append((f"message #{index} ({message.get('role')})",
                       json.dumps(message, ensure_ascii=False, sort_keys=True)))
    return chunks


def compare(previous: list[tuple[str, str]], current: list[tuple[str, str]]) -> tuple[int, int, int, str | None]:
    """Return (shared_chunks, shared_bytes, current_bytes, break_label)."""
    shared = 0
    for (_, left), (_, right) in zip(previous, current):
        if left != right:
            break
        shared += 1
    shared_bytes = sum(len(text.encode()) for _, text in current[:shared])
    total = sum(len(text.encode()) for _, text in current)
    label = current[shared][0] if shared < len(previous) and shared < len(current) else None
    return shared, shared_bytes, total, label


def scan(rows) -> dict:
    turns, previous = [], None
    for row in rows:
        usage = row["response"].get("usage") or {}
        cached = usage.get("cache_read") or 0
        prompt = (usage.get("input") or 0) + cached
        current = sections(payload_of(row))
        entry = {
            "iteration": row["iteration"],
            "messages": sum(1 for label, _ in current if label.startswith("message")),
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "cache_hit": round(cached / prompt, 4) if prompt else None,
            "reusable_share": None,
            "break": None,
        }
        if previous is not None:
            shared, shared_bytes, total, label = compare(previous, current)
            entry["reusable_share"] = round(shared_bytes / total, 4) if total else None
            entry["break"] = label
        turns.append(entry)
        previous = current
    prompt_total = sum(turn["prompt_tokens"] for turn in turns)
    cached_total = sum(turn["cached_tokens"] for turn in turns)
    return {
        "turns": turns,
        "prompt_tokens": prompt_total,
        "cached_tokens": cached_total,
        "cache_hit": round(cached_total / prompt_total, 4) if prompt_total else None,
        "breaks": sum(1 for turn in turns if turn["break"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="append", default=[], help="run id prefix (repeatable)")
    parser.add_argument("--latest", type=int, default=0, help="audit the N most recent runs")
    parser.add_argument("--output", help="write the full report as JSON")
    arguments = parser.parse_args()

    with psycopg.connect(CONNINFO, row_factory=dict_row) as connection:
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
            summary = scan(rows)
            summary["run_id"] = str(run["id"])
            report.append(summary)

    for entry in report:
        hit_total = "—" if entry["cache_hit"] is None else f"{entry['cache_hit']:.1%}"
        print(f"run {entry['run_id'][:8]}  turns={len(entry['turns'])}  "
              f"prompt={entry['prompt_tokens']}  cached={entry['cached_tokens']}  "
              f"hit={hit_total}  breaks={entry['breaks']}")
        for turn in entry["turns"]:
            share = "—" if turn["reusable_share"] is None else f"{turn['reusable_share']:.1%}"
            hit = "—" if turn["cache_hit"] is None else f"{turn['cache_hit']:.1%}"
            print(f"   it={turn['iteration']:<2} msgs={turn['messages']:<3} prompt={turn['prompt_tokens']:<6} "
                  f"cached={turn['cached_tokens']:<6} hit={hit:<7} reusable={share:<7}"
                  + (f" break at {turn['break']}" if turn["break"] else ""))
        print()

    prompt = sum(entry["prompt_tokens"] for entry in report)
    cached = sum(entry["cached_tokens"] for entry in report)
    clean = sum(1 for entry in report if not entry["breaks"])
    if not prompt:
        print("no calls recorded")
        return
    print(f"total: {len(report)} runs, prompt={prompt}, cached={cached}, hit={cached / prompt:.1%}")
    print(f"runs whose every turn reused the previous prefix: {clean}/{len(report)}")
    if arguments.output:
        with open(arguments.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
        print(f"written: {arguments.output}")


if __name__ == "__main__":
    main()
