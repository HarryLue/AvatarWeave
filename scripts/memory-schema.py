"""Explicit additive P1 migration. Never called by API/Worker startup."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from avatarweave_agent.memory.config import MemorySettings
from avatarweave_agent.memory.storage import MemoryDatabase


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Explicitly create additive tables"
    )
    args = parser.parse_args()
    settings = MemorySettings()
    if not settings.database_url:
        parser.error(
            "Set MEMORY_DATABASE_URL explicitly (never infers the legacy database)"
        )
    database = MemoryDatabase(settings.database_url)
    try:
        if args.apply:
            await database.create_schema()
        await database.check_schema()
        print("Memory schema v1 verified")
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
