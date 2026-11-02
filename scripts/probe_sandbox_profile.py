"""Decide whether a ``(deny default)`` seatbelt profile is usable on this machine.

WHY THIS SCRIPT EXISTS
    ``tools/sandbox.py`` starts every profile with ``(allow default)`` and adds
    targeted denials for writes, outbound network and nine credential paths. Its
    module docstring says macOS "refuses any profile that denies reads globally
    (the loader aborts before main)", which is the reason reads are protected by
    naming paths instead of narrowing to the workspace.

    执行边界设计 §14.4 的第 1 条门槛要的却是 ``(deny default)`` 加九项探针。两者
    不可能同时对。那句"macOS 拒绝全局 deny 读"是三个月前试错的结论，不是实测记录
    ——Codex 的 macOS 沙箱走的就是 deny-default 加一张读白名单，所以它至少需要被
    重新验一次。

WHAT IT MEASURES
    Three profiles against the same synthetic tree (workspace / secret / outside):

      current        reproduce ``tools.sandbox.profile()`` exactly, by calling it
      wide-read      ``(deny default)`` + ``(allow file-read*)``
      narrow-read    ``(deny default)`` + read allowlist for system paths

    Per profile: whether ``/bin/sh`` starts at all, whether the workspace is
    readable and writable, whether an outside write and a credential read are
    denied, whether a local TCP connect is denied, and how many ``*_API_KEY``
    variables the child can see.

LIMITS (read these before quoting a result)
    - It says nothing about ``kill`` against a foreign process or about toolchain
      commands; those belong to the real-project regression, not to a profile probe.
    - A profile that fails to apply (``sandbox_apply: Operation not permitted``,
      exit 71) is reported as ``backend_unavailable``, never as "the profile
      denies everything". Running this inside another sandbox produces exactly
      that result — it has to be run in a plain Terminal.
    - ``narrow-read`` is one candidate, not the answer. If it fails, the report
      names the failing check so the next attempt has a starting point.

Usage:
    PYTHONPATH=src uv run python scripts/probe_sandbox_profile.py \
        --output docs/diagnostics/sandbox-profile-$(date +%F).json
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import socket
import shlex
import subprocess
import sys
import tempfile

SEATBELT = Path("/usr/bin/sandbox-exec")
PROBE_TIMEOUT = 5.0
#: Exit code ``sandbox_apply`` uses when the profile cannot be applied at all.
APPLY_DENIED = 71


def _sbpl(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def wide_read_profile(workspace: Path, tmp: Path) -> str:
    """Deny everything, then hand back the three capabilities a shell needs.

    Reads are unrestricted in this control; it cannot protect other projects.
    """
    return "\n".join([
        "(version 1)",
        "(deny default)",
        "(allow process-exec*)",
        "(allow process-fork)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow file-read*)",
        "(allow file-write*",
        f"  (subpath {_sbpl(workspace)})",
        f"  (subpath {_sbpl(tmp)})",
        '  (literal "/dev/null")',
        '  (literal "/dev/stdout")',
        '  (literal "/dev/stderr")',
        ")",
    ]) + "\n"


def narrow_read_profile(workspace: Path, tmp: Path) -> str:
    """Same, except reads are narrowed to the system, the toolchain and the workspace.

    Metadata is allowed globally on purpose: the dynamic loader stats far more
    than it reads, and denying stats turns every attempt into an unexplained
    abort instead of a readable result.
    """
    readable = ["/bin", "/sbin", "/usr", "/System", "/Library/Apple", "/private/var/db/dyld"]
    for extra in ("/opt/homebrew", "/usr/local", str(Path.home() / ".nvm")):
        if Path(extra).exists():
            readable.append(extra)
    reads = "\n".join(f"  (subpath {_sbpl(item)})" for item in readable + [str(workspace), str(tmp)])
    return "\n".join([
        "(version 1)",
        "(deny default)",
        "(allow process-exec*)",
        "(allow process-fork)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow file-read-metadata)",
        "(allow file-read-data",
        reads,
        ")",
        "(allow file-write*",
        f"  (subpath {_sbpl(workspace)})",
        f"  (subpath {_sbpl(tmp)})",
        '  (literal "/dev/null")',
        '  (literal "/dev/stdout")',
        '  (literal "/dev/stderr")',
        ")",
    ]) + "\n"


def current_profile(workspace: Path) -> tuple[str | None, str]:
    """The profile this repository ships today. Imported, never re-typed.

    A copy would drift; the whole point of this probe is to report on what the
    product actually runs.
    """
    try:
        from chagent.tools.sandbox import SandboxPolicy, SandboxSettings, profile
    except ImportError:
        return None, "chagent not importable (run with PYTHONPATH=src)"
    return profile(SandboxPolicy(workspace=workspace, deny_read=SandboxSettings().deny_read)), "chagent.tools.sandbox.profile"


def run(profile: str, argv: list[str]) -> tuple[int, str]:
    command = [str(SEATBELT), "-p", profile, "--", *argv]
    try:
        finished = subprocess.run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return -9, ""
    return finished.returncode, (finished.stdout + finished.stderr)[-2000:]


def api_key_count(profile: str | None) -> int:
    """How many ``*_API_KEY`` variables the child can see. Values never printed."""
    script = "env | grep -c '_API_KEY=' || true"
    argv = ["/bin/sh", "-c", script]
    if profile is None:
        finished = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        return int(finished.stdout.strip() or 0)
    code, output = run(profile, argv)
    digits = "".join(ch for ch in output if ch.isdigit())
    return int(digits) if digits else 0


def network_denied(profile: str) -> bool | None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        script = f"import socket\nsocket.create_connection(('127.0.0.1', {port}), 1)"
        control, _ = run(profile, [sys.executable, "-c", "print(123)"])
        if control != 0:
            return None
        code, output = run(profile, [sys.executable, "-c", script])
        if code == 0:
            return False
        return True if ("PermissionError" in output or "Operation not permitted" in output) else None
    except OSError:
        return False
    finally:
        listener.close()


def probe(name: str, profile: str | None, tree: dict) -> dict:
    if profile is None:
        return {"name": name, "available": False, "detail": "profile not built"}
    workspace, secret, outside = tree["workspace"], tree["secret"], tree["outside"]
    starts_code, starts_out = run(profile, ["/bin/sh", "-c", "echo alive"])
    started = starts_code == 0 and "alive" in starts_out
    if not started:
        return {"name": name, "available": starts_code != APPLY_DENIED, "checks_meaningful": False,
                "starts": False, "exit_on_apply": starts_code, "startup_output": starts_out}
    checks = {
        "starts": True,
        "workspace_read": run(profile, ["/bin/sh", "-c", f"cat {shlex.quote(str(workspace / 'marker'))} >/dev/null"])[0] == 0,
        "workspace_write": run(profile, ["/bin/sh", "-c", f"printf ok > {shlex.quote(str(workspace / 'written'))}"])[0] == 0,
        "outside_write_denied": run(profile, ["/bin/sh", "-c", f"printf no > {shlex.quote(str(outside / 'denied'))}"])[0] != 0,
        "secret_read_denied": run(profile, ["/bin/sh", "-c", f"cat {shlex.quote(str(secret))} >/dev/null 2>&1"])[0] != 0,
        "home_listing_denied": _home_listing_denied(profile),
        "network_denied": network_denied(profile),
    }
    # A denied write that still leaves a file is worse than a hard failure.
    checks["no_residue"] = not (outside / "denied").exists()
    report = {
        "name": name,
        "available": not (starts_code == APPLY_DENIED and not checks["starts"]),
        "checks": checks,
        "api_keys_visible": api_key_count(profile),
        "exit_on_apply": starts_code,
    }
    if not report["available"]:
        # Every check "passes" when nothing runs at all, which reads like a
        # working boundary. Say so in the record instead of leaving it implicit.
        report["checks_meaningful"] = False
        report["detail"] = ("sandbox_apply refused the profile (exit 71): this process is already "
                            "inside a sandbox. Re-run in a plain Terminal.")
    return report


def _home_listing_denied(profile: str) -> bool | None:
    ssh = Path.home() / ".ssh"
    if not ssh.exists():
        return None
    return run(profile, ["/bin/sh", "-c", f"ls {shlex.quote(str(ssh))} >/dev/null 2>&1"])[0] != 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Is a (deny default) seatbelt profile usable here?")
    parser.add_argument("--output", type=Path, default=None, help="write the record here as JSON")
    args = parser.parse_args()

    if not SEATBELT.exists():
        raise SystemExit(f"{SEATBELT} is missing; this probe is macOS only")

    record = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "sandbox_exec": str(SEATBELT),
        "home": str(Path.home()),
        "inherit_api_keys": api_key_count(None),
        "profiles": [],
    }

    with tempfile.TemporaryDirectory(prefix=".chagent-profile-", dir=Path.cwd()) as directory:
        base = Path(directory).resolve()
        workspace, secret, outside, scratch = base / "ws", base / "secret", base / "outside", base / "scratch"
        for path in (workspace, outside, scratch):
            path.mkdir()
        secret.write_text("probe\n", encoding="utf-8")
        (workspace / "marker").write_text("marker\n", encoding="utf-8")
        tree = {"workspace": workspace, "secret": secret, "outside": outside}

        built = {
            "current": current_profile(workspace)[0],
            "wide-read": wide_read_profile(workspace, scratch),
            "narrow-read": narrow_read_profile(workspace, scratch),
        }
        candidate = built["narrow-read"]
        variants = {
            "original": candidate,
            "read_all_ops_for_allowed_paths": candidate.replace("(allow file-read-data\n", "(allow file-read*\n"),
            "allow_var_db": candidate.replace("(allow file-read-metadata)", "(allow file-read-metadata)\n(allow file-read* (subpath \"/private/var/db\"))"),
            "allow_all_reads": candidate.replace("(allow file-read-metadata)", "(allow file-read*)"),
            "allow_all_process_ops": candidate.replace("(allow process-exec*)", "(allow process*)"),
        }
        record["narrow_diagnostics"] = []
        for name, variant in variants.items():
            code, output = run(variant, ["/bin/sh", "-c", "echo alive"])
            record["narrow_diagnostics"].append({"variant": name, "exit_code": code, "output": output})
        for name, profile in built.items():
            (outside / "denied").unlink(missing_ok=True)
            record["profiles"].append(probe(name, profile, tree))
            # Each run may leave the workspace written; that is what we measure.

    deny_default = [item for item in record["profiles"]
                    if item["name"] != "current" and item["available"] and item.get("checks", {}).get("starts")]
    applied = [item for item in record["profiles"] if item["available"]]
    if not applied:
        conclusion = ("inconclusive: no profile could be applied in this process (exit 71), so "
                      "nothing here is evidence about deny-default — re-run in Terminal.app")
    elif deny_default:
        conclusion = ("deny-default can start a shell; further checks are needed before concluding §14.4 gate 1 is "
                      "reachable by narrowing reads")
    else:
        conclusion = ("deny-default applied but no shell started; read the failing check in 'checks' "
                      "before changing the plan")
    record["verdict"] = {
        "deny_default_starts_a_shell": bool(deny_default),
        "shell_started": [item["name"] for item in deny_default],
        "conclusion": conclusion,
    }

    text = json.dumps(record, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    if record["profiles"] and all(not item["available"] for item in record["profiles"]):
        raise SystemExit("no profile could be applied here (exit 71) — this result proves nothing "
                         "about deny-default; re-run in Terminal.app")


if __name__ == "__main__":
    main()
