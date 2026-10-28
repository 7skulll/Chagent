"""Run with: python -m chagent.storage check|migrate."""

import argparse
import asyncio

from chagent.config import Settings
from chagent.storage.database import Database
from chagent.storage.migrate import migrate


async def run(command: str) -> None:
    async with Database(Settings.from_env()) as database:
        if command == "migrate":
            applied = await migrate(database)
            print("Applied: " + ", ".join(applied) if applied else "Database is up to date.")
        else:
            print("PostgreSQL connection OK.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chagent PostgreSQL utilities")
    parser.add_argument("command", choices=("check", "migrate"))
    args = parser.parse_args()
    asyncio.run(run(args.command))


if __name__ == "__main__":
    main()
