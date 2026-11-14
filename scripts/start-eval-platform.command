#!/bin/zsh
set -euo pipefail
chagent_eval_root="${0:A:h:h}"
cd "$chagent_eval_root"
export PYTHONPATH="$chagent_eval_root/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$chagent_eval_root/.venv/bin/python" -m eval.web "$@"
