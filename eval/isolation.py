"""Kernel-confined controller checks. No unsandboxed fallback."""
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile


#: Runs the workspace's own suite. Kept separate so every caller shares one
#: definition of "the controller's unittest entry point".
UNITTEST = ('import os,sys,unittest;sys.path.insert(0,os.getcwd());'
            'unittest.main(module=None,argv=["unittest","discover","-s","tests"])')


class IsolationUnavailable(RuntimeError):
    pass


def command(workspace: Path, scratch: Path, bootstrap=None) -> list[str]:
    workspace, scratch = workspace.resolve(), scratch.resolve()
    python = str(Path(sys.executable).resolve())
    bootstrap = bootstrap or UNITTEST
    check = [python, '-I', '-S', '-B', '-c', bootstrap]
    if sys.platform == 'darwin' and shutil.which('sandbox-exec'):
        roots = {str(workspace), str(scratch), sys.base_prefix, sys.prefix,
                 '/System', '/usr/lib', '/usr/share/locale', '/usr/share/zoneinfo', '/Library/Apple'}
        forbidden_reads = ' '.join(f'(require-not (subpath {json.dumps(str(Path(root).resolve()))}))'
                                   for root in roots)
        ancestors = {parent for root in roots for parent in Path(root).resolve().parents}
        # Python resolves its runtime via ancestor directory reads; these literals
        # allow listing directories, never reading arbitrary descendant files.
        forbidden_reads += ' ' + ' '.join(
            f'(require-not (literal {json.dumps(str(parent))}))' for parent in ancestors)
        profile = ('(version 1)(allow default)'
                   '(deny network*)(deny appleevent-send)(deny process-fork)(deny process-info*)'
                   '(deny mach-lookup (require-not (global-name "com.apple.system.notification_center")))'
                   f'(deny process-exec (require-not (literal {json.dumps(python)})))'
                   '(deny signal (require-not (target self)))'
                   f'(deny file-read-data (require-all {forbidden_reads} '
                   '(require-not (literal "/dev/null"))(require-not (literal "/dev/urandom"))))'
                   f'(deny file-write* (require-all (require-not (subpath {json.dumps(str(scratch))})) '
                   '(require-not (literal "/dev/null"))))')
        return ['/usr/bin/sandbox-exec', '-p', profile, *check]
    if sys.platform.startswith('linux') and shutil.which('bwrap'):
        cmd = [shutil.which('bwrap'), '--unshare-all', '--die-with-parent', '--new-session']
        roots = {'/usr', '/lib', '/lib64', sys.base_prefix, sys.prefix}
        for root in sorted(roots):
            if Path(root).exists():
                cmd += ['--ro-bind', root, root]
        if Path('/etc/ld.so.cache').exists():
            cmd += ['--ro-bind', '/etc/ld.so.cache', '/etc/ld.so.cache']
        return cmd + ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                      '--ro-bind', str(workspace), '/workspace', '--bind', str(scratch), '/scratch',
                      '--chdir', '/workspace', '--setenv', 'TMPDIR', '/scratch',
                      '--setenv', 'HOME', '/scratch', *check]
    raise IsolationUnavailable('no supported kernel sandbox (sandbox-exec/bwrap); controller did not run')


def unit_tests(workspace: Path, timeout: float = 120):
    """Diagnostic only: candidate stdout cannot authorize a passing verdict."""
    return run_script(workspace, timeout=timeout)


def available() -> bool:
    """Whether this host can actually confine a script, not just name a backend.

    ``sandbox-exec`` exists on every macOS and can still refuse to apply a
    profile (the evaluator itself running sandboxed: exit 71). Tests that assert
    on confined behaviour ask this first, so they skip on a host that cannot
    isolate instead of asserting against an outage.
    """
    try:
        with tempfile.TemporaryDirectory(prefix='chagent-eval-probe-') as root:
            completed = run_script(Path(root), "print('probe-ok')", timeout=30)
    except (IsolationUnavailable, OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == 'probe-ok'


def run_script(workspace: Path, script=None, *, timeout: float = 120):
    """Run controller-owned code with a read-only workspace and clean env."""
    with tempfile.TemporaryDirectory(prefix='chagent-eval-check-') as temporary:
        scratch = Path(temporary).resolve()
        # The first statement writes a per-run token to the pipe, before any
        # candidate file is read. A sandbox that fails to apply (macOS exits 71
        # with "sandbox_apply: Operation not permitted") never reaches Python,
        # so the token is absent and the check is infrastructure, not a failure.
        # Bytes already on the pipe cannot be retracted, so a candidate cannot
        # forge "the sandbox died" and escape the denominator; conversely a
        # working sandbox always emits it, so printing sandbox-shaped errors on
        # stderr cannot be mistaken for an outage.
        handshake = f"controller-started:{secrets.token_hex(16)}"
        bootstrap = (f"import os;os.write(1,{handshake!r}.encode()+b'\\n');"
                     + (UNITTEST if script is None else script))
        cmd = command(workspace, scratch, bootstrap)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(scratch), 'TMPDIR': str(scratch),
               'LANG': 'en_US.UTF-8', 'TZ': 'UTC', 'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen(cmd, cwd=workspace, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=max(.01, timeout))
            first, _, rest = stdout.partition('\n')
            if first != handshake:
                raise IsolationUnavailable(
                    f"the sandbox never started the controller: exit {process.returncode}, "
                    f"no handshake on stdout, stderr {_tail(stderr) or '(empty)'}")
            return subprocess.CompletedProcess(cmd, process.returncode, rest, stderr)
        except subprocess.TimeoutExpired as expired:
            # A sandbox that hangs before Python runs is an outage too: charging
            # it as "the candidate timed out" would blame the arm for the host.
            partial = expired.output or ''
            if isinstance(partial, bytes):
                partial = partial.decode('utf-8', 'replace')
            if handshake not in partial:
                raise IsolationUnavailable(
                    f"the sandbox did not start the controller within {timeout:g}s: "
                    "no handshake on stdout") from expired
            raise
        finally:
            # Descendants retain the sandbox, and cannot survive a completed check.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


def _tail(text: str, limit: int = 200) -> str:
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    return ' … '.join(lines[-2:])[:limit]
