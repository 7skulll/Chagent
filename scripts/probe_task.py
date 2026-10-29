"""Reproducible paid-provider task probes with turn-level metrics.

Run from the repository root:
  uv run --env-file .env python scripts/probe_task.py --case all \
    --repeats 2 --output docs/diagnostics/probe-baseline.json

Every run uses a fresh synthetic repository and a disposable database schema.
Only synthetic fixture content is sent to the provider. The default exit code
reports harness success, not target attainment; use --require-targets for a gate.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from time import perf_counter
from typing import Any
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

try:  # Direct script execution versus offline unit-test import.
    from run_metrics import summarize_tool_calls
except ModuleNotFoundError:  # pragma: no cover - exercised by the script CLI
    from scripts.run_metrics import summarize_tool_calls
from chagent.bootstrap import create_runner
from chagent.config import Settings
from chagent.domain.memories import MemoryPolicy
from chagent.domain.summaries import CompactionPolicy
from chagent.models.events import DoneEvent, ErrorEvent
from chagent.models.factory import create_model_client
from chagent.models.settings import ModelSettings
from chagent.storage.database import Database
from chagent.storage.migrate import migrate

SCENARIOS = ("fix", "edit", "multifile", "long")


def write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected_hashes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): digest(path)
            for path in sorted((root / "tests").glob("*.py"))} if (root / "tests").exists() else {}


def build_fixture(name: str, root: Path) -> str:
    """Create one deterministic repository and return its task prompt."""
    if name not in SCENARIOS:
        raise ValueError(f"Unknown probe scenario: {name}")
    root.mkdir(parents=True, exist_ok=True)
    write(root, "app/__init__.py", "")
    if name == "fix":
        write(root, "app/pricing.py", '''"""Order pricing with tiered discounts."""

TIERS = ((100, 0.05), (500, 0.10), (1000, 0.15))


def discount_rate(subtotal_cents: int) -> float:
    for threshold, rate in TIERS:
        if subtotal_cents >= threshold:
            return rate
    return 0.0


def total_cents(items, member=False):
    subtotal = sum(price * qty for price, qty in items)
    rate = discount_rate(subtotal)
    if member:
        rate += 0.02
    return int(round(subtotal * (1 - rate)))
''')
        write(root, "app/invoice.py", '''from app.pricing import total_cents


def render(items, member=False):
    return f"TOTAL={total_cents(items, member)}"
''')
        write(root, "tests/test_invoice.py", '''import unittest

from app.invoice import render
from app.pricing import discount_rate, total_cents


class PricingTests(unittest.TestCase):
    def test_tier_rate_is_best_eligible(self):
        self.assertEqual(discount_rate(1000), 0.15)
        self.assertEqual(discount_rate(600), 0.10)
        self.assertEqual(discount_rate(120), 0.05)
        self.assertEqual(discount_rate(50), 0.0)

    def test_total(self):
        self.assertEqual(total_cents([(1000, 1)]), 850)
        self.assertEqual(total_cents([(1000, 1)], member=True), 830)

    def test_render(self):
        self.assertEqual(render([(200, 2)]), "TOTAL=380")


if __name__ == "__main__":
    unittest.main()
''')
        for index in range(3):
            write(root, f"docs/note-{index}.md", "# Background\n" + "Unrelated material. " * 60)
        return ("tests 目录里有 2 个失败用例。找出根因，修改 app/ 下的源码把它们修好，"
                "然后实际运行测试确认全部通过。不要修改 tests 下的任何文件。")
    if name == "edit":
        write(root, "app/text_utils.py", 'LABEL = "“draft”"   \nDEFAULT_MODE = "safe"\n\n\ndef normalize(value):\n    value = value.strip()   \n    return value.lower()\n\n\ndef render(value):\n    suffix = "…"   \n    return f"{normalize(value)}{suffix}"\n')
        write(root, "tests/test_text_utils.py", '''import unittest

from app.text_utils import LABEL, normalize, render


class TextTests(unittest.TestCase):
    def test_label(self):
        self.assertEqual(LABEL, "“published”")

    def test_casefold(self):
        self.assertEqual(normalize("  Straße "), "strasse")

    def test_render(self):
        self.assertEqual(render(" Straße "), "strasse!")


if __name__ == "__main__":
    unittest.main()
''')
        return ("修改 app/text_utils.py 中三处不连续内容：LABEL 的 “draft” 改为 “published”；"
                "normalize 中的 lower() 改为 casefold()；render 中的 suffix 从 … 改为半角 !。"
                "尽量用一次编辑调用完成，保留尾随空格等其他内容，不要修改 tests，最后运行测试。")
    if name == "multifile":
        write(root, "app/pricing.py", '''def total_cents(items, member=False):
    subtotal = sum(price * qty for price, qty in items)
    rate = 0.10 if member else 0.0
    return int(round(subtotal * (1 - rate)))
''')
        write(root, "app/invoice.py", '''from app.pricing import total_cents


def render(items, member=False):
    return f"TOTAL={total_cents(items, member)}"
''')
        write(root, "app/report.py", '''from app.pricing import total_cents


def summary(items, member=False):
    return {"total": total_cents(items, member)}
''')
        write(root, "tests/test_totals.py", '''import unittest

from app.invoice import render
from app.pricing import total_cents
from app.report import summary


class TotalTests(unittest.TestCase):
    def test_tax_after_discount(self):
        self.assertEqual(total_cents([(1000, 1)], tax_rate=0.2), 1200)
        self.assertEqual(total_cents([(1000, 1)], member=True, tax_rate=0.2), 1080)

    def test_call_sites_forward_tax(self):
        self.assertEqual(render([(1000, 1)], tax_rate=0.2), "TOTAL=1200")
        self.assertEqual(summary([(1000, 1)], tax_rate=0.2), {"total": 1200})


if __name__ == "__main__":
    unittest.main()
''')
        return ("把 app/pricing.py 的 total_cents 签名改为 (items, member=False, tax_rate=0.0)，"
                "在会员折扣之后按 (1 + tax_rate) 加收并取整。同步修改 invoice.render 与 report.summary "
                "两个调用点，让它们也接受并传递 tax_rate。不要修改 tests，最后运行测试。")
    write(root, "scripts/slow_job.py", '''import json
from pathlib import Path
from time import sleep

print("SLOW_JOB_STARTED", flush=True)
sleep(45)
Path("long-result.json").write_text(json.dumps({"marker": "SLOW_JOB_DONE"}), encoding="utf-8")
print("SLOW_JOB_DONE", flush=True)
''')
    return ("运行 python scripts/slow_job.py 并等待它结束，然后回答 SLOW_JOB_DONE。"
            "只尝试一次，不要用其他命令代替，不要修改任何文件；如果工具无法等待这么久的命令，如实报告。")


def run_fixture_tests(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                          cwd=root, capture_output=True, text=True, timeout=20, check=False)


def verify_fixture(name: str, root: Path, protected: dict[str, str]) -> dict[str, Any]:
    unchanged = all(path.is_file() and digest(path) == expected
                    for relative, expected in protected.items() for path in [root / relative])
    if name == "long":
        try:
            marker = json.loads((root / "long-result.json").read_text(encoding="utf-8")).get("marker")
        except (OSError, ValueError):
            marker = None
        return {"expected_result_created": marker == "SLOW_JOB_DONE",
                "protected_files_unchanged": unchanged}
    completed = run_fixture_tests(root)
    return {"tests_pass": completed.returncode == 0,
            "protected_files_unchanged": unchanged,
            "test_output_tail": (completed.stdout + completed.stderr)[-1200:]}


class MeasuredClient:
    def __init__(self, client, max_calls: int):
        self.client, self.max_calls = client, max_calls
        self.calls = []

    async def stream(self, request):
        if len(self.calls) >= self.max_calls:
            raise RuntimeError("Probe provider request cap reached")
        record = {"purpose": request.purpose, "usage": None}
        self.calls.append(record)
        async for event in self.client.stream(request):
            if isinstance(event, (DoneEvent, ErrorEvent)):
                message = event.message if isinstance(event, DoneEvent) else event.error
                record["usage"] = asdict(message.usage) if message.usage else None
            yield event


def targets(name: str, record: dict[str, Any]) -> dict[str, Any]:
    sequence = record["tool_sequence"]
    mutations = sum(call["name"] in {"edit_file", "write_file", "apply_patch"} for call in sequence)
    if name == "fix":
        return {"turns_max": 4, "tool_calls_max": 6, "elapsed_s_max": 6.5,
                "met": record["correct"] and record["turns"] <= 4
                and record["tool_calls"] <= 6 and record["elapsed_s"] <= 6.5}
    if name == "edit":
        return {"mutation_calls_max": 1,
                "met": record["correct"] and mutations == 1}
    if name == "multifile":
        return {"turns_max": 5, "write_file_max": 0,
                "met": record["correct"] and record["turns"] <= 5
                and not any(call["name"] == "write_file" for call in sequence)}
    return {"expected_before_persistent_shell": False, "met": record["correct"]}


async def run_one(name: str, repetition: int, database: Database, selected: ModelSettings,
                  client: MeasuredClient) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"chagent-probe-{name}-") as directory:
        root = Path(directory).resolve()
        prompt = build_fixture(name, root)
        protected = protected_hashes(root)
        runner = create_runner(database, selected.model(), client, allow_host_commands=True,
                               timeout=100 if name == "long" else 75, max_turns=10,
                               memory_policy=MemoryPolicy(enabled=False, extract=False),
                               compaction_policy=CompactionPolicy(enabled=False))
        session = await runner.conversations.create_session(root, f"probe-{name}-{repetition}")
        first_call = len(client.calls)
        started = perf_counter()
        identities = []
        result = None
        error_type = None
        try:
            result = await runner.run(session, prompt, on_started=identities.append)
        except Exception as caught:
            error_type = type(caught).__name__
        elapsed = perf_counter() - started
        run_id = result.run_id if result else (identities[0] if identities else None)
        tool_rows = []
        if run_id:
            async with database.connection() as connection:
                cursor = await connection.execute(
                    "SELECT name, arguments, status, call_index FROM tool_calls "
                    "WHERE run_id = %s ORDER BY created_at, call_index", (run_id,))
                tool_rows = await cursor.fetchall()
        checks = await asyncio.to_thread(verify_fixture, name, root, protected)
        checks["run_completed"] = result is not None
        provider_calls = client.calls[first_call:]
        record = {"scenario": name, "repetition": repetition,
                  "run_id": str(run_id) if run_id else None,
                  "turns": len(provider_calls), "elapsed_s": round(elapsed, 3),
                  "correct": all(checks.values()), "checks": checks,
                  "end_reason": result.end_reason if result else None,
                  "error_type": error_type, **summarize_tool_calls(tool_rows),
                  "usage": {key: sum((call["usage"] or {}).get(key, 0) for call in provider_calls)
                            for key in ("input", "output", "cache_read", "cache_write")}}
        record["target"] = targets(name, record)
        return record


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    report = {}
    for name in SCENARIOS:
        rows = [row for row in results if row["scenario"] == name]
        if not rows:
            continue
        turns = [row["turns"] for row in rows]
        report[name] = {"runs": len(rows), "correct_runs": sum(row["correct"] for row in rows),
                        "target_runs": sum(row["target"]["met"] for row in rows),
                        "turns": turns, "turn_variation": max(turns) - min(turns),
                        "tool_calls": [row["tool_calls"] for row in rows],
                        "elapsed_s": [row["elapsed_s"] for row in rows]}
    return report


async def main_async(args) -> dict[str, Any]:
    configured = Settings.from_env()
    selected = replace(ModelSettings.from_env(), max_tokens=2048, request_timeout=60, run_timeout=100)
    schema = "chagent_probe_" + uuid4().hex
    report = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "configuration": {"provider": selected.provider, "model": selected.model_id,
                                "api": selected.api, "case": args.case, "repeats": args.repeats,
                                "max_provider_requests": args.max_calls},
              "environment": {"python": platform.python_version(), "platform": platform.system()},
              "results": [], "schema_cleaned": False}
    admin = await AsyncConnection.connect(configured.database_url, autocommit=True, connect_timeout=5)
    observed = None
    try:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            database_settings = replace(configured, database_url=make_conninfo(
                configured.database_url, options=f"-c search_path={schema}"))
            async with Database(database_settings) as database:
                await migrate(database)
                async with create_model_client(selected) as raw_client:
                    observed = MeasuredClient(raw_client, args.max_calls)
                    names = SCENARIOS if args.case == "all" else (args.case,)
                    for name in names:
                        for repetition in range(1, args.repeats + 1):
                            try:
                                record = await run_one(name, repetition, database, selected, observed)
                            except Exception as caught:
                                record = {"scenario": name, "repetition": repetition, "correct": False,
                                          "error_type": type(caught).__name__, "turns": 0,
                                          "tool_calls": 0, "repeated_tool_calls": 0,
                                          "elapsed_s": 0, "tool_sequence": [],
                                          "target": {"met": False}}
                            report["results"].append(record)
                            summary = {key: record.get(key) for key in
                                       ("scenario", "repetition", "correct", "turns", "tool_calls",
                                        "repeated_tool_calls", "elapsed_s", "error_type")}
                            summary["target_met"] = record["target"]["met"]
                            if not record["correct"]:
                                summary["tool_sequence"] = record["tool_sequence"]
                            print(json.dumps(summary, ensure_ascii=False), flush=True)
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            report["schema_cleaned"] = True
    finally:
        await admin.close()
        report["summary"] = summarize(report["results"])
        report["total_provider_requests"] = len(observed.calls) if observed else 0
        root = Path(__file__).resolve().parents[1]
        report["source_sha256"] = {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (root / "src/chagent").rglob("*.py")}
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        assert selected.api_key not in serialized, "Refusing to save a credential-bearing report"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", *SCENARIOS), default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=60)
    parser.add_argument("--require-targets", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 5:
        parser.error("--repeats must be between 1 and 5")
    if not 1 <= args.max_calls <= 200:
        parser.error("--max-calls must be between 1 and 200")
    report = asyncio.run(main_async(args))
    targets_met = all(row["target"]["met"] for row in report["results"])
    print(json.dumps({"results": len(report["results"]), "targets_met": targets_met,
                      "schema_cleaned": report["schema_cleaned"],
                      "report": str(args.output.resolve())}, ensure_ascii=False))
    raise SystemExit(0 if targets_met or not args.require_targets else 1)


if __name__ == "__main__":
    main()
