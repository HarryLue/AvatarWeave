"""Bounded memory maintenance process; independently deployed from API and Agent."""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from avatarweave_agent.memory.config import MemorySettings
from avatarweave_agent.memory.embedding import EmbeddingGateway
from avatarweave_agent.memory.gateway import ModelGateway
from avatarweave_agent.memory.service import MemoryService
from avatarweave_agent.memory.storage import MemoryDatabase


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--health-file", type=Path)
    args = parser.parse_args()
    settings = MemorySettings()
    if not settings.database_url:
        parser.error("Set MEMORY_DATABASE_URL explicitly")
    database = MemoryDatabase(settings.database_url)
    gateway = ModelGateway.from_pipeline()
    embedding = EmbeddingGateway.from_settings(settings)
    service = MemoryService(
        database, workspace_id=settings.workspace_id, companions={settings.companion_id}
    )
    from avatarweave_agent.memory.archive import EncryptedObjectStore, FileKeyProvider

    store = (
        EncryptedObjectStore(
            settings.archive_root,
            FileKeyProvider(settings.archive_key_root),
            settings.archive_key_version,
        )
        if settings.archive_root and settings.archive_key_root
        else None
    )
    worker = "maintenance-" + uuid4().hex
    journal = None
    if settings.deletion_journal_path and store:
        from avatarweave_agent.memory.deletion_journal import FileDeletionJournal

        journal = FileDeletionJournal(settings.deletion_journal_path, store)
        service.deletion_journal = journal
    try:
        await database.check_schema()
        health_written = 0.0
        while True:
            if args.health_file and monotonic() - health_written >= 5:
                from avatarweave_agent.memory.utils import private_write

                private_write(
                    args.health_file,
                    json.dumps(
                        {
                            "state": "ready",
                            "pid": os.getpid(),
                            "worker_id": worker,
                            "updated_at": datetime.now(UTC).isoformat(),
                        }
                    ),
                )
                health_written = monotonic()
            if journal:
                from avatarweave_agent.memory.deletion_journal import sync_journal

                await sync_journal(service, journal)
            cleaned = await service.process_deletions(store=store)
            scheduled = await service.schedule_incremental(
                embedding, store, model_available=gateway is not None
            )
            worked = await service.next_job(
                worker, gateway, embedding_gateway=embedding, store=store
            )
            worked = worked or bool(cleaned) or bool(scheduled)
            await service.expire_raw_batch()
            await service.expire_sessions()
            if store:
                await service.expire_exports(store)
            if args.once:
                break
            if not worked:
                await asyncio.sleep(0.25)
    finally:
        if store:
            await store.close()
        if gateway:
            await gateway.close()
        if embedding:
            await embedding.close()
        await database.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
