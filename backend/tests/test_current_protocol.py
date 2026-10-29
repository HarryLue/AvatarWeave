import json
from types import SimpleNamespace

import pytest
from avatarweave_agent.avatars import get_avatar
from avatarweave_agent.pipeline import PipelineConfig
from avatarweave_agent.protocol import COMMAND_TOPIC, COMMANDS
from avatarweave_agent.session_options import merge_options, read_metadata, read_options
from pydantic import ValidationError


def test_only_current_versioned_metadata_with_explicit_context():
    config = PipelineConfig(mode="mock")
    raw = merge_options(
        '{"context":{"request_id":"123"}}',
        avatar_id="kei",
        tts_voice=None,
        config=config,
    )
    data = json.loads(raw)
    assert set(data) == {"avatarweave_session", "context"}
    assert data["avatarweave_session"] == {"version": 1, "avatar_id": "kei"}
    assert data["context"] == {"request_id": "123"}
    assert read_options(raw).avatar_id == "kei"
    assert read_options("").version == 1
    for raw in [
        '{"avatar_session":{"version":1}}',
        '{"avatar_tts_voice":"zm_yunxi"}',
        '{"avatarweave_session":{"version":2}}',
        '{"avatarweave_session":{"version":true}}',
        '{"avatarweave_session":{}}',
        '{"context":{},"context":{}}',
        '{"context":{"number":NaN}}',
        '{"context":{"x":' + "[" * 1100 + "0" + "]" * 1100 + "}}",
        "not-json",
        "[]",
        "null",
        '{"context":' + " " * 4100 + "{}}",
    ]:
        with pytest.raises(ValueError):
            read_metadata(raw)


@pytest.mark.asyncio
async def test_exactly_one_current_action_packet():
    from avatarweave_agent.worker import publish_action

    _, avatar = get_avatar()
    packets = []

    async def publish(data, **kwargs):
        packets.append((json.loads(data), kwargs))

    ctx = SimpleNamespace(
        room=SimpleNamespace(local_participant=SimpleNamespace(publish_data=publish))
    )
    await publish_action(ctx, avatar)
    assert len(packets) == 1
    command, options = packets[0]
    assert options == {"topic": COMMAND_TOPIC, "reliable": True}
    assert COMMAND_TOPIC == "avatarweave.command.v1"
    assert COMMANDS.validate_python(command).version == 1


def test_command_schema_rejects_ambiguous_or_invalid_actions():
    assert (
        COMMANDS.validate_python(
            {"version": 1, "id": "x", "type": "expression", "name": "Smile"}
        ).name
        == "Smile"
    )
    for command in [
        {"version": 1, "id": "", "type": "motion", "name": ""},
        {"version": True, "id": "x", "type": "motion", "name": ""},
        {"version": 1, "id": "x", "type": "motion", "name": "", "index": True},
        {
            "version": 1,
            "id": "x",
            "type": "motion",
            "name": "",
            "index": 9007199254740992,
        },
        {"version": 1, "id": "x", "type": "expression", "name": ""},
        {"version": 1, "id": "x", "type": "expression", "name": "Smile", "index": 1},
        {"version": 1, "id": "x", "type": "motion", "name": "", "script": "x"},
    ]:
        with pytest.raises(ValidationError):
            COMMANDS.validate_python(command)
