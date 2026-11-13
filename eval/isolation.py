"""Kernel-confined controller checks. No unsandboxed fallback."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile


class IsolationUnavailable(RuntimeError):
    pass


def command(workspace: Path, scratch: Path) -> list[str]:
    workspace, scratch = workspace.resolve(), scratch.resolve()
    python = str(Path(sys.executable).resolve())
    bootstrap = ('import os,sys,unittest;sys.path.insert(0,os.getcwd());'
                 'unittest.main(module=None,argv=["unittest","discover","-s","tests"])')
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
    """Workspace is read-only, scratch is disposable, env/network are denied."""
    with tempfile.TemporaryDirectory(prefix='chagent-eval-check-') as temporary:
        scratch = Path(temporary).resolve()
        cmd = command(workspace, scratch)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(scratch), 'TMPDIR': str(scratch),
               'LANG': 'en_US.UTF-8', 'TZ': 'UTC', 'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen(cmd, cwd=workspace, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=max(.01, timeout))
            return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            raise
        finally:
            # Descendants retain the sandbox, and cannot survive a completed check.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
