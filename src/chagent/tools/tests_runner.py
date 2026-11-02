"""One test run per call, reduced to a digest the model can act on.

Running the suite is the same work as ``exec_command``; what this tool adds is
(1) suite detection, (2) a bounded, structured summary so failure output does
not flood the context, and (3) a single process-group lifetime, so a hung test
run cannot outlive the call.
"""

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import re
import signal as signals
import sys
from time import monotonic

from chagent.domain.messages import JsonObject, TextContent
from chagent.tools.base import AgentTool, AgentToolResult, AgentToolUpdateCallback
from chagent.tools.context import ToolContext
from chagent.tools.sandbox import Sandbox


MAX_CAPTURE_BYTES = 262_144
OUTPUT_TAIL_CHARS = 4_000
MAX_MESSAGE_CHARS = 600


def detect_suite(workspace: Path) -> str:
    """Pick a runner from the files present; never guesses beyond a clear signal."""
    for marker in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"):
        candidate = workspace / marker
        if candidate.is_file():
            try:
                if "pytest" in candidate.read_text(encoding="utf-8", errors="replace"):
                    return "pytest"
            except OSError:
                pass
    if (workspace / "go.mod").is_file():
        return "go"
    if (workspace / "Cargo.toml").is_file():
        return "cargo"
    package = workspace / "package.json"
    if package.is_file():
        try:
            import json

            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        if "test" in scripts:
            return "npm"
    for directory in ("tests", "test"):
        if any((workspace / directory).glob("test*.py")):
            return "unittest"
    if any(workspace.glob("test_*.py")):
        return "unittest"
    return "pytest"


