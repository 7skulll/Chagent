"""Per-process runtime: the runner, the open workspaces, and the live streams.

Separate from the HTTP layer so request dependencies can name the type without
importing the module that assembles the application.
"""

import asyncio
import logging
import re
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

#: Longest probe reason carried into the 503. Docker quotes multi-line daemon
#: output, and the banner is the only place a person sees it.
PROBE_REASON_LIMIT = 240

#: The one probe failure with a fix a person can apply without reading Docker
#: output. Docker names the missing image itself, so quote it back.
MISSING_IMAGE = re.compile(r"Unable to find image '([\w][\w./:@-]{0,80})' locally")


def probe_reason(support: dict) -> str:
    """Only the cause, without the "code is off" wrapper.

    Two places need this sentence and disagree about the wrapper: the 503 puts
    it in parentheses, the status line shows it after a colon. Parsing one out
    of the other would make the wording load-bearing.
    """
    detail = " ".join(str(support.get("detail") or "").split())
    if not detail:
        return ""
    missing = MISSING_IMAGE.search(detail)
    if missing is not None:
        image = missing.group(1)
        return f"执行器镜像 {image} 在本机不存在，先运行 docker build -t {image} ."
    suffix = "…" if len(detail) > PROBE_REASON_LIMIT else ""
    return f"{detail[:PROBE_REASON_LIMIT]}{suffix}"


def isolation_failure(support: dict) -> str:
    """The 503 a failed preflight raises, including the probe's own reason.

    The banner used to say only "unavailable", which reads as a permission
    problem, while the cause worth acting on sat unused in ``detail`` — "Unable
    to find image 'chagent:local' locally" names a missing executor image, not
    an account. That case is translated into the command that fixes it; every
    other reason is flattened to one line and bounded, because Docker quotes
    multi-line daemon output verbatim.
    """
    base = "执行隔离探针失败，code 不可用；不会降低隔离等级"
    reason = probe_reason(support)
    return base if not reason else f"{base}（原因：{reason}）"


