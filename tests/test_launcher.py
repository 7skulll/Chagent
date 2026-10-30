"""Launcher checks: one process owns a port, and a relaunch focuses it.

These use real sockets on loopback. They deliberately do not import the agent:
the point is that deciding where to listen never needs a database.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import threading
import unittest

from chagent.web.__main__ import is_free, resolve_port, running_here


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def serve(payload: dict):
    """A tiny loopback server answering /api/info with ``payload``."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    port = free_port()
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


CHAGENT_INFO = {"workspace": "/tmp", "provider": "openai", "model": "gpt"}


class LauncherTests(unittest.TestCase):
    def test_a_free_port_is_used_as_asked(self):
        port = free_port()
        self.assertTrue(is_free(port))
        self.assertEqual(resolve_port(port), (port, False))

    def test_a_stranger_on_the_port_moves_to_the_next_one(self):
        port = free_port()
        with socket.socket() as stranger:
            stranger.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            stranger.bind(("127.0.0.1", port))
            stranger.listen()
            self.assertFalse(is_free(port))
            chosen, already = resolve_port(port)
        self.assertFalse(already)
        self.assertGreater(chosen, port)

    def test_a_running_chagent_is_adopted_instead_of_starting_a_twin(self):
        server, port = serve(CHAGENT_INFO)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertTrue(running_here(port))
        self.assertEqual(resolve_port(port), (port, True))

    def test_a_different_service_is_not_mistaken_for_chagent(self):
        server, port = serve({"hello": "world"})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertFalse(running_here(port))
        chosen, already = resolve_port(port)
        self.assertFalse(already)
        self.assertNotEqual(chosen, port)


if __name__ == "__main__":
    unittest.main()
