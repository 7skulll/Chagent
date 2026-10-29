"""Run a configured provider, or explicitly select the no-network demo."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from chagent.bootstrap import create_demo_runner, create_runner
from chagent.config import Settings
from chagent.diagnostics.behavior import describe as describe_behavior
from chagent.domain.summaries import CompactionPolicy
from chagent.tools.sandbox import Sandbox
from chagent.domain.messages import TextContent
from chagent.domain.json_types import JsonObject
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.sessions.manager import SessionManager
from chagent.models.factory import create_model_client
from chagent.models.http import HTTPModelClient
from chagent.models.settings import ModelConfigurationError, ModelSettings


async def run(
    text: str, workspace: Path, session_id: UUID | None, tool: str | None,
    arguments: JsonObject | None, allow_commands: bool, model_settings: ModelSettings | None,
) -> None:
    settings = Settings.from_env()
    client = create_model_client(model_settings) if model_settings is not None else None
    try:
        await _run(text, workspace, session_id, tool, arguments, allow_commands, settings, model_settings, client)
    finally:
        if client is not None:
            await client.aclose()


async def _run(
    text: str, workspace: Path, session_id: UUID | None, tool: str | None,
    arguments: JsonObject | None, allow_commands: bool, settings: Settings,
    model_settings: ModelSettings | None, client: HTTPModelClient | None,
) -> None:
    async with Database(settings) as database:
        await migrate(database)
        host_commands = allow_commands or settings.allow_host_commands
        sandbox = Sandbox.from_settings(settings)
        if model_settings is None:
            runner = create_demo_runner(database, tool_name=tool or "echo", tool_arguments=arguments,
                                        allow_host_commands=host_commands, max_turns=settings.max_turns,
                                        max_tool_concurrency=settings.max_tool_concurrency,
                                        memory_worker_concurrency=settings.memory_worker_concurrency,
                                        sandbox=sandbox)
        else:
            assert client is not None
            runner = create_runner(database, model_settings.model(), client,
                                   allow_host_commands=host_commands, timeout=model_settings.run_timeout, max_turns=settings.max_turns,
                                   max_tool_concurrency=settings.max_tool_concurrency,
                                   memory_worker_concurrency=settings.memory_worker_concurrency,
                                   sandbox=sandbox)
        if session_id is None:
            session = await SessionManager(runner.conversations).create(workspace, title="新会话")
            session_id = session.id
        # Printed before the run: a bad credential fails inside it, and this
        # line is the one that explains why.
        if model_settings is not None:
            print(f"Resolved: {model_settings.describe()}", flush=True)
        print(f"Behavior: {describe_behavior(settings, CompactionPolicy.from_env(), runner.context.counter.name)}", flush=True)
        result = await runner.run(session_id, text)
        print(f"Model: {runner.model.provider}/{runner.model.id}" + (" (deterministic demo)" if client is None else ""))
        print(f"Session: {result.session_id}")
        print(f"Run: {result.run_id}")
        print(f"End reason: {result.end_reason}")
        if result.message is not None:
            print("\n".join(block.text for block in result.message.content if isinstance(block, TextContent)))
        else:
            print("Tools requested a normal stop; no final model reply was generated.")
        # One-shot runs drain the queue so memories are available before exit;
        # anything still open (a retry waiting out its backoff, or an attempt
        # that outlived the drain deadline) is left for the next process. Say
        # so instead of exiting as if the queue were empty.
        if runner.memory_worker is not None and result.message is not None:
            worker = runner.memory_worker
            try:
                await worker.drain(timeout=runner.memory_service.policy.timeout + 5)
            except TimeoutError:
                pass
            remaining = await worker.jobs.unfinished()
            if remaining:
                print(f"Memory: {remaining} extraction job(s) still open; they resume on the next run.")
        history = await runner.conversations.messages(session_id)
        events = await runner.runs.events(result.run_id)
        print(f"Persisted: {len(history)} messages; {len(events)} events in this run.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chagent local model/tool agent")
    parser.add_argument("text", nargs="?", default="你好")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--session", type=UUID, help="Continue an existing session")
    parser.add_argument("--demo", action="store_true", help="Use the deterministic model without network access")
    parser.add_argument("--provider", choices=("openai", "deepseek", "anthropic", "qwen", "custom"))
    parser.add_argument("--model", help="Override the provider's default model")
    parser.add_argument("--tool", choices=("echo", "list_dir", "read_file", "write_file", "edit_file", "exec_command"), help="Select a deterministic demo tool (implies --demo)")
    parser.add_argument("--arguments", help="JSON object for the deterministic tool invocation")
    parser.add_argument("--allow-host-commands", action="store_true", help="Enable unsandboxed host commands")
    args = parser.parse_args()
    try:
        arguments = json.loads(args.arguments) if args.arguments is not None else None
    except json.JSONDecodeError:
        parser.error("--arguments must be valid JSON")
    if arguments is not None and not isinstance(arguments, dict):
        parser.error("--arguments must be a JSON object")
    if args.tool is not None and args.tool != "echo" and arguments is None:
        parser.error("--arguments is required when selecting a tool")
    demo = args.demo or args.tool is not None
    if demo and (args.provider or args.model):
        parser.error("--demo/--tool cannot be combined with --provider or --model")
    if arguments is not None and not demo:
        parser.error("--arguments is for deterministic demo tool calls")
    model_settings = None
    if not demo:
        environment = dict(os.environ)
        if args.provider:
            environment["CHAGENT_PROVIDER"] = args.provider
        if args.model:
            environment["CHAGENT_MODEL"] = args.model
        try:
            model_settings = ModelSettings.from_env(environment)
        except ModelConfigurationError as error:
            parser.error(str(error))
    asyncio.run(run(args.text, args.workspace, args.session, args.tool, arguments, args.allow_host_commands, model_settings))


if __name__ == "__main__":
    main()
