"""Per-process runtime: the runner, the open workspaces, and the live streams.

Separate from the HTTP layer so request dependencies can name the type without
importing the module that assembles the application.
"""

import asyncio
import logging
from pathlib import Path
from uuid import UUID, uuid4

from starlette.exceptions import HTTPException

from chagent.agent.runner import AgentRunner, RunFailedError
from chagent.boundaries import validate_workspace
from chagent.config import Settings
from chagent.domain.summaries import CompactionPolicy
from chagent.sessions.errors import SessionNotFoundError
from chagent.sessions.manager import SessionManager
from chagent.storage.approvals import ApprovalRepository
from chagent.web.policy import validate_web_runner
from chagent.storage.workspace_identity import ensure_workspace

logger = logging.getLogger(__name__)


class WebRuntime:
    def __init__(self, runner: AgentRunner, workspace: Path, settings: Settings | None = None,
                 compaction: CompactionPolicy | None = None, *, programmable: bool | None = None):
        self.runner = runner
        self.workspace = validate_workspace(workspace)
        # Resolved once: the mode gate below is a policy decision, and reading it
        # per request could let the environment change under a running server.
        self.settings = settings or Settings()
        self.programmable = (settings is not None) if programmable is None else programmable
        if self.programmable:
            validate_web_runner(self.settings, runner)
        self.compaction = compaction
        self.sessions = SessionManager(runner.conversations)
        self.approvals = ApprovalRepository(runner.runs.database, runner.runs)
        # Workspaces the user has opened in this process. A session outside this
        # set is not addressable, so a stale tab cannot act across workspaces it
        # never selected. The startup directory is open by construction.
        self.roots: set[Path] = {self.workspace}
        self.tasks: set[asyncio.Task] = set()
        self.closing = False
        self._workspace_ids: dict[Path, UUID] = {}
        self.auth_guard = None
        if self.runner.sandbox is not None and self.settings.execution_profile == "host-controller":
            self.runner.sandbox.database = self.runner.runs.database
        self._recovery_task: asyncio.Task | None = None
        self._scratch_task: asyncio.Task | None = None
        #: How many browser streams are attached to each run. A count, not a
        #: flag: two tabs on one run means closing one is not "nobody is left".
        self.viewers: dict[UUID, int] = {}
        # The broker asks this before letting a question wait for a human who
        # may have closed the browser; the runtime is the only object that sees
        # streams come and go.
        broker = getattr(getattr(runner, "scheduler", None), "approvals", None)
        if broker is not None:
            broker.audience = self.has_viewer

    def watch(self, run_id: UUID) -> None:
        self.viewers[run_id] = self.viewers.get(run_id, 0) + 1

    def unwatch(self, run_id: UUID) -> None:
        remaining = self.viewers.get(run_id, 0) - 1
        if remaining > 0:
            self.viewers[run_id] = remaining
        else:
            self.viewers.pop(run_id, None)

    def has_viewer(self, run_id: UUID) -> bool:
        return self.viewers.get(run_id, 0) > 0

    def open_workspace(self, raw: str) -> Path:
        """Resolve one directory and add it to the open set."""
        try:
            target = validate_workspace(Path(raw))
        except (OSError, RuntimeError):
            raise ValueError("工作区目录不存在") from None
        if self.auth_guard is not None:
            self.auth_guard(target)
        self.roots.add(target)
        return target

    def root(self, raw: str | None) -> Path:
        """The workspace a request is about; ``None`` means the startup one."""
        if raw is None or not raw.strip():
            return validate_workspace(self.workspace)
        target = Path(raw).expanduser().resolve(strict=False)
        if target not in self.roots:
            raise ValueError("该工作区尚未打开")
        return validate_workspace(target)

    async def workspace_id(self, workspace: Path | None = None):
        root = self.workspace if workspace is None else workspace
        if self.settings.execution_profile == "host-controller":
            if root == Path("/workspaces") or root.is_relative_to(Path("/workspaces")):
                raise HTTPException(409, "旧容器工作区路径必须先核实原宿主目录并重新绑定")
            root = validate_workspace(root)
            if self.auth_guard is not None:
                self.auth_guard(root)
            stat = root.stat()
            async with self.runner.conversations.database.transaction() as connection:
                cursor = await connection.execute(
                    "INSERT INTO workspaces (id, root_path) VALUES (%s, %s) ON CONFLICT (root_path) "
                    "DO UPDATE SET root_path = EXCLUDED.root_path RETURNING id", (uuid4(), str(root)))
                identity = (await cursor.fetchone())["id"]
                cursor = await connection.execute(
                    "INSERT INTO workspace_bindings (workspace_id, host_path, device, inode, state) "
                    "VALUES (%s,%s,%s,%s,'active') ON CONFLICT (workspace_id) DO UPDATE SET "
                    "updated_at=now() WHERE workspace_bindings.host_path=EXCLUDED.host_path "
                    "AND workspace_bindings.device=EXCLUDED.device "
                    "AND workspace_bindings.inode=EXCLUDED.inode "
                    "AND workspace_bindings.state='active' RETURNING workspace_id",
                    (identity, str(root), stat.st_dev, stat.st_ino))
                if await cursor.fetchone() is None:
                    raise HTTPException(409, "工作区文件系统身份已变化；执行已暂停，需显式重新绑定")
            self._workspace_ids[root] = identity
            return identity
        if root not in self._workspace_ids:
            async with self.runner.conversations.database.connection() as connection:
                self._workspace_ids[root] = await ensure_workspace(connection, root)
        return self._workspace_ids[root]

    async def memory_scope(self, raw: str | None):
        """The workspace UUID memory rows are scoped to, for one request."""
        return await self.workspace_id(self.root(raw))

    async def session(self, identity: UUID):
        try:
            session = await self.sessions.get(identity)
        except (ValueError, OSError):
            raise SessionNotFoundError("Session workspace is no longer available") from None
        if session.workspace not in self.roots:
            raise SessionNotFoundError("Session belongs to a workspace that is not open")
        await self.require_programming(session.workspace)
        return session

    async def require_programming(self, workspace: Path) -> None:
        if not self.programmable:
            return
        validate_web_runner(self.settings, self.runner)
        if self.settings.execution_profile == "host-controller":
            await self.workspace_id(workspace)
        # Explicit preflight owner, separate from all conversation runs.
        support = await self.runner.sandbox.audit(workspace, owner="web-preflight")
        if not support["available"]:
            raise HTTPException(503, "执行隔离探针失败，code 不可用；不会降低隔离等级")

    async def run(self, identity: UUID):
        run = await self.runner.runs.get(identity)
        if run is None:
            raise HTTPException(404, "Run does not exist")
        await self.session(run["session_id"])
        # A waiting run keeps status=running; the projection tells the UI why.
        return {**run, **await self.approvals.waiting_for(identity)}

    async def start(self, session_id: UUID, text: str, *, supersede_from: int | None = None) -> UUID:
        if self.closing:
            raise HTTPException(503, "Server is shutting down")
        started = asyncio.get_running_loop().create_future()

        async def drive():
            try:
                await self.runner.run(session_id, text, supersede_from=supersede_from,
                                      on_started=started.set_result)
            except asyncio.CancelledError:
                if not started.done():
                    started.cancel()
                raise
            except Exception as error:
                if not started.done():
                    started.set_exception(error)
                elif not isinstance(error, RunFailedError):
                    # Avoid logging arbitrary exception strings that could contain credentials.
                    logger.error("Background run failed (%s)", type(error).__name__)

        task = asyncio.create_task(drive(), name=f"web-run-{session_id}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        # Request disconnection must not cancel the durable agent task.
        return await asyncio.shield(started)

    async def _recover_runs(self) -> None:
        while True:
            try:
                while await self.runner.runs.reap_expired():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Run recovery failed (%s)", type(error).__name__)
            await asyncio.sleep(5)

    async def _reap_scratch(self) -> None:
        # The first pass runs after the run recovery loop has had a chance to
        # mark expired owners. A nonterminal run is never removed in any case.
        await asyncio.sleep(5)
        first_pass = True
        while True:
            try:
                await self.runner.sandbox.reap_terminal_scratch(self.runner.runs.database)
                if first_pass:
                    # Give the diagnostic inventory one GC interval to expose
                    # candidates before any orphan container is deleted.
                    first_pass = False
                else:
                    await self.runner.sandbox.reap_orphan_containers(self.runner.runs.database)
                await self.runner.sandbox.reap_idle_containers(
                    self.runner.runs.database, self.settings.container_idle_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Run scratch recovery failed (%s)", type(error).__name__)
            await asyncio.sleep(300)

    def start_worker(self) -> None:
        """Start recovery and extraction while the application is serving."""
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._recover_runs(), name="run-recovery")
        if (self.runner.sandbox is not None and self.runner.sandbox.settings.host_controller
                and (self._scratch_task is None or self._scratch_task.done())):
            self._scratch_task = asyncio.create_task(self._reap_scratch(), name="scratch-recovery")
        if getattr(self.runner, "memory_worker", None) is not None:
            self.runner.memory_worker.start()

    async def close(self):
        self.closing = True
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            await asyncio.gather(self._recovery_task, return_exceptions=True)
            self._recovery_task = None
        if self._scratch_task is not None:
            self._scratch_task.cancel()
            await asyncio.gather(self._scratch_task, return_exceptions=True)
            self._scratch_task = None
        for task in self.tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # A post-turn pass belongs to a run that already answered, so it is not
        # cancelled above; give it a bounded chance to land before the pool goes.
        if self.runner.context_manager is not None:
            await self.runner.context_manager.drain_post_turn()
        # Queued jobs survive shutdown: an interrupted job is returned to the
        # queue and picked up by the next process.
        if getattr(self.runner, "memory_worker", None) is not None:
            await self.runner.memory_worker.stop()
        if self.runner.sandbox is not None:
            self.runner.sandbox.close()
