"""Command line entry point for the ledger service."""

from service.routes import ROUTES
from service.store import Ledger

DEFAULT_PORT = 8765


def create_app():
    return {"routes": ROUTES, "ledger": Ledger()}


def main(argv=None):
    port = DEFAULT_PORT
    argv = list(argv or [])
    for index, item in enumerate(argv):
        if item == "--port" and index + 1 < len(argv):
            port = int(argv[index + 1])
    app = create_app()
    print("listening on http://127.0.0.1:%d with %d routes" % (port, len(app["routes"])))


if __name__ == "__main__":
    main()
