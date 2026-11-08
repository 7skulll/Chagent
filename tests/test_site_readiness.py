"""B5：`site(start)` 必须等到站点自己在应答，而不是 worker 自己在跑。

实测证据：一次真实 run 里 `site(probe)` 失败为 `HTTP probe failed: ReadTimeout`。
原因是 worker 用自己的代理端口绑定成功就写 `state=running`，而它管的子进程还在
import；这段时间里请求会打到代理上，代理连不上上游就直接关连接——调用方看到的是
一个 10 秒读超时，而不是"还没起来"。
"""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from chagent.tools.context import ToolContext
from chagent.tools.sandbox import Sandbox, SandboxSettings
from chagent.tools.site import SiteTool


class _NotFound(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serving() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NotFound)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class AnsweringTests(unittest.IsolatedAsyncioTestCase):
    async def test_any_http_answer_counts_as_ready(self):
        server, port = _serving()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # 404 也意味着子进程已经绑定端口并处理请求。
        self.assertTrue(await SiteTool._answering(f"http://127.0.0.1:{port}"))

    async def test_closed_port_is_not_ready(self):
        self.assertFalse(await SiteTool._answering(f"http://127.0.0.1:{_closed_port()}"))


class StartWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
        self.tool = SiteTool(self.sandbox, None)
        self.context = ToolContext(Path(self.temporary.name))

    async def test_start_returns_only_after_the_site_answers(self):
        with patch.object(self.sandbox, "ensure_container", AsyncMock(return_value="chagent-ws-test")), \
             patch.object(self.tool, "_state",
                           AsyncMock(side_effect=[None, {"state": "running"}, {"state": "running"}])), \
             patch.object(self.tool, "_port", AsyncMock(return_value=1234)), \
             patch.object(self.tool, "_answering", AsyncMock(side_effect=[False, True])) as answering, \
             patch.object(self.tool, "_save", AsyncMock()) as save, \
             patch.object(self.tool, "_saved_workdir", AsyncMock(return_value=None)), \
             patch.object(self.sandbox, "_docker", AsyncMock(return_value=(0, ""))):
            result = await self.tool.execute("call", {"action": "start", "command": "python3 app.py",
                                                      "port": 8000}, context=self.context)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertIn("answering", result.content[0].text)
        self.assertEqual(answering.await_count, 2)
        self.assertEqual(save.await_args_list[-1].args[-1], "running")

    async def test_start_reports_failure_when_the_child_dies(self):
        with patch.object(self.sandbox, "ensure_container", AsyncMock(return_value="chagent-ws-test")), \
             patch.object(self.tool, "_state",
                           AsyncMock(side_effect=[None, {"state": "failed"}])), \
             patch.object(self.tool, "_port", AsyncMock(return_value=1234)), \
             patch.object(self.tool, "_answering", AsyncMock(return_value=False)) as answering, \
             patch.object(self.tool, "_save", AsyncMock()), \
             patch.object(self.tool, "_saved_workdir", AsyncMock(return_value=None)), \
             patch.object(self.sandbox, "_docker", AsyncMock(return_value=(0, ""))):
            result = await self.tool.execute("call", {"action": "start", "command": "false",
                                                      "port": 8000}, context=self.context)
        self.assertTrue(result.is_error)
        self.assertIn("did not reach running", result.content[0].text)
        self.assertEqual(answering.await_count, 0)


if __name__ == "__main__":
    unittest.main()

class ProbeTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """探测超时要说清"等多久、怎么办"，而不是一个裸 ReadTimeout。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
        self.tool = SiteTool(self.sandbox, None)
        self.context = ToolContext(Path(self.temporary.name))

    async def test_a_silent_endpoint_reports_the_budget_not_the_exception(self):
        # 接受连接但永不应答：这正是"站点在跑、这一页很慢"的形状。
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        self.addCleanup(listener.close)

        with patch.object(self.sandbox, "ensure_container", AsyncMock(return_value="chagent-ws-test")), \
             patch.object(self.tool, "_state", AsyncMock(return_value={"state": "running"})), \
             patch.object(self.tool, "_port", AsyncMock(return_value=port)), \
             patch.object(self.tool, "_reconcile", AsyncMock()), \
             patch.object(self.sandbox, "_docker", AsyncMock(return_value=(0, ""))):
            result = await self.tool.execute("call", {"action": "probe", "path": "/api/board",
                                                      "timeout_s": 1}, context=self.context)
        self.assertTrue(result.is_error)
        text = result.content[0].text
        self.assertIn("within 1s", text)
        self.assertIn("Raise timeout_s", text)
        self.assertIn("site(logs)", text)
        self.assertEqual(result.details["timeout_s"], 1)


if __name__ == "__main__":
    unittest.main()

class WorkdirTests(unittest.IsolatedAsyncioTestCase):
    """站点重启必须回到它自己的目录，而不是工作区根。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "semis-ai-dashboard").mkdir()
        self.sandbox = Sandbox(SandboxSettings(containerized=True, host_controller=True))
        self.tool = SiteTool(self.sandbox, None)
        self.context = ToolContext(self.root)

    async def test_start_falls_back_to_the_saved_directory_when_the_state_file_is_gone(self):
        # 容器被重建后 /tmp 状态文件没了，只有数据库那一行还在。
        with patch.object(self.sandbox, "ensure_container", AsyncMock(return_value="chagent-ws-test")), \
             patch.object(self.tool, "_state", AsyncMock(side_effect=[None, None, {"state": "running"}, {"state": "running"}])), \
             patch.object(self.tool, "_saved_workdir", AsyncMock(return_value="semis-ai-dashboard")), \
             patch.object(self.tool, "_port", AsyncMock(return_value=1234)), \
             patch.object(self.tool, "_answering", AsyncMock(return_value=True)), \
             patch.object(self.tool, "_save", AsyncMock()) as save, \
             patch.object(self.sandbox, "_docker", AsyncMock(return_value=(0, ""))) as docker:
            result = await self.tool.execute("call", {"action": "start", "command": "python3 server.py 8000",
                                                      "port": 8000}, context=self.context)
        self.assertFalse(result.is_error, result.content[0].text)
        starting = [call for call in save.await_args_list if call.args[-1] == "starting"]
        self.assertEqual(len(starting), 1)
        self.assertEqual(starting[0].kwargs["workdir"], "semis-ai-dashboard")
        launches = [call for call in docker.await_args_list if "--detach" in call.args]
        self.assertEqual(len(launches), 1)
        argv = list(launches[0].args)
        self.assertEqual(Path(argv[argv.index("--workdir") + 1]).resolve(),
                         (self.root / "semis-ai-dashboard").resolve())

    async def test_an_explicit_workdir_still_wins(self):
        with patch.object(self.sandbox, "ensure_container", AsyncMock(return_value="chagent-ws-test")), \
             patch.object(self.tool, "_state", AsyncMock(side_effect=[None, None, {"state": "running"}, {"state": "running"}])), \
             patch.object(self.tool, "_saved_workdir", AsyncMock(return_value="ignored")), \
             patch.object(self.tool, "_port", AsyncMock(return_value=1234)), \
             patch.object(self.tool, "_answering", AsyncMock(return_value=True)), \
             patch.object(self.tool, "_save", AsyncMock()) as save, \
             patch.object(self.sandbox, "_docker", AsyncMock(return_value=(0, ""))):
            await self.tool.execute("call", {"action": "start", "command": "python3 app.py", "port": 8000,
                                              "workdir": str(self.root)}, context=self.context)
        starting = [call for call in save.await_args_list if call.args[-1] == "starting"]
        self.assertEqual(starting[0].kwargs["workdir"], ".")


if __name__ == "__main__":
    unittest.main()
