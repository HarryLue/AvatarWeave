import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import settings


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    __table_args__ = (UniqueConstraint("conversation_id", "id"),)


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def sqlite_pragmas(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def save_message(room: str, message_id: str, role: str, text: str):
    if not text.strip():
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    async with Session.begin() as db:
        await db.execute(
            insert(Conversation)
            .values(id=str(uuid.uuid4()), room=room)
            .on_conflict_do_nothing(index_elements=["room"])
        )
        conversation = await db.scalar(
            select(Conversation).where(Conversation.room == room)
        )
        await db.execute(
            insert(Message)
            .values(
                id=message_id, conversation_id=conversation.id, role=role, text=text
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )


async def get_messages(room: str, limit: int = 500):
    """Latest N messages returned in chronological order, with a stable tie-breaker."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 2000:
        raise ValueError("History limit must be an integer from 1 to 2000")
    async with Session() as db:
        rows = await db.scalars(
            select(Message)
            .join(Conversation)
            .where(Conversation.room == room)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return [
            {"id": m.id, "role": m.role, "text": m.text} for m in reversed(list(rows))
        ]
