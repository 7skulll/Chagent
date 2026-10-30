"""Start the local web worker; this is the default way to run Chagent.

One loopback process owns the port. A second launch does not start a twin: it
finds the running instance, opens the browser and exits. If the preferred port
is taken by something else, the next free one is used, so the failure mode is
"a different URL", never "address already in use".
"""

import argparse
from contextlib import closing
import os
from pathlib import Path
import socket
import threading
import webbrowser

import httpx
import uvicorn

from chagent.config import Settings
from chagent.diagnostics.behavior import describe as describe_behavior
from chagent.domain.summaries import CompactionPolicy
from chagent.models.settings import ModelSettings
from chagent.web.app import create_app


HOST = "127.0.0.1"
DEFAULT_PORT = 8765
#: Consecutive ports tried when the preferred one is taken by another program.
PORT_ATTEMPTS = 10
PROBE_TIMEOUT = 0.6


def base_url(port: int) -> str:
    return f"http://{HOST}:{port}"


def is_free(port: int) -> bool:
    """True when a listener can bind this port right now."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def running_here(port: int) -> bool:
    """True only for *this* app: a stranger on the port must not be adopted.

    ``trust_env`` matters: an ambient HTTP proxy would otherwise swallow the
    loopback request and the answer would come from somewhere else entirely.
    """
    try:
        response = httpx.get(f"{base_url(port)}/api/info", timeout=PROBE_TIMEOUT, trust_env=False)
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and {"workspace", "provider", "model"} <= set(payload)


def resolve_port(preferred: int) -> tuple[int, bool]:
    """Return ``(port, already_running)``.

    ``already_running`` means the bound port answers as Chagent, so the caller
    should open a browser instead of starting a second worker.
    """
    if is_free(preferred):
        return preferred, False
    if running_here(preferred):
        return preferred, True
    for candidate in range(preferred + 1, preferred + PORT_ATTEMPTS):
        if is_free(candidate):
            return candidate, False
    raise SystemExit(f"端口 {preferred}-{preferred + PORT_ATTEMPTS - 1} 都被占用，请用 --port 指定其他端口")


def open_later(url: str, delay: float = 0.8) -> None:
    """Open after the server is listening; a browser opened first shows an error."""
    threading.Timer(delay, webbrowser.open, args=(url,)).start()


def main():
    parser = argparse.ArgumentParser(prog="chagent", description="Chagent 本地网页（默认入口）")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="起始工作区，之后可在网页内切换")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--demo", action="store_true", help="Use the deterministic offline model")
    parser.add_argument("--provider", choices=("openai", "deepseek", "anthropic", "qwen", "custom"))
    parser.add_argument("--model")
    parser.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if args.demo and (args.provider or args.model):
        parser.error("Demo cannot be combined with provider/model")

    port, already = resolve_port(args.port)
    url = base_url(port)
    if already:
        print(f"Chagent 已在运行：{url}", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        return

    environment = dict(os.environ)
    if args.provider:
        environment["CHAGENT_PROVIDER"] = args.provider
    if args.model:
        environment["CHAGENT_MODEL"] = args.model
    try:
        settings = Settings.from_env()
        compaction = CompactionPolicy.from_env()
        selected = None if args.demo else ModelSettings.from_env(environment)
        app = create_app(workspace=args.workspace, settings=settings, model_settings=selected,
                         demo=args.demo)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    # Ambient variables win over --env-file, so name the resolved pair once.
    if selected is not None:
        print(f"Resolved: {selected.describe()}", flush=True)
    print(f"Behavior: {describe_behavior(settings, compaction)}", flush=True)
    print(f"Chagent 网页：{url}（工作区 {Path(args.workspace).expanduser().resolve()}）", flush=True)
    if not args.no_open:
        open_later(url)
    # Bound the wait for open SSE responses before lifespan cancels agent tasks.
    uvicorn.run(app, host=HOST, port=port, workers=1, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
