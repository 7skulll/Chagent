"""Construct the minimal runnable agent from explicit dependencies."""

from chagent.agent.runner import AgentRunner
from chagent.context.builder import BasicContextBuilder
from chagent.context.manager import ContextManager
from chagent.domain.json_types import JsonObject
from chagent.domain.context import ContextPolicy
from chagent.domain.summaries import CompactionPolicy
from chagent.context.tokens import TokenCounter, select_counter
from chagent.memory.worker import MemoryWorker
from chagent.models.base import Model, ModelClient
from chagent.models.fake import FakeModelClient
from chagent.storage.database import Database
from chagent.storage.memory_jobs import MemoryJobRepository
from chagent.storage.repositories import ConversationRepository, RunRepository
from chagent.storage.summaries import SummaryRepository
from chagent.storage.artifacts import ArtifactRepository
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.executor import ToolExecutor
from chagent.tools.processes import ProcessManager
from chagent.tools.sandbox import Sandbox
from chagent.tools.web import WebAccess
from chagent.domain.memories import MemoryPolicy
from chagent.memory.service import MemoryService
from chagent.storage.approvals import ApprovalRepository
from chagent.storage.memories import MemoryRepository
from chagent.tools.approvals import ApprovalBroker
from chagent.tools.policy import ApprovalPolicy


def create_demo_runner(
    database: Database, *, tool_name: str = "echo", tool_arguments: JsonObject | None = None,
    allow_host_commands: bool = False,
    context_policy: ContextPolicy | None = None, token_counter: TokenCounter | None = None,
    compaction_policy: CompactionPolicy | None = None,
    max_turns: int = 32,
    memory_policy: MemoryPolicy | None = None,
    max_tool_concurrency: int = 4,
    memory_worker_concurrency: int = 1,
    sandbox: Sandbox | None = None,
    approval_policy: ApprovalPolicy | None = None,
    approval_timeout: float = 300.0,
    approval_decider=None,
    approval_remote: bool = True,
    process_wall_timeout: float = 600.0,
    max_processes: int = 4,
    web: WebAccess | None = None,
) -> AgentRunner:
    return create_runner(
        database, Model(id="fake-echo", provider="local-test", api="fake", context_window=8192, max_tokens=512),
        FakeModelClient(tool_name, tool_arguments), allow_host_commands=allow_host_commands,
        context_policy=context_policy, token_counter=token_counter, compaction_policy=compaction_policy, timeout=60, max_turns=max_turns,
        memory_policy=memory_policy,
        max_tool_concurrency=max_tool_concurrency,
        memory_worker_concurrency=memory_worker_concurrency,
        sandbox=sandbox,
        approval_policy=approval_policy, approval_timeout=approval_timeout,
        approval_decider=approval_decider, approval_remote=approval_remote,
        process_wall_timeout=process_wall_timeout, max_processes=max_processes,
        web=web,
    )


def create_runner(
    database: Database, model: Model, client: ModelClient, *, allow_host_commands: bool = False,
    context_policy: ContextPolicy | None = None, token_counter: TokenCounter | None = None,
    timeout: float = 180,
    compaction_policy: CompactionPolicy | None = None,
    max_turns: int = 32,
    memory_policy: MemoryPolicy | None = None,
    max_tool_concurrency: int = 4,
    memory_worker_concurrency: int = 1,
    sandbox: Sandbox | None = None,
    approval_policy: ApprovalPolicy | None = None,
    approval_timeout: float = 300.0,
    approval_decider=None,
    approval_remote: bool = True,
    process_wall_timeout: float = 600.0,
    max_processes: int = 4,
    web: WebAccess | None = None,
) -> AgentRunner:
    # One manager per runner: process ownership is per run, and the runner is
    # the only object that knows when a run ends.
    processes = ProcessManager(wall_timeout=process_wall_timeout, max_per_run=max_processes)
    registry = create_builtin_registry(allow_host_commands=allow_host_commands,
                                       artifacts=ArtifactRepository(database), sandbox=sandbox,
                                       processes=processes, web=web)
    builder = BasicContextBuilder(policy=context_policy,
                                  counter=token_counter or select_counter(model))
    conversations, runs = ConversationRepository(database), RunRepository(database)
    jobs = MemoryJobRepository(database)
    memory_service = MemoryService(MemoryRepository(database), conversations, runs, builder.counter,
                                   policy=memory_policy, jobs=jobs)
    runner = AgentRunner(
        model=model, client=client, context=builder,
        tools=ToolExecutor(registry, allow_host_commands=allow_host_commands),
        conversations=conversations, runs=runs,
        context_manager=ContextManager(builder, conversations, runs, SummaryRepository(database), policy=compaction_policy),
        memory_service=memory_service,
        timeout=timeout, max_turns=max_turns,
        max_tool_concurrency=max_tool_concurrency,
    )
    # Approval questions are asked by the scheduler before it takes execution
    # resources; the broker owns the wait and the single-use grant.
    runner.scheduler.policy = approval_policy or ApprovalPolicy()
    runner.scheduler.approvals = ApprovalBroker(
        ApprovalRepository(database, runs), runs, timeout=approval_timeout,
        decider=approval_decider, remote=approval_remote,
    )
    runner.scheduler.sandbox_mode = sandbox.settings.mode if sandbox is not None else "off"
    # One worker per runner: it reads model/client at call time, so a replaced
    # client (test doubles, provider switch) applies to extraction as well.
    runner.sandbox = sandbox
    runner.processes = processes
    runner.memory_worker = MemoryWorker(
        jobs, memory_service, runner,
        concurrency=memory_worker_concurrency,
        lease_seconds=max(60.0, memory_service.policy.timeout * 3),
        retry_delay=5.0,
    )
    return runner
