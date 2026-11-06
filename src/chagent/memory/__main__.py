"""Manage local memory without configuring or calling a model."""

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from uuid import UUID, uuid4

from chagent.config import Settings
from chagent.domain.memories import MemoryProposal
from chagent.storage.database import Database
from chagent.storage.memories import MemoryRepository
from chagent.storage.migrate import migrate
from chagent.storage.workspace_identity import ensure_workspace


async def run(args):
    workspace = args.workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    async with Database(Settings.from_env()) as database:
        await migrate(database)
        async with database.connection() as connection:
            workspace_id = await ensure_workspace(connection, workspace)
        repository = MemoryRepository(database)
        if args.command == "list":
            page = await repository.list(workspace_id, status=args.status, limit=args.limit, before=args.before)
            result = {"items": [asdict(item) for item in page["items"]], "next_cursor": page["next_cursor"]}
        elif args.command == "create":
            result = asdict(await repository.propose(MemoryProposal(args.key, args.content, args.scope, args.pinned, "manual"), workspace_id))
        elif args.command == "show":
            result = asdict(await repository.get(args.id, workspace_id))
        elif args.command == "revisions":
            result = await repository.revisions(args.id, workspace_id)
        else:
            result = asdict(await repository.change(args.id, workspace_id, expected_revision=args.expected_revision,
                              content=args.content if args.command == "edit" else None, action=args.command,
                              replace_id=args.replace_id if args.command == "accept" else None,
                              replace_revision=args.replace_revision if args.command == "accept" else None))
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Manage Chagent long-term memory")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=("active", "candidate", "deleted", "rejected", "quarantined", "all"), default="active")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--before", type=UUID)
    create = commands.add_parser("create")
    create.add_argument("key"); create.add_argument("content")
    create.add_argument("--scope", choices=("workspace", "user"), default="workspace")
    create.add_argument("--pinned", action="store_true")
    for name in ("show", "revisions", "edit", "accept", "reject", "delete"):
        command = commands.add_parser(name); command.add_argument("id", type=UUID)
        if name == "edit":
            command.add_argument("content")
        if name in {"edit", "accept", "reject", "delete"}:
            command.add_argument("--expected-revision", type=int, required=True)
        if name == "accept":
            command.add_argument("--replace-id", type=UUID)
            command.add_argument("--replace-revision", type=int)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, LookupError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
