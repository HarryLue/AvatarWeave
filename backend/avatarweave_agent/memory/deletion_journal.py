"""Independent signed deletion journal, kept outside database backup snapshots.

This development adapter uses its own SQLite file. Production may implement the
same interface with an independently retained deletion inventory.
"""

import hashlib
import hmac
import os
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime
from sqlalchemy import and_, select

from .contracts import Contract, Identifier
from .execution import MemoryFault
from .storage import Account
from .tables import JournalAck, Tombstone
from .utils import utc


class DeletionMark(Contract):
    workspace_id: Identifier
    account_id: Identifier
    object_id: Identifier
    kind: Literal["memory", "source_raw", "account"]
    privacy_epoch: int
    occurred_at: AwareDatetime


class FileDeletionJournal:
    def __init__(self, path, store):
        self.path, self.store = Path(path).resolve(), store

    def _connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        if self.path.stat().st_mode & 0o077:
            raise PermissionError("Deletion journal must be private")
        db = sqlite3.connect(self.path, timeout=1)
        db.execute("PRAGMA synchronous=FULL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS marks (id INTEGER PRIMARY KEY, workspace TEXT NOT NULL, account TEXT NOT NULL, object TEXT NOT NULL, kind TEXT NOT NULL, epoch INTEGER NOT NULL, key_version TEXT NOT NULL, payload TEXT NOT NULL, signature TEXT NOT NULL, UNIQUE(workspace, account, object, kind, epoch))"
        )
        db.execute("CREATE INDEX IF NOT EXISTS account_marks ON marks(workspace, account, id)")
        return db

    def _append(self, marks):
        db = self._connect()
        try:
            with db:
                for mark in marks:
                    payload = mark.model_dump_json()
                    key_version = self.store.key_version
                    signature = hmac.new(
                        self.store.keys.get_key(key_version), payload.encode(), "sha256"
                    ).hexdigest()
                    db.execute(
                        "INSERT OR IGNORE INTO marks(workspace, account, object, kind, epoch, key_version, payload, signature) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            mark.workspace_id,
                            mark.account_id,
                            mark.object_id,
                            mark.kind,
                            mark.privacy_epoch,
                            key_version,
                            payload,
                            signature,
                        ),
                    )
        finally:
            db.close()

    async def append(self, marks):
        if not 1 <= len(marks) <= 128:
            raise ValueError("Invalid deletion journal batch")
        await self.store._io("deletion-journal", self._append, marks)

    def _read(self, scope, limit, cursor):
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT id, key_version, payload, signature FROM marks WHERE workspace=? AND account=? AND id>? ORDER BY id LIMIT ?",
                (scope.workspace_id, scope.user_id, cursor, limit + 1),
            ).fetchall()
            result = []
            for sequence, key_version, payload, signature in rows[:limit]:
                expected = hmac.new(
                    self.store.keys.get_key(key_version), payload.encode(), "sha256"
                ).hexdigest()
                if not hmac.compare_digest(expected, signature):
                    raise ValueError("Deletion journal integrity check failed")
                mark = DeletionMark.model_validate_json(payload)
                if mark.workspace_id != scope.workspace_id or mark.account_id != scope.user_id:
                    raise ValueError("Deletion journal owner mismatch")
                result.append(mark)
            return result, rows[limit - 1][0] if len(rows) > limit else None
        finally:
            db.close()

    async def read(self, scope, *, limit=128, cursor=0):
        if not 1 <= limit <= 10000 or cursor < 0:
            raise ValueError("Invalid journal page")
        return await self.store._io("deletion-journal", self._read, scope, limit, cursor)

    async def verify_reconciled(self, database, scope, expected_watermark, limit):
        if not self.path.is_file():
            raise MemoryFault("storage_unavailable", "Independent deletion journal is unavailable")
        marks, next_cursor = await self.read(scope, limit=limit)
        if next_cursor is not None:
            raise MemoryFault(
                "budget_exceeded", "Deletion reconciliation requires another isolated batch"
            )
        if max((mark.privacy_epoch for mark in marks), default=0) != expected_watermark:
            raise MemoryFault("privacy_changed", "Independent deletion watermark differs")
        async with database.sessions() as db:
            for mark in marks:
                stored = await db.get(Tombstone, (scope.user_id, mark.object_id, mark.kind))
                if stored is None or stored.privacy_epoch < mark.privacy_epoch:
                    raise MemoryFault(
                        "privacy_changed",
                        "Replay independent deletions before exposing restored data",
                    )
        return len(marks)


async def sync_journal(service, journal, batch_size=128):
    if not 1 <= batch_size <= 128:
        raise ValueError("Invalid journal sync batch")
    async with service.database.sessions() as db:
        rows = (
            await db.execute(
                select(Tombstone, Account.workspace_id)
                .join(Account, Account.id == Tombstone.account_id)
                .outerjoin(
                    JournalAck,
                    and_(
                        JournalAck.account_id == Tombstone.account_id,
                        JournalAck.object_id == Tombstone.object_id,
                        JournalAck.kind == Tombstone.kind,
                    ),
                )
                .where(Account.workspace_id == service.workspace_id, JournalAck.object_id.is_(None))
                .order_by(Tombstone.created_at, Tombstone.object_id)
                .limit(batch_size)
            )
        ).all()
        marks = [
            DeletionMark(
                workspace_id=workspace,
                account_id=row.account_id,
                object_id=row.object_id,
                kind=row.kind,
                privacy_epoch=row.privacy_epoch,
                occurred_at=utc(row.created_at),
            )
            for row, workspace in rows
        ]
    if not marks:
        return 0
    await journal.append(marks)
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    insert = sqlite_insert if service.database.engine.dialect.name == "sqlite" else pg_insert
    async with service.database.sessions.begin() as db:
        for mark in marks:
            await db.execute(
                insert(JournalAck)
                .values(
                    account_id=mark.account_id,
                    object_id=mark.object_id,
                    kind=mark.kind,
                    fingerprint=hashlib.sha256(mark.model_dump_json().encode()).hexdigest(),
                )
                .on_conflict_do_nothing()
            )
    return len(marks)