class WebRuntime:
    def __init__(self, runner: AgentRunner, workspace: Path, settings: Settings | None = None,
                 compaction: CompactionPolicy | None = None, *, programmable: bool | None = None, native_allowed: bool = True):
        self.runner = runner
        self.native_allowed = native_allowed
        self.workspace = validate_workspace(workspace)
        # Resolved once: the mode gate below is a policy decision, and reading it
        # per request could let the environment change under a running server.
        self.settings = settings or Settings()
        self.programmable = (settings is not None) if programmable is None else programmable
        if self.programmable:
            validate_web_runner(self.settings, runner, native_allowed=self.native_allowed)
        self.compaction = compaction
        self.sessions = SessionManager(runner.conversations)
        self.approvals = ApprovalRepository(runner.runs.database, runner.runs)
        # Workspaces the user has opened in this process. A session outside this
        # set is not addressable, so a stale tab cannot act across workspaces it
        # never selected. The startup directory is open by construction.
        self.roots: set[Path] = {self.workspace}
        #: Last answer the executor probe gave, for the page to read. ``None``
        #: until the first check. Kept as data rather than an exception because
        #: "code is off" describes the machine, not the request that noticed.
        self.isolation: dict | None = None
        #: Executor state per workspace: ``preparing`` → ``ready``/``unavailable``.
        #: A container belongs to one directory, so one directory's answer must
        #: not stand for another's; ``isolation`` above stays as "the last one
        #: this page asked about" for the single-workspace compatibility field.
        self.executors: dict[Path, dict] = {}
        #: Preparation in flight, so opening the same directory twice joins the
        #: first attempt instead of racing a second ``docker run`` against it.
        self._provisioning: dict[Path, asyncio.Task] = {}
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
        if getattr(self, 'account_id', None) and self.auth_guard is not None:
            self.auth_guard(Path(raw).expanduser())
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
            target = validate_workspace(self.workspace)
            if self.auth_guard is not None:
                self.auth_guard(target)
            return target
        target = Path(raw).expanduser().resolve(strict=False)
        if target not in self.roots:
            raise ValueError("该工作区尚未打开")
        target = validate_workspace(target)
        if self.auth_guard is not None:
            self.auth_guard(target)
        return target

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
        """A session this process may read.

        Membership only. The executor probe used to sit here, which made a
        missing Docker image answer 503 to listing messages, opening a file and
        browsing workspaces — everything a person would try in order to find out
        what was wrong. Execution is gated where it starts (run creation, site
        and container management), not on the way to the history.
        """
        try:
            session = await self.sessions.get(identity)
        except (ValueError, OSError):
            raise SessionNotFoundError("Session workspace is no longer available") from None
        if session.workspace not in self.roots:
            raise SessionNotFoundError("Session belongs to a workspace that is not open")
        return session

    def workspace_path(self, identity: UUID) -> Path | None:
        """The open directory one workspace id names, if this process opened it."""
        return next((root for root in self.roots if self._workspace_ids.get(root) == identity), None)

    def executor_state(self, workspace: Path | None = None) -> dict | None:
        """What is known about one workspace's executor; ``None`` means unknown."""
        return self.executors.get(self.workspace if workspace is None else workspace)

    async def check_isolation(self, workspace: Path | None = None) -> dict:
        """Probe now and remember the answer; never refuses anything.

        The page needs to know that code is off *before* someone types a
        request and loses the work to a 503, so this returns the refusal a
        request would have raised instead of raising it. Docker daemon output is
        kept to one bounded line for the same reason it is in the 503.

        Failures that are not about the probe — a workspace whose host identity
        changed, a Docker CLI that is not installed — are reported the same way.
        They used to abort startup, which turned every one of them into a page
        that could not say what was wrong.
        """
        return await self.provision(workspace)

    async def provision(self, workspace: Path | None = None) -> dict:
        """Bring one workspace's executor up and record the outcome.

        The container that runs a directory's commands belongs to that
        directory, but it used to appear inside the first turn: the person
        waited for Docker mid-sentence, and a missing image looked like a broken
        agent. Creating it when the directory is opened moves the wait to an
        empty transcript, and ``preparing`` exists so the page can say so
        instead of blocking.

        Never raises. Every failure here is a fact about the machine to display,
        not a reason to fail the request that asked for it.
        """
        root = self.workspace if workspace is None else workspace
        if not self.programmable:
            state = {"state": "ready", "available": True, "reason": None}
            self.executors[root] = state
            self.isolation = state
            return state
        self.executors[root] = {"state": "preparing", "available": False, "reason": None}
        try:
            # Same order as ``require_programming``: the container bookkeeping
            # is keyed by workspace identity, so the identity is established
            # before a container is inspected or built.
            if self.settings.execution_profile == "host-controller":
                await self.workspace_id(root)
            support = await self.runner.sandbox.audit(root, owner="web-preflight")
        except (HTTPException, OSError, RuntimeError, ValueError) as error:
            detail = getattr(error, "detail", None) or str(error)
            state = {"state": "unavailable", "available": False,
                     "reason": probe_reason({"detail": str(detail)})}
        else:
            available = bool(support["available"])
            state = {"state": "ready" if available else "unavailable", "available": available,
                     "reason": None if available else probe_reason(support)}
        self.executors[root] = state
        self.isolation = state
        return state

    def schedule_provision(self, workspace: Path | None = None) -> dict | None:
        """Start preparing in the background; opening a directory does not wait.

        The open call is a dialog closing, not a place to spend ``docker run``.
        Returns the state as of now — ``preparing`` on the first call, and the
        same attempt's state when the directory is opened again while it runs.
        """
        root = self.workspace if workspace is None else workspace
        if not self.programmable or self.closing:
            return self.executors.get(root)
        running = self._provisioning.get(root)
        if running is not None and not running.done():
            return self.executors.get(root)
        # Recorded before the task starts: a caller that reads the state now, or
        # a second open of the same directory, must see "preparing" rather than
        # "nothing happened yet" while the coroutine is still queued.
        state = {"state": "preparing", "available": False, "reason": None}
        self.executors[root] = state
        task = asyncio.create_task(self.provision(root), name=f"web-provision-{root.name}")
        self._provisioning[root] = task
        self.tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self.tasks.discard(done)
            if self._provisioning.get(root) is done:
                self._provisioning.pop(root, None)
            # ``provision`` swallows its own failures; this is for the ones it
            # cannot know about, and for a task cancelled at shutdown.
            if not done.cancelled() and done.exception() is not None:
                logger.warning("Executor preparation failed (%s)", type(done.exception()).__name__)

        task.add_done_callback(finished)
        return state

    def require_configuration(self) -> None:
        """The cheap half of the preflight: declared axes must agree with reality.

        Separate from the probe because they fail for different reasons and at
        different times. A contradictory configuration is a mistake in the
        settings and is worth refusing at the first request; a missing Docker
        image is a repair someone can make while the page stays open.
        """
        if not self.programmable:
            return
        validate_web_runner(self.settings, self.runner, native_allowed=self.native_allowed)

    async def require_programming(self, workspace: Path) -> None:
        if not self.programmable:
            return
        self.require_configuration()
        if self.settings.execution_profile == "host-controller":
            await self.workspace_id(workspace)
        # Explicit preflight owner, separate from all conversation runs.
        support = await self.runner.sandbox.audit(workspace, owner="web-preflight")
        if not support["available"]:
            reason = probe_reason(support)
            state = {"state": "unavailable", "available": False, "reason": reason}
            self.isolation = {"available": False, "reason": reason}
            self.executors[workspace] = state
            raise HTTPException(503, isolation_failure(support))
        self.isolation = {"available": True, "reason": None}
        self.executors[workspace] = {"state": "ready", "available": True, "reason": None}

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
