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


class LauncherConfigTests(unittest.TestCase):
    def test_main_loads_dotenv_without_logging_key(self):
        import contextlib,io,os,tempfile
        from pathlib import Path
        from unittest.mock import patch
        from chagent.web.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'.env'
            path.write_text('DEEPSEEK_API_KEY=synthetic-secret\nCHAGENT_DATABASE_URL=postgresql:///custom\n'
                            'CHAGENT_EXECUTION_PROFILE=containerized\nCHAGENT_CONTAINER_DECLARED=true\n'
                            'CHAGENT_ALLOW_HOST_COMMANDS=true\nCHAGENT_SANDBOX_NETWORK=true\n')
            captured=io.StringIO()
            with patch.dict(os.environ,{},clear=True),patch('sys.argv',['chagent','--env-file',str(path),'--workspace',tmp,'--no-open']),patch('chagent.web.policy.Path.exists',return_value=True),patch('chagent.web.__main__.resolve_port',return_value=(8765,False)),patch('chagent.web.__main__.create_app') as create,patch('chagent.web.__main__.uvicorn.run'),contextlib.redirect_stdout(captured):
                main()
            self.assertEqual(create.call_args.kwargs['settings'].database_url,'postgresql:///custom')
            self.assertEqual(create.call_args.kwargs['model_settings'].provider,'deepseek')
            self.assertNotIn('synthetic-secret',captured.getvalue())

    def test_relaunch_reports_actual_service_without_claiming_new_settings(self):
        import contextlib,io,tempfile
        from pathlib import Path
        from unittest.mock import patch,MagicMock
        from chagent.web.__main__ import main
        captured=io.StringIO();response=MagicMock();response.json.return_value={'behavior':'actual-work-only'}
        with tempfile.TemporaryDirectory() as tmp,patch('sys.argv',['chagent','--env-file',str(Path(tmp)/'missing'),'--no-open']),patch('chagent.web.__main__.resolve_port',return_value=(8765,True)),patch('chagent.web.__main__.httpx.get',return_value=response),patch('chagent.web.__main__.uvicorn.run') as serve,contextlib.redirect_stdout(captured):
            main()
        serve.assert_not_called()
        self.assertIn('actual-work-only',captured.getvalue())
        self.assertIn('新的环境设置不会生效',captured.getvalue())
