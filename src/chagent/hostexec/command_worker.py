"""Linux invocation supervisor, sent as source to the executor's Python.

Subreaping keeps double-forked children owned by this invocation. This is process
lifecycle management, not a security boundary against hostile same-user code.
"""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def identity(pid):
    parts = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
    return parts[19], int(parts[1]), parts[0]


def descendants():
    parents = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                start, parent, state = identity(int(entry.name))
                parents[int(entry.name)] = (parent, start, state)
            except (OSError, ValueError, IndexError):
                pass
    owned = {os.getpid()}
    while True:
        more = {pid for pid, (parent, _, _) in parents.items() if parent in owned}
        if more <= owned:
            break
        owned |= more
    return [(pid, parents[pid][1]) for pid in owned if pid != os.getpid()]


def terminate_children():
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        children = descendants()
        if not children:
            return
        for pid, start in children:
            try:
                if identity(pid)[0] == start:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except FileNotFoundError:
                pass
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        time.sleep(.02)
    if descendants():
        raise RuntimeError('invocation descendants survived cleanup')


def main():
    action, directory = sys.argv[1:3]
    folder = Path(directory)
    folder.parent.parent.mkdir(mode=0o700, exist_ok=True)
    folder.parent.mkdir(mode=0o700, exist_ok=True)
    folder.mkdir(mode=0o700, exist_ok=True)
    state_path = folder / 'state.json'
    with (folder / 'lock').open('a') as lock:
        if action == 'stop':
            fcntl.flock(lock, fcntl.LOCK_EX)
            (folder / 'cancel').touch()
            if not state_path.exists():
                # A worker racing startup observes cancel under the same lock.
                return
            state = json.loads(state_path.read_text())
            if state.get('phase') == 'done':
                return
            try:
                start, _, process_state = identity(state['pid'])
                if start != state['start'] or process_state == 'Z':
                    raise RuntimeError('supervisor identity lost before cleanup confirmation')
                os.kill(state['pid'], signal.SIGTERM)
            except ProcessLookupError as error:
                raise RuntimeError('supervisor exited without cleanup confirmation') from error
            fcntl.flock(lock, fcntl.LOCK_UN)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if json.loads(state_path.read_text()).get('phase') == 'done':
                    return
                time.sleep(.05)
            raise RuntimeError('container invocation cleanup not confirmed')

        stopped = False

        def stop(*_):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        # Linux PR_SET_CHILD_SUBREAPER; fail before executing user code if absent.
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'cannot enable subreaper')
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (folder / 'cancel').exists():
            return
        state = {'pid': os.getpid(), 'start': identity(os.getpid())[0], 'phase': 'running'}

        def save():
            tmp = folder / 'state.tmp'
            tmp.write_text(json.dumps(state))
            tmp.replace(state_path)

        save()
        timeout = float(sys.argv[3])
        child = subprocess.Popen(sys.argv[4:], start_new_session=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        deadline = time.monotonic() + timeout
        expired = False
        try:
            while child.poll() is None and not stopped:
                if time.monotonic() >= deadline:
                    expired = True
                    break
                time.sleep(.02)
        finally:
            terminate_children()
        code = child.poll()
        state.update(phase='done', exit_code=code, timed_out=expired, cancelled=stopped)
        save()
        sys.exit(124 if expired else 143 if stopped else code if code is not None and code >= 0 else 1)


if __name__ == '__main__':
    main()
