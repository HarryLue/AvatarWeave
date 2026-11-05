from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert

from avatarweave_agent.db import (
    Conversation,
    Message,
    Session,
    engine,
    get_messages,
    init_db,
)


@pytest_asyncio.fixture
async def history_scope():
    await init_db()
    suffix = uuid4().hex[:16]
    room = "long-history-room-" + suffix
    primary_id, other_id = "long-" + suffix, "other-" + suffix
    try:
        yield room, primary_id, other_id, suffix
    finally:
        try:
            async with Session.begin() as db:
                await db.execute(
                    delete(Message).where(Message.conversation_id.in_([primary_id, other_id]))
                )
                await db.execute(
                    delete(Conversation).where(Conversation.id.in_([primary_id, other_id]))
                )
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_long_room_restores_latest_messages_in_chronological_order(history_scope):
    room, primary_id, other_id, suffix = history_scope
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with Session.begin() as db:
        await db.execute(
            insert(Conversation),
            [
                {"id": primary_id, "room": room},
                {"id": other_id, "room": "other-history-room-" + suffix},
            ],
        )
        await db.execute(
            insert(Message),
            [
                {
                    "id": f"history-{suffix}-{i:04d}",
                    "conversation_id": primary_id,
                    "role": "user" if i % 2 == 0 else "assistant",
                    "text": str(i),
                    "created_at": start + timedelta(seconds=i // 2),
                }
                for i in range(650)
            ]
            + [
                {
                    "id": "history-other-" + suffix,
                    "conversation_id": other_id,
                    "role": "user",
                    "text": "not this room",
                    "created_at": start + timedelta(days=1),
                }
            ],
        )
    latest = await get_messages(room, limit=100)
    assert [m["text"] for m in latest] == [str(i) for i in range(550, 650)]
    assert len(await get_messages(room)) == 500
    assert [m["text"] for m in await get_messages(room, limit=1)] == ["649"]
    assert await get_messages("no-such-room") == []
    with pytest.raises(ValueError):
        await get_messages(room, limit=0)
