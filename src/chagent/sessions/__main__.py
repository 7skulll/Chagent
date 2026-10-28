"""Small CLI for session metadata, history, task state and context inspection."""

import argparse
import asyncio
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from uuid import UUID

from chagent.config import Settings
from chagent.domain.sessions import TaskState
from chagent.sessions.manager import SessionManager
from chagent.storage.database import Database
from chagent.storage.migrate import migrate
from chagent.storage.repositories import ConversationRepository
from chagent.storage.summaries import SummaryRepository


async def run(args: argparse.Namespace):
    async with Database(Settings.from_env()) as database:
        await migrate(database)
        manager = SessionManager(ConversationRepository(database))
        if args.command == "create":
            result = await manager.create(args.workspace, args.title)
        elif args.command == "list":
            result = await manager.list(workspace=args.workspace, include_archived=args.include_archived,
                                        limit=args.limit, before=args.before)
        elif args.command == "show":
            result = await manager.get(args.session)
        elif args.command == "rename":
            result = await manager.rename(args.session, args.title)
        elif args.command == "archive":
            result = await manager.archive(args.session)
        elif args.command == "restore":
            result = await manager.restore(args.session)
        elif args.command == "history":
            result = await manager.messages(args.session, limit=args.limit, before=args.before)
        elif args.command == "context":
            result = await manager.contexts(args.session, limit=args.limit)
        elif args.command == "summaries":
            await manager.get(args.session)
            result = [asdict(summary) for summary in await SummaryRepository(database).list(args.session, args.limit)]
        elif args.set_json is None:
            result = await manager.task_state(args.session)
        else:
            result = await manager.update_task_state(args.session, TaskState(**args.set_json),
                                                     expected_revision=args.expected_revision)
        print(json.dumps(asdict(result) if is_dataclass(result) else result, default=str,
                         ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Inspect and manage Chagent sessions")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--workspace", type=Path, default=Path.cwd())
    create.add_argument("--title", default="")
    listing = commands.add_parser("list")
    listing.add_argument("--workspace", type=Path)
    listing.add_argument("--include-archived", action="store_true")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--before", type=UUID)
    for name in ("show", "rename", "archive", "restore", "history", "state", "context", "summaries"):
        command = commands.add_parser(name)
        command.add_argument("session", type=UUID)
        if name == "rename":
            command.add_argument("title")
        elif name == "history":
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--before", type=int)
        elif name in {"context", "summaries"}:
            command.add_argument("--limit", type=int, default=10)
        elif name == "state":
            command.add_argument("--set-json")
            command.add_argument("--expected-revision", type=int)
    args = parser.parse_args()
    if args.command == "state" and args.set_json is not None:
        try:
            args.set_json = json.loads(args.set_json)
        except json.JSONDecodeError:
            parser.error("--set-json must be valid JSON")
        if not isinstance(args.set_json, dict) or args.expected_revision is None:
            parser.error("State updates require a JSON object and --expected-revision")
        if set(args.set_json) - {"goal", "constraints", "decisions", "pending", "source_message_ids"}:
            parser.error("Unknown task state field")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
