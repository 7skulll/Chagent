#!/bin/sh
# One-click start on macOS: double-click in Finder, or run `./scripts/start.command`.
#
# The venv console script is tried first because it is the normal path. When an
# outer sitecustomize takes over the interpreter (editable installs lose their
# .pth), `chagent` cannot import itself, so this falls back to running the
# package with an explicit PYTHONPATH.
set -e
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

if [ -x .venv/bin/python ]; then
  python=.venv/bin/python
else
  python=$(command -v python3)
fi

export PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$python" -m chagent "$@"
