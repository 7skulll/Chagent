"""Host-controlled packet metadata capture for one workspace run."""

import asyncio
import json
import logging
import os
from pathlib import Path
from uuid import UUID

logger = logging.getLogger(__name__)


class NetworkAudit:
    def __init__(self, sandbox, workspace: Path, run_id: UUID):
        self.sandbox = sandbox
        self.workspace = workspace
        self.name = "chagent-net-" + run_id.hex
        self.started = False
        self.ready = False
        self.finished: dict | None = None

    async def start(self) -> bool:
        try:
            workspace_name = await self.sandbox.ensure_container(self.workspace)
            image = os.environ.get("CHAGENT_EXECUTOR_IMAGE", "chagent:local")
            code, _ = await self.sandbox._docker(
                "run", "-d", "--name", self.name, "--network", "container:" + workspace_name,
                "--cap-drop", "ALL", "--cap-add", "NET_RAW", "--read-only",
                "--security-opt", "no-new-privileges", "--pids-limit", "32", "--memory", "64m",
                "--entrypoint", "/app/.venv/bin/python", image,
                "-m", "chagent.hostexec.network_monitor", timeout=15)
            if code:
                await self.sandbox._docker("rm", "-f", self.name, timeout=8)
                return False
            self.started = True
            # Do not admit commands until the capture socket has been bound.
            for _ in range(20):
                code, output = await self.sandbox._docker("logs", self.name)
                if code == 0 and "READY" in output.splitlines():
                    self.ready = True
                    return True
                await asyncio.sleep(.1)
            return False
        except (OSError, RuntimeError) as error:
            logger.warning("Network audit could not start (%s)", type(error).__name__)
            return False

    async def stop(self) -> dict:
        if self.finished is not None:
            return self.finished
        if not self.started:
            self.finished = {"complete": False, "reason": "monitor_unavailable", "targets": [],
                             "sent_bytes": None, "packets": None}
            return self.finished
        try:
            await self.sandbox._docker("stop", "-t", "3", self.name, timeout=8)
            code, output = await self.sandbox._docker("logs", self.name)
            if code:
                raise RuntimeError("Monitor log unavailable")
            data = json.loads(output.splitlines()[-1])
            self.finished = {**data, "complete": self.ready and not data.get("truncated", False)}
            return self.finished
        except (OSError, RuntimeError, ValueError, IndexError) as error:
            logger.warning("Network audit could not finish (%s)", type(error).__name__)
            self.finished = {"complete": False, "reason": type(error).__name__, "targets": [],
                             "sent_bytes": None, "packets": None}
            return self.finished
        finally:
            try:
                await self.sandbox._docker("rm", "-f", self.name, timeout=8)
            except (OSError, RuntimeError):
                logger.warning("Could not remove network audit sidecar")
