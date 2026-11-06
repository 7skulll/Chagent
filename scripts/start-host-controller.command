#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
workspace="${1:?Usage: start-host-controller.command /absolute/workspace [port]}"
port="${2:-8765}"
if [[ "$workspace" != /* || ! -d "$workspace" ]]; then
  print -u2 'Workspace must be an existing absolute directory.'
  exit 2
fi
cd "$project_dir"
docker compose up -d db
export CHAGENT_HOST_DB_COMPOSE=true
export PYTHONPATH=src
exec .venv/bin/python -m chagent.web --host-controller --workspace "$workspace" --port "$port"
