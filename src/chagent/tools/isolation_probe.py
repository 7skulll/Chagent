"""Positive and negative controls for the isolated seatbelt execution mode."""
import asyncio
from contextlib import suppress
from dataclasses import replace
import os
from pathlib import Path
import shlex
import socket
import sys
import tempfile

from chagent.tools.sandbox import SandboxSupport, _run, isolated_environment


async def probe_isolated(sandbox, policy):
    checks = {}
    with tempfile.TemporaryDirectory(prefix="chagent-isolation-probe-") as directory:
        base = Path(directory).resolve()
        workspace, scratch, outside = (base / name for name in ("workspace", "scratch", "other-project"))
        for root in (workspace, scratch, outside, scratch / "home", scratch / "tmp"):
            root.mkdir(mode=0o700)
        marker, secret = workspace / "marker", outside / "secret"
        marker.write_text("workspace-ok")
        secret.write_text("synthetic-secret")
        (workspace / "outside-link").symlink_to(secret)
        probe = replace(policy, workspace=workspace, scratch=scratch)
        env = isolated_environment(probe)
        python = str(Path(sys.executable).resolve())

        async def run(argv):
            return await _run(sandbox.backend.wrap(argv, probe), env=env, cwd=workspace)

        code, text = await run(["/bin/sh", "-c", "printf shell-ok"])
        checks["shell_starts"] = code == 0 and text == "shell-ok"
        if not checks["shell_starts"]:
            return SandboxSupport(False, sandbox.backend.name, checks, "isolated shell failed to start")
        code, text = await run([python, "-I", "-c", "print('python-ok')"])
        checks["python_starts"] = code == 0 and text.strip() == "python-ok"
        code, text = await run(["/bin/cat", str(marker)])
        checks["workspace_read"] = code == 0 and text == "workspace-ok"
        target = workspace / "written"
        code, _ = await run(["/bin/sh", "-c", f"printf ok > {shlex.quote(str(target))}"])
        checks["workspace_write"] = code == 0 and target.read_text() == "ok"
        for name, path in (("other_project_read_denied", secret), ("symlink_read_denied", workspace / "outside-link")):
            code, text = await run(["/bin/cat", str(path)])
            checks[name] = code != 0 and "synthetic-secret" not in text
        source = Path(__file__).resolve()
        code, _ = await run(["/bin/cat", str(source)])
        checks["source_read_denied"] = source.is_file() and code != 0
        alias = Path("/System/Volumes/Data") / str(secret).lstrip("/")
        if alias.exists():
            code, text = await run(["/bin/cat", str(alias)])
            checks["data_volume_alias_denied"] = code != 0 and "synthetic-secret" not in text
        denied = outside / "written"
        code, _ = await run(["/bin/sh", "-c", f"printf no > {shlex.quote(str(denied))}"])
        checks["outside_write_denied"] = code != 0 and not denied.exists()
        code, _ = await run(["/bin/sh", "-c", 'printf ok > "$HOME/probe"; printf ok > "$TMPDIR/probe"'])
        checks["private_home_tmp"] = code == 0 and (scratch / "home/probe").read_text() == "ok"
        # A synthetic credential proves the whole spawn environment is replaced;
        # never load or print a real key for this check.
        code, text = await _run([python, "-I", "-c",
            "import os; print('control' if os.environ.get('CHAGENT_PROBE_API_KEY') == 'synthetic-only' else 'missing')"],
            env={**os.environ, "CHAGENT_PROBE_API_KEY": "synthetic-only"}, cwd=workspace)
        control_ok = code == 0 and text.strip() == "control"
        code, text = await run([python, "-I", "-c",
            "import os; print('clean' if 'CHAGENT_PROBE_API_KEY' not in os.environ and 'PYTHONPATH' not in os.environ else 'leaked')"])
        checks["environment_clean"] = control_ok and code == 0 and text.strip() == "clean"
        code, _ = await run(["/bin/ln", str(secret), str(workspace / "hardlink")])
        checks["outside_hardlink_denied"] = code != 0 and not (workspace / "hardlink").exists()
        sibling = await asyncio.create_subprocess_exec("/bin/sleep", "30")
        try:
            code, _ = await run(["/bin/kill", "-TERM", str(sibling.pid)])
            await asyncio.sleep(0.03)
            checks["foreign_signal_denied"] = code != 0 and sibling.returncode is None
        finally:
            with suppress(ProcessLookupError):
                sibling.kill()
            await sibling.wait()
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            # A host positive control prevents a dead listener from passing denial.
            control = socket.create_connection(("127.0.0.1", port), 1)
            control.close()
            code, text = await run([python, "-I", "-c",
                f"import socket; socket.create_connection(('127.0.0.1', {port}), 1)"])
            checks["network_denied"] = checks["python_starts"] and code != 0 and "PermissionError" in text
        except OSError:
            checks["network_denied"] = False
        finally:
            listener.close()
    failed = [key for key, ok in checks.items() if not ok]
    return SandboxSupport(not failed, sandbox.backend.name, checks,
                          "failed isolated checks: " + ", ".join(failed) if failed else "")
