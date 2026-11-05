"""Explicit memory maintenance operator. No deployment, index switch or implicit legacy import."""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from avatarweave_agent.memory import contracts as dto
from avatarweave_agent.memory.archive import EncryptedObjectStore, FileKeyProvider
from avatarweave_agent.memory.config import MemorySettings
from avatarweave_agent.memory.deletion_journal import FileDeletionJournal, sync_journal
from avatarweave_agent.memory.embedding import EmbeddingGateway
from avatarweave_agent.memory.execution import MemoryFault
from avatarweave_agent.memory.gateway import ModelGateway
from avatarweave_agent.memory.http import meta
from avatarweave_agent.memory.service import MemoryService
from avatarweave_agent.memory.storage import MemoryDatabase
from avatarweave_agent.memory.utils import private_write

MODELS = {
    "admission": dto.AdmissionInput,
    "entities": dto.EntityInput,
    "temporal": dto.TemporalInput,
    "conflict": dto.ConflictInput,
    "commit": dto.CommitInput,
    "tiering": dto.TieringInput,
    "compression": dto.CompressionInput,
    "reflection": dto.ReflectionInput,
    "decay": dto.DecayInput,
    "forget": dto.ForgetInput,
    "query": dto.QueryInput,
    "retrieve": dto.RetrievalInput,
    "open_loops": dto.OpenLoopInput,
    "archive": dto.ArchiveInput,
    "privacy": dto.PrivacyInput,
    "migration": dto.MigrationInput,
    "embedding": dto.EmbeddingInput,
    "export": dto.ExportInput,
    "repository": dto.RepositoryInput,
}
JOBS = {
    "admission": "extract",
    "compression": "summarize",
    "reflection": "reflect",
    "tiering": "tier",
    "archive": "archive",
    "privacy": "delete",
    "migration": "restore",
    "embedding": "embed",
    "export": "export",
}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser(
        "init-store", help="Create development encrypted object/key directories"
    )
    initialize.add_argument("--root", type=Path, default=Path(".runtime/memory"))
    initialize.add_argument("--key-version", default="v1")
    initialize.add_argument("--objects", type=Path, help="Explicit object directory")
    initialize.add_argument("--keys", type=Path, help="Explicit key directory")
    execute = commands.add_parser(
        "run", help="Execute a typed internal request under current SQL authorization"
    )
    execute.add_argument("module", choices=MODELS)
    execute.add_argument("--input", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--enqueue", action="store_true")
    commands.add_parser("sync-deletion-journal")
    args = parser.parse_args()
    if args.command == "init-store":
        from cryptography.fernet import Fernet

        if not re.fullmatch("[A-Za-z0-9_-]{1,64}", args.key_version):
            parser.error("Invalid key version")
        objects, keys = (
            args.objects or args.root / "objects",
            args.keys or args.root / "keys",
        )
        for directory in (objects, keys):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = keys / (args.key_version + ".key")
        if not key.exists():
            descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(Fernet.generate_key())
                output.flush()
                os.fsync(output.fileno())
        FileKeyProvider(keys).get_key(args.key_version)
        print(
            json.dumps(
                {
                    "status": "initialized",
                    "object_root": str(objects.resolve()),
                    "key_root": str(keys.resolve()),
                    "key_version": args.key_version,
                }
            )
        )
        return
    settings = MemorySettings()
    if not settings.database_url:
        parser.error("Set MEMORY_DATABASE_URL explicitly")
    database = MemoryDatabase(settings.database_url)
    gateway, embedding = (
        ModelGateway.from_pipeline(),
        EmbeddingGateway.from_settings(settings),
    )
    store = (
        EncryptedObjectStore(
            settings.archive_root,
            FileKeyProvider(settings.archive_key_root),
            settings.archive_key_version,
        )
        if settings.archive_root and settings.archive_key_root
        else None
    )
    journal = (
        FileDeletionJournal(settings.deletion_journal_path, store)
        if settings.deletion_journal_path and store
        else None
    )
    service = MemoryService(
        database, workspace_id=settings.workspace_id, companions={settings.companion_id}
    )
    service.deletion_journal = journal
    try:
        await database.check_schema()
        if args.command == "sync-deletion-journal":
            if journal is None:
                parser.error(
                    "Configure an independent MEMORY_DELETION_JOURNAL_PATH and encrypted store"
                )
            print(json.dumps({"journaled": await sync_journal(service, journal)}))
            return
        if args.input.stat().st_size > 256 * 1024:
            parser.error("Typed request exceeds one bounded 256 KiB shard")
        request = MODELS[args.module].model_validate_json(args.input.read_bytes())
        request = request.model_copy(
            update={
                "meta": meta(
                    service, lane=request.meta.execution.lane, budget_ms=30000
                ).model_copy(update={"operation_id": request.meta.operation_id})
            }
        )
        if args.enqueue:
            if args.module not in JOBS or args.module == "admission":
                parser.error(
                    "Source extraction uses the transactional source outbox; this module has no generic job kind"
                )
            result = await service.enqueue_maintenance(JOBS[args.module], request)
        elif args.module == "temporal":
            from avatarweave_agent.memory.lifecycle import temporal

            async with database.sessions() as db:
                account = await service._account(db, request.scope)
                service._privacy(account, request)
            result = dto.ModuleResult(status="ok", data=temporal(request))
        else:
            kwargs = {}
            if args.module in {"admission", "reflection"}:
                if gateway is None:
                    raise MemoryFault(
                        "provider_unavailable", "Model gateway is not configured"
                    )
                kwargs["gateway"] = gateway
            if args.module == "embedding":
                if embedding is None:
                    raise MemoryFault(
                        "provider_unavailable", "Embedding gateway is not configured"
                    )
                kwargs["gateway"] = embedding
            if args.module == "retrieve":
                kwargs["embedding_gateway"] = embedding
            if args.module in {"archive", "export", "migration"}:
                kwargs["store"] = store
            if args.module == "migration":
                kwargs.update(embedding_gateway=embedding, deletion_journal=journal)
            result = await getattr(service, args.module)(request, **kwargs)
        private_write(args.output, result.model_dump_json(indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": getattr(result, "status", "queued"),
                    "output": str(args.output),
                }
            )
        )
    finally:
        if store:
            await store.close()
        if gateway:
            await gateway.close()
        if embedding:
            await embedding.close()
        await database.close()


if __name__ == "__main__":
    from pydantic import ValidationError

    try:
        asyncio.run(main())
    except MemoryFault as error:
        raise SystemExit(error.info.model_dump_json()) from None
    except ValidationError:
        raise SystemExit("Input request failed strict contract validation") from None
