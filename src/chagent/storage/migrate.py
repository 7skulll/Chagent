"""Transactional, checksum-verified migrations bundled with the package."""

from hashlib import sha256
from importlib.resources import files

from chagent.storage.database import Database


async def migrate(database: Database) -> list[str]:
    directory = files("chagent.storage").joinpath("migrations")
    migrations = sorted(
        ((path.name, path.read_text(encoding="utf-8")) for path in directory.iterdir()
         if path.name.endswith(".sql")),
    )
    applied: list[str] = []
    async with database.transaction() as connection:
        # Serialize migrators; released automatically on commit or rollback.
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (0x43484147454E54,))
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name text PRIMARY KEY,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        cursor = await connection.execute("SELECT name, checksum FROM schema_migrations")
        existing = {row["name"]: row["checksum"] for row in await cursor.fetchall()}
        available = {name for name, _ in migrations}
        if existing.keys() - available:
            raise RuntimeError("Database contains migrations missing from this application version")
        for name, source in migrations:
            checksum = sha256(source.encode("utf-8")).hexdigest()
            if name in existing:
                if existing[name] != checksum:
                    raise RuntimeError(f"Applied migration has been modified: {name}")
                continue
            await connection.execute(source)
            await connection.execute(
                "INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)",
                (name, checksum),
            )
            applied.append(name)
    return applied
