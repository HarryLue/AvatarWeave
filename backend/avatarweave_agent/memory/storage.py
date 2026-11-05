"""Additive memory schema. Created only by the explicit migration command, never init_db.

JSON bodies are validated contracts, not an alternative untyped business protocol.
The legacy room history deliberately has no foreign key into this schema.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class MemoryBase(DeclarativeBase):
    pass


class SchemaVersion(MemoryBase):
    __tablename__ = "vm_schema_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int]


class Account(MemoryBase):
    __tablename__ = "vm_accounts"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128))
    issuer: Mapped[str] = mapped_column(String(2048))
    subject: Mapped[str] = mapped_column(String(255))
    privacy_epoch: Mapped[int] = mapped_column(default=0)
    memory_revision: Mapped[int] = mapped_column(default=0)
    source_watermark: Mapped[int] = mapped_column(default=0)
    structured_watermark: Mapped[int] = mapped_column(default=0)
    index_watermark: Mapped[int] = mapped_column(default=0)
    preferences_json: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("workspace_id", "issuer", "subject"),)


class Login(MemoryBase):
    __tablename__ = "vm_logins"
    # Only a digest of a high entropy random bearer cookie is stored.
    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    csrf_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemorySession(MemoryBase):
    __tablename__ = "vm_sessions"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    room: Mapped[str] = mapped_column(String(128), unique=True)
    participant: Mapped[str] = mapped_column(String(128), unique=True)
    memory_enabled: Mapped[bool]
    status: Mapped[str] = mapped_column(String(16), default="ready")
    capabilities_json: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    owner_epoch: Mapped[int] = mapped_column(default=0)
    owner_id: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("account_id", "idempotency_key"),)


class EventHead(MemoryBase):
    __tablename__ = "vm_event_heads"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("vm_sessions.id"), index=True)
    turn_id: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int]
    __table_args__ = (UniqueConstraint("session_id", "turn_id"),)


class SessionReceipt(MemoryBase):
    __tablename__ = "vm_session_receipts"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[str] = mapped_column(ForeignKey("vm_sessions.id"))
    request_json: Mapped[str] = mapped_column(Text)


class PreferenceReceipt(MemoryBase):
    __tablename__ = "vm_preference_receipts"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)


class SourceRevision(MemoryBase):
    __tablename__ = "vm_source_revisions"
    event_id: Mapped[str] = mapped_column(ForeignKey("vm_event_heads.event_id"), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"))
    companion_id: Mapped[str] = mapped_column(String(128))
    session_id: Mapped[str] = mapped_column(ForeignKey("vm_sessions.id"))
    sequence: Mapped[int]
    # Erased on retention expiry / deletion, metadata persists independently.
    source_json: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32), default="persisted")
    structured_settled: Mapped[bool] = mapped_column(default=False)
    index_settled: Mapped[bool] = mapped_column(default=False)
    revoked: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (
        UniqueConstraint("account_id", "sequence"),
        Index("vm_source_scope_sequence", "account_id", "companion_id", "sequence"),
        Index("vm_source_retention", "expires_at"),
    )


class QueueGuard(MemoryBase):
    __tablename__ = "vm_queue_guard"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(default=0)


class Outbox(MemoryBase):
    __tablename__ = "vm_outbox"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[str] = mapped_column(String(128))
    source_revision: Mapped[int]
    privacy_epoch: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16), default="extract")
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempt: Mapped[int] = mapped_column(default=0)
    payload_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("event_id", "source_revision", "kind"),)


def make_engine(url: str, *, pool_size: int = 2):
    options = {"pool_size": pool_size, "max_overflow": 0, "pool_timeout": 0.1}
    if url.endswith(":memory:"):
        options = {}
    if url.startswith("postgresql"):
        options["connect_args"] = {
            "server_settings": {
                "application_name": "avatarweave-memory-control",
                "statement_timeout": "150ms",
                "lock_timeout": "50ms",
            }
        }
    engine = create_async_engine(url, pool_pre_ping=True, **options)
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def pragmas(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=1000")
            cursor.close()

    return engine


class MemoryDatabase:
    """One bounded control pool per process; no implicit production DDL."""

    def __init__(self, url: str):
        from . import tables  # noqa: F401 -- register the additive schema

        self.engine = make_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def check_schema(self):
        async with self.sessions() as db:
            row = await db.get(SchemaVersion, 1)
            if row is None or row.version != 3:
                raise RuntimeError("Memory schema version 3 is required; run explicit migration")

    async def create_schema(self):
        """Only called by migration CLI and isolated tests."""
        from sqlalchemy import select

        async with self.engine.begin() as connection:
            from sqlalchemy import inspect

            exists = await connection.run_sync(
                lambda sync: inspect(sync).has_table("vm_schema_version")
            )
            if exists:
                version = await connection.scalar(
                    select(SchemaVersion.version).where(SchemaVersion.id == 1)
                )
                if version not in {1, 2, 3}:
                    raise RuntimeError("Unknown memory schema; refusing to mutate it")
            if self.engine.dialect.name == "postgresql":
                from sqlalchemy import text

                await connection.execute(text("SET LOCAL statement_timeout = '30s'"))
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.run_sync(MemoryBase.metadata.create_all)
            # v3 adds a rebuildable scoped exact-alias projection. No private
            # source text is scanned and no existing fact/identity is reassigned.
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            from .contracts import EntityCandidate
            from .lifecycle import normalized
            from .tables import Entity, EntityAlias

            insert = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
            stream = await connection.stream(
                select(Entity.id, Entity.account_id, Entity.companion_id, Entity.entity_json)
            )
            async for rows in stream.partitions(100):
                aliases = []
                for entity_id, account_id, companion_id, encoded in rows:
                    value = EntityCandidate.model_validate_json(encoded)
                    from hashlib import sha256

                    aliases.extend(
                        {
                            "account_id": account_id,
                            "companion_id": companion_id,
                            "entity_id": entity_id,
                            "alias": name,
                            "alias_key": sha256(name.encode()).hexdigest(),
                        }
                        for name in {normalized(s) for s in [value.canonical_name] + value.aliases}
                        if name
                    )
                if aliases:
                    await connection.execute(insert(EntityAlias).on_conflict_do_nothing(), aliases)
        async with self.sessions.begin() as db:
            version = await db.scalar(select(SchemaVersion))
            if version is not None and version.version not in {1, 2, 3}:
                raise RuntimeError("Unknown memory schema; refusing to downgrade")
            if version is None:
                db.add(SchemaVersion(id=1, version=3))
            else:
                version.version = 3
            if await db.get(QueueGuard, 1) is None:
                db.add(QueueGuard(id=1, revision=0))

    async def close(self):
        await self.engine.dispose()
