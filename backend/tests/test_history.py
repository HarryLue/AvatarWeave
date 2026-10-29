from datetime import UTC, datetime, timedelta

import pytest
from avatarweave_agent.db import (
    Conversation,
    Message,
    Session,
    engine,
    get_messages,
    init_db,
)
from sqlalchemy import insert


@pytest.mark.asyncio
async def test_long_room_restores_latest_messages_in_chronological_order():
    await init_db()
    room = "long-history-room"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with Session.begin() as db:
        await db.execute(
            insert(Conversation),
            [
                {"id": "long-room", "room": room},
                {"id": "other-room", "room": "other-history-room"},
            ],
        )
        await db.execute(
            insert(Message),
            [
                {
                    "id": f"history-{i:04d}",
                    "conversation_id": "long-room",
                    "role": "user" if i % 2 == 0 else "assistant",
                    "text": str(i),
                    "created_at": start + timedelta(seconds=i // 2),
                }
                for i in range(650)
            ]
            + [
                {
                    "id": "history-other",
                    "conversation_id": "other-room",
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
    await engine.dispose()
