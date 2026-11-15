"""Minimal HTTP app for the delivery under review."""

ROUTES = {
    "GET /status": lambda: {"status": "ok"},
    "GET /version": lambda: {"version": "1.4.2"},
}


def handle(method, path):
    handler = ROUTES.get(f"{method} {path}")
    if handler is None:
        return 404, {"error": "not found"}
    return 200, handler()