def _python(workspace: Path) -> str:
    """Prefer the project's own interpreter, then the one running this process."""
    for candidate in (workspace / ".venv/bin/python", workspace / "venv/bin/python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def build_command(suite: str, workspace: Path, selection: str | None) -> list[str]:
    if suite in {"pytest", "unittest"}:
        interpreter = _python(workspace)
        if suite == "pytest":
            command = [interpreter, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
            return command + (["-k", selection] if selection else [])
        base = "tests" if (workspace / "tests").is_dir() else ("test" if (workspace / "test").is_dir() else ".")
        # No -t: a top-level dir makes discovery require an importable package,
        # which would reject the common bare tests/ layout. ``python -m`` already
        # puts the workspace on sys.path, so application imports still resolve.
        command = [interpreter, "-m", "unittest", "discover", "-s", base]
        return command + (["-k", selection] if selection else [])
    if suite == "npm":
        return ["npm", "test", "--silent"] + ([selection] if selection else [])
    if suite == "go":
        return ["go", "test", "./..."] + (["-run", selection] if selection else [])
    if suite == "cargo":
        return ["cargo", "test", "-q"] + ([selection] if selection else [])
    raise ValueError(f"Unsupported suite: {suite}")


def _count(text: str, word: str) -> int:
    match = re.search(rf"(\d+) {word}\b", text)
    return int(match.group(1)) if match else 0


def summarize_pytest(output: str) -> tuple[dict, list[dict]]:
    counts = {"passed": _count(output, "passed"), "failed": _count(output, "failed"),
              "errors": _count(output, "error") + _count(output, "errors")}
    failures = []
    for pattern in (r"^FAILED (\S+)(?: - (.*))?$", r"^ERROR (\S+)(?: - (.*))?$"):
        for match in re.finditer(pattern, output, re.M):
            name, message = match.group(1), (match.group(2) or "").strip()
            failures.append({"name": name, "message": (message or "collection error")[:MAX_MESSAGE_CHARS]})
    return counts, failures


def summarize_unittest(output: str) -> tuple[dict, list[dict]]:
    ran = re.search(r"^Ran (\d+) tests?", output, re.M)
    total = int(ran.group(1)) if ran else 0
    failures = errors = 0
    summary = re.search(r"^FAILED \((.*)\)$", output, re.M)
    if summary:
        failure_match = re.search(r"failures=(\d+)", summary.group(1))
        error_match = re.search(r"errors=(\d+)", summary.group(1))
        failures = int(failure_match.group(1)) if failure_match else 0
        errors = int(error_match.group(1)) if error_match else 0
    found = []
    for match in re.finditer(r"^(FAIL|ERROR): (\S+) \((.*?)\)\n(.*?)(?=\n(?:FAIL|ERROR): |\n-{20,}|\Z)",
                             output, re.M | re.S):
        _, name, location, block = match.groups()
        detail = [line.strip() for line in block.strip().splitlines() if line.strip()]
        # 3.11+ already reports the full id inside the parentheses.
        label = location if location.endswith(f".{name}") else f"{location}.{name}"
        found.append({"name": label, "message": (detail[-1] if detail else "")[:MAX_MESSAGE_CHARS]})
    return {"passed": max(0, total - failures - errors), "failed": failures, "errors": errors}, found


class RunTestsTool(AgentTool):
    def __init__(self, sandbox: Sandbox | None = None):
        self.sandbox = sandbox
        super().__init__(
            name="run_tests", label="Run tests",
            description="Run the project's test suite once and return a compact digest: counts plus the first "
                        "failures with file and assertion message. Detects pytest, unittest, npm, go or cargo. "
                        "Prefer this over repeating shell commands: one call per verification, not one per rerun.",
            parameters={
                "type": "object", "properties": {
                    "suite": {"type": "string", "enum": ["auto", "pytest", "unittest", "npm", "go", "cargo"]},
                    "selection": {"type": "string", "minLength": 1, "maxLength": 256},
                    "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 300_000},
                    "max_failures": {"type": "integer", "minimum": 1, "maximum": 20},
                }, "required": [], "additionalProperties": False,
            }, requires_workspace=True, requires_host_execution=True, execution_mode="sequential",
        )

    def execution_timeout(self, params: JsonObject) -> float:
        return params.get("timeout_ms", 120_000) / 1000 + 10

    async def execute(
        self, tool_call_id: str, params: JsonObject, signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None, *, context: ToolContext | None = None,
    ) -> AgentToolResult:
        if context is None:
            raise PermissionError("A session workspace is required")
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        workspace = context.workspace.resolve()
        suite = params.get("suite", "auto")
        suite = detect_suite(workspace) if suite == "auto" else suite
        command = build_command(suite, workspace, params.get("selection"))
        if self.sandbox is not None and self.sandbox.settings.mode == "isolated" and command[0] == sys.executable:
            command[0] = str(Path(sys.executable).resolve())
        child_env = self.sandbox.environment(workspace) if self.sandbox is not None else None
        timeout = params.get("timeout_ms", 120_000) / 1000
        argv, sandbox_record = ["/bin/sh", "-c", " ".join(_quote(part) for part in command)], {}
        if self.sandbox is not None:
            argv, sandbox_record = await self.sandbox.wrap(argv, workspace)
        started = monotonic()
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, cwd=workspace, env=child_env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        ))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            _terminate(process.pid)
            await process.communicate()
            raise

        captured = bytearray()

        async def capture():
            while chunk := await process.stdout.read(8192):
                room = max(0, MAX_CAPTURE_BYTES - len(captured))
                captured.extend(chunk[:room])

        reader = asyncio.create_task(capture())
        waiter = asyncio.create_task(process.wait())
        timed_out = False
        try:
            done, _ = await asyncio.wait({waiter, reader}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                timed_out = True
                _terminate(process.pid)
            await asyncio.gather(waiter, reader, return_exceptions=timed_out)
        except BaseException:
            _terminate(process.pid)
            await asyncio.gather(waiter, reader, return_exceptions=True)
            raise
        finally:
            _terminate(process.pid)

        output = captured.decode("utf-8", errors="replace")
        duration = (monotonic() - started) * 1000
        counts, failures = (summarize_unittest(output) if suite == "unittest"
                            else summarize_pytest(output) if suite == "pytest"
                            else ({}, []))
        if not counts and not failures:
            # Unknown runner output: keep the tail, which holds the summary line.
            tail = [line for line in output.strip().splitlines() if line.strip()][-8:]
            failures = [{"name": "output", "message": line[:MAX_MESSAGE_CHARS]} for line in tail[-3:]]
        kept = failures[:params.get("max_failures", 5)]
        verdict = "timed out" if timed_out else ("passed" if process.returncode == 0 else "failed")
        header = [f"Suite: {suite} ({' '.join(command)})", f"Result: {verdict} (exit {process.returncode}) "
                                                          f"in {duration:.0f}ms"]
        if counts:
            header.append("Counts: " + ", ".join(f"{value} {name}" for name, value in counts.items()))
        if kept:
            header.append(f"Failures ({len(failures)} total, showing {len(kept)}):")
            header.extend(f"{number}. {item['name']} - {item['message']}" for number, item in enumerate(kept, 1))
        tail = output.strip()[-OUTPUT_TAIL_CHARS:] if process.returncode != 0 or timed_out else ""
        text = "\n".join(header) + ("\n--- output tail ---\n" + tail if tail else "")
        return AgentToolResult(
            content=[TextContent(text)], is_error=timed_out or process.returncode != 0,
            outcome_unknown=timed_out,
            details={"suite": suite, "command": command, "exit_code": process.returncode,
                     "duration_ms": round(duration, 1), "counts": counts,
                     "failures": failures[:20], "timed_out": timed_out,
                     **({"sandbox": sandbox_record} if sandbox_record else {}),
                     "output": output[-MAX_CAPTURE_BYTES:]},
        )


def _quote(part: str) -> str:
    return part if re.fullmatch(r"[A-Za-z0-9_./:=@+-]+", part) else "'" + part.replace("'", "'\\''") + "'"


def _terminate(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pid, signals.SIGKILL)
