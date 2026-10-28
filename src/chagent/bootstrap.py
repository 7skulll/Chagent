"""Construct the minimal runnable agent from explicit dependencies."""

from chagent.agent.runner import AgentRunner
from chagent.context.builder import BasicContextBuilder
from chagent.context.manager import ContextManager
from chagent.domain.json_types import JsonObject
from chagent.domain.context import ContextPolicy
from chagent.domain.summaries import CompactionPolicy
from chagent.context.tokens import TokenCounter
from chagent.models.base import Model, ModelClient
from chagent.models.fake import FakeModelClient
from chagent.storage.database import Database
from chagent.storage.repositories import ConversationRepository, RunRepository
from chagent.storage.summaries import SummaryRepository
from chagent.storage.artifacts import ArtifactRepository
from chagent.tools.builtin import create_builtin_registry
from chagent.tools.executor import ToolExecutor
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
) -> AgentRunner:
    return create_runner(
        database, Model(id="fake-echo", provider="local-test", api="fake", context_window=8192, max_tokens=512),
        FakeModelClient(tool_name, tool_arguments), allow_host_commands=allow_host_commands,
        context_policy=context_policy, token_counter=token_counter, compaction_policy=compaction_policy, timeout=60, max_turns=max_turns,
        memory_policy=memory_policy,
    )


def create_runner(
    database: Database, model: Model, client: ModelClient, *, allow_host_commands: bool = False,
    context_policy: ContextPolicy | None = None, token_counter: TokenCounter | None = None,
    timeout: float = 180,
    compaction_policy: CompactionPolicy | None = None,
    max_turns: int = 32,
    memory_policy: MemoryPolicy | None = None,
) -> AgentRunner:
    registry = create_builtin_registry(allow_host_commands=allow_host_commands, artifacts=ArtifactRepository(database))
    builder = BasicContextBuilder(policy=context_policy, counter=token_counter)
    conversations, runs = ConversationRepository(database), RunRepository(database)
    return AgentRunner(
        model=model, client=client, context=builder,
        tools=ToolExecutor(registry, allow_host_commands=allow_host_commands),
        conversations=conversations, runs=runs,
        context_manager=ContextManager(builder, conversations, runs, SummaryRepository(database), policy=compaction_policy),
        memory_service=MemoryService(MemoryRepository(database), conversations, runs, builder.counter, policy=memory_policy),
        timeout=timeout, max_turns=max_turns,
    )
