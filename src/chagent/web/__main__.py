"""Start a single loopback web worker with server-owned model credentials."""

import argparse
import os
from pathlib import Path

import uvicorn

from chagent.config import Settings
from chagent.models.settings import ModelSettings
from chagent.web.app import create_app


def main():
    parser = argparse.ArgumentParser(description="Chagent local web chat")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="Use the deterministic offline model")
    parser.add_argument("--provider", choices=("openai", "deepseek", "anthropic", "qwen", "custom"))
    parser.add_argument("--model")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if args.demo and (args.provider or args.model):
        parser.error("Demo cannot be combined with provider/model")
    environment = dict(os.environ)
    if args.provider:
        environment["CHAGENT_PROVIDER"] = args.provider
    if args.model:
        environment["CHAGENT_MODEL"] = args.model
    try:
        selected = None if args.demo else ModelSettings.from_env(environment)
        app = create_app(workspace=args.workspace, settings=Settings.from_env(), model_settings=selected, demo=args.demo)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    # Bound the wait for open SSE responses before lifespan cancels agent tasks.
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
