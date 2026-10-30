"""Exercise the two outbound tools against the real provider, and record what they did.

WHY THIS SCRIPT EXISTS
    W 批 shipped web_search against a provider whose key never existed on this
    machine, so its live path was recorded as "未验" and stayed that way until
    the provider changed. WD 批 moved both tools onto DeepSeek's Search/Fetch
    API, whose key we already have — so the honest thing is to measure it once
    and archive the numbers instead of trusting the documentation.

WHAT IT MEASURES (and refuses to infer)
    Per call: HTTP status, wall clock, result count, and how many characters
    reach the model versus how many the provider returned. It does not run the
    model, so it says nothing about turns: a search that returns 15 000
    characters of ``summary`` explains *why* a budget cap exists, it does not
    prove the cap is right. That needs the task probe.

COST
    Each Search and each Fetch that returns results is one billed request
    (5 CNY / 1000). The default run is one search plus one fetch, so ~0.01 CNY.

Usage:
    PYTHONPATH=src uv run --env-file .env python scripts/probe_web_search.py \
        --query "python 3.13 what's new" --output docs/diagnostics/probe-web-search.json
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import monotonic

from chagent.config import Settings
from chagent.tools.web import (
    FETCH_ENDPOINT, MAX_FETCH_CHARS, MAX_SEARCH_CHARS, SEARCH_ENDPOINT, WebAccess, create_web_tools,
)


def settings_from_env() -> Settings:
    """Read the search key without turning the probe into a running agent."""
    env = dict(__import__("os").environ)
    env.setdefault("CHAGENT_WEB_ACCESS", "deepseek")
    try:
        return Settings.from_env(env)
    except ValueError as error:
        raise SystemExit(f"{error}\nSet CHAGENT_SEARCH_API_KEY (or DEEPSEEK_API_KEY) first.") from None


async def run(session_query: str, count: int, recency: str | None, fetch: bool) -> dict:
    settings = settings_from_env()
    if settings.web_access == "off":
        raise SystemExit("CHAGENT_WEB_ACCESS is off; this probe only makes sense with a provider selected")
    tools = {tool.name: tool for tool in create_web_tools(WebAccess(settings.search_api_key))}
    record: dict = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider": settings.web_access,
        "endpoints": {"search": SEARCH_ENDPOINT, "fetch": FETCH_ENDPOINT},
        "budgets": {"search_chars": MAX_SEARCH_CHARS, "fetch_chars": MAX_FETCH_CHARS},
        "key_source": "CHAGENT_SEARCH_API_KEY" if settings.search_api_key else "missing",
        "results": [],
    }

    params: dict = {"query": session_query, "count": count}
    if recency:
        params["recency"] = recency
    started = monotonic()
    try:
        search = await tools["web_search"].execute("probe-search", params)
    except Exception as error:  # noqa: BLE001 - the point is to record the failure class
        record["results"].append({"name": "search", "passed": False,
                                  "error": f"{type(error).__name__}: {error}",
                                  "elapsed_ms": round((monotonic() - started) * 1000, 1)})
        return record
    elapsed = round((monotonic() - started) * 1000, 1)
    text = search.content[0].text
    rows = search.details.get("results") or []
    record["results"].append({
        "name": "search", "passed": True, "elapsed_ms": elapsed,
        "query": params, "sent_query": search.details.get("sent_query"),
        "result_count": len(rows),
        "text_chars": len(text), "text_truncated": bool(search.details.get("truncated")),
        "text_head": text[:400],
        "details_chars": len(json.dumps(search.details, ensure_ascii=False)),
        "cards": [{"title": (row.get("title") or "")[:60], "url": row.get("url"),
                   "site_name": row.get("site_name"), "date": row.get("date"),
                   "authority": row.get("authority"), "score": row.get("score"),
                   "snippet_chars": len(row.get("snippet") or "")} for row in rows[:5]],
        "quality": {
            "score_ge_1_5": sum(1 for row in rows if isinstance(row.get("score"), (int, float))
                                and row["score"] >= 1.5),
            "authority_ge_3": sum(1 for row in rows if isinstance(row.get("authority"), int)
                                  and row["authority"] >= 3),
            "dated": sum(1 for row in rows if row.get("date")),
            "named_site": sum(1 for row in rows if row.get("site_name")),
        },
    })

    if fetch and rows:
        url = rows[0]["url"]
        started = monotonic()
        try:
            page = await tools["web_fetch"].execute("probe-fetch", {"url": url})
        except Exception as error:  # noqa: BLE001
            record["results"].append({"name": "fetch", "passed": False, "url": url,
                                      "error": f"{type(error).__name__}: {error}",
                                      "elapsed_ms": round((monotonic() - started) * 1000, 1)})
            return record
        elapsed = round((monotonic() - started) * 1000, 1)
        body = page.content[0].text
        record["results"].append({
            "name": "fetch", "passed": True, "url": url, "elapsed_ms": elapsed,
            "text_chars": len(body), "source_chars": page.details.get("chars"),
            "markdown": bool(page.details.get("format") == "markdown"),
            "has_headings": "\n#" in body, "truncated": bool(page.details.get("truncated")),
            "head": body[:300],
        })
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", default="python 3.13 what's new", help="search query to send")
    parser.add_argument("--count", type=int, default=5, help="results requested (1..8)")
    parser.add_argument("--recency", choices=("day", "week", "month", "year"), default=None)
    parser.add_argument("--no-fetch", action="store_true", help="skip reading the first result")
    parser.add_argument("--output", type=Path, default=None, help="write the record here as JSON")
    args = parser.parse_args()

    record = asyncio.run(run(args.query, args.count, args.recency, not args.no_fetch))
    text = json.dumps(record, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    failed = [item for item in record["results"] if not item["passed"]]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
