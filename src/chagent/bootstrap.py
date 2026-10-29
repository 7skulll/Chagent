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
from chagent.tools.sandbox import Sandbox
from chagent.domain.memories import MemoryPolicy
from chagent.memory.service import MemoryService
from chagent.storage.memories import MemoryRepository


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
) -> AgentRunner:
    return create_runner(
        database, Model(id="fake-echo", provider="local-test", api="fake", context_window=8192, max_tokens=512),
        FakeModelClient(tool_name, tool_arguments), allow_host_commands=allow_host_commands,
        context_policy=context_policy, token_counter=token_counter, compaction_policy=compaction_policy, timeout=60, max_turns=max_turns,
        memory_policy=memory_policy,
        max_tool_concurrency=max_tool_concurrency,
        memory_worker_concurrency=memory_worker_concurrency,
        sandbox=sandbox,
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
) -> AgentRunner:
    registry = create_builtin_registry(allow_host_commands=allow_host_commands,
                                       artifacts=ArtifactRepository(database), sandbox=sandbox)
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
    # One worker per runner: it reads model/client at call time, so a replaced
    # client (test doubles, provider switch) applies to extraction as well.
    runner.sandbox = sandbox
    runner.memory_worker = MemoryWorker(
        jobs, memory_service, runner,
        concurrency=memory_worker_concurrency,
        lease_seconds=max(60.0, memory_service.policy.timeout * 3),
        retry_delay=5.0,
    )
    return runner
