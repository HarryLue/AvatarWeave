"""Versioned memory, evidence, dependency and maintenance tables (schema v3)."""

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .storage import MemoryBase


class FactKey(MemoryBase):
    __tablename__ = "vm_fact_keys"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    lock_revision: Mapped[int] = mapped_column(default=0)


class ControlOperation(MemoryBase):
    __tablename__ = "vm_control_operations"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(32))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[str | None] = mapped_column(Text)


class CorrectionTarget(MemoryBase):
    __tablename__ = "vm_correction_targets"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    memory_id: Mapped[str] = mapped_column(String(128))
    expected_version: Mapped[int]
    state: Mapped[str] = mapped_column(String(32))


class ImportMapping(MemoryBase):
    __tablename__ = "vm_import_mappings"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    export_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    old_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    old_version: Mapped[int] = mapped_column(primary_key=True)
    new_id: Mapped[str] = mapped_column(String(128))
    new_version: Mapped[int]


class FactVersion(MemoryBase):
    __tablename__ = "vm_fact_versions"
    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"))
    companion_id: Mapped[str] = mapped_column(String(128))
    fact_key: Mapped[str] = mapped_column(ForeignKey("vm_fact_keys.id"))
    subject_id: Mapped[str] = mapped_column(String(128))
    predicate: Mapped[str] = mapped_column(String(128))
    namespace: Mapped[str] = mapped_column(String(32))
    system_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    system_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    record_json: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index("vm_fact_scope_key", "account_id", "companion_id", "fact_key"),
        Index("vm_fact_scope_time", "account_id", "companion_id", "system_from"),
    )


class FactSource(MemoryBase):
    __tablename__ = "vm_fact_sources"
    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_revision: Mapped[int] = mapped_column(primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["memory_id", "version"], ["vm_fact_versions.memory_id", "vm_fact_versions.version"]
        ),
        ForeignKeyConstraint(
            ["event_id", "source_revision"],
            ["vm_source_revisions.event_id", "vm_source_revisions.revision"],
        ),
        Index("vm_fact_source_reverse", "event_id", "source_revision"),
    )


class EvidenceSuppression(MemoryBase):
    __tablename__ = "vm_evidence_suppressions"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_revision: Mapped[int] = mapped_column(primary_key=True)
    fact_key: Mapped[str] = mapped_column(String(64), primary_key=True)


class Capsule(MemoryBase):
    __tablename__ = "vm_evidence_capsules"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[str] = mapped_column(String(128), index=True)
    source_revision: Mapped[int]
    capsule_json: Mapped[str] = mapped_column(Text)


class Dependency(MemoryBase):
    __tablename__ = "vm_dependencies"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    parent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    parent_version: Mapped[int] = mapped_column(primary_key=True)
    child_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    child_version: Mapped[int] = mapped_column(primary_key=True)
    relation: Mapped[str] = mapped_column(String(32))


class Entity(MemoryBase):
    __tablename__ = "vm_entities"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    entity_json: Mapped[str] = mapped_column(Text)


class EntityAlias(MemoryBase):
    __tablename__ = "vm_entity_aliases"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    companion_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    alias_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    alias: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(ForeignKey("vm_entities.id", ondelete="CASCADE"), primary_key=True)


class OperationReceipt(MemoryBase):
    __tablename__ = "vm_operation_receipts"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(32))
    # References and control arguments only; never raw source or deleted text.
    request_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)


class Tombstone(MemoryBase):
    __tablename__ = "vm_tombstones"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    privacy_epoch: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Deletion(MemoryBase):
    __tablename__ = "vm_deletions"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    privacy_epoch: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32))
    result_json: Mapped[str] = mapped_column(Text)


class JournalAck(MemoryBase):
    __tablename__ = "vm_journal_ack"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))


class AccountBarrier(MemoryBase):
    __tablename__ = "vm_account_barriers"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    deletion_job_id: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16))


class Job(MemoryBase):
    __tablename__ = "vm_jobs"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    spec_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), index=True)
    attempt: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    last_error_json: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class UserWork(MemoryBase):
    __tablename__ = "vm_user_work"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    last_claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DerivedView(MemoryBase):
    __tablename__ = "vm_derived_views"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    document_json: Mapped[str | None] = mapped_column(Text)


class VectorEntry(MemoryBase):
    __tablename__ = "vm_vectors"
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), primary_key=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    index_generation: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Portable authoritative vector payload; pgvector projections are disposable.
    profile_json: Mapped[str] = mapped_column(Text)
    vector_json: Mapped[str] = mapped_column(Text)
    projection: Mapped[list[float] | None] = mapped_column(Vector().with_variant(JSON(), "sqlite"))


class ObjectManifest(MemoryBase):
    __tablename__ = "vm_object_manifests"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"), index=True)
    companion_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(32))
    manifest_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Generation(MemoryBase):
    __tablename__ = "vm_generations"
    session_id: Mapped[str] = mapped_column(ForeignKey("vm_sessions.id"), primary_key=True)
    generation_id: Mapped[str] = mapped_column(String(128))
    privacy_epoch: Mapped[int]
    memory_revision: Mapped[int]
    owner_epoch: Mapped[int]
    cancelled: Mapped[bool] = mapped_column(default=False)
    serial: Mapped[int] = mapped_column(default=1)


class GenerationUse(MemoryBase):
    __tablename__ = "vm_generation_uses"
    session_id: Mapped[str] = mapped_column(ForeignKey("vm_sessions.id"), primary_key=True)
    generation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)


class OutputLedger(MemoryBase):
    __tablename__ = "vm_output_ledger"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("vm_sessions.id"), index=True)
    generation_id: Mapped[str] = mapped_column(String(128))
    account_id: Mapped[str] = mapped_column(ForeignKey("vm_accounts.id"))
    state: Mapped[str] = mapped_column(String(32))
    plan_json: Mapped[str | None] = mapped_column(Text)
    last_client_sequence: Mapped[int] = mapped_column(default=-1)
