import hmac
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from google.protobuf.json_format import ParseDict
from pydantic import BaseModel, ConfigDict, Field

from livekit import api

from .avatars import load_avatars
from .config import settings
from .db import engine, init_db
from .pipeline import load_pipeline
from .practice import router as practice_router
from .session_options import merge_options
from .voices import open_preview, voice_service


@asynccontextmanager
async def lifespan(app):
    await init_db()
    yield
    await engine.dispose()


app = FastAPI(title="AvatarWeave", version="0.1.0", lifespan=lifespan)

app.include_router(practice_router)


async def authenticate(authorization: str = Header(default="")):
    if settings.api_access_key and not hmac.compare_digest(
        authorization, f"Bearer {settings.api_access_key}"
    ):
        raise HTTPException(401, "Invalid access key")


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    room_name: str = Field(
        default_factory=lambda: f"avatar-{uuid4().hex}",
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )
    participant_identity: str = Field(
        default_factory=lambda: f"user-{uuid4().hex}", min_length=1, max_length=128
    )
    participant_name: str = Field(default="Guest", max_length=128)
    participant_metadata: str = Field(default="", max_length=4096)
    participant_attributes: dict[str, str] = Field(default_factory=dict)
    room_config: dict | None = None
    tts_voice: str | None = Field(default=None, min_length=1, max_length=128)
    avatar_id: str | None = Field(default=None, min_length=1, max_length=128)


@app.get("/api/health")
async def health():
    from sqlalchemy import text

    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "protocol_version": 1,
        "pipeline": load_pipeline().summary(),
        "database": engine.dialect.name,
    }


@app.post("/api/token", status_code=201, dependencies=[Depends(authenticate)])
async def token(body: TokenRequest, request: Request):
    if len(body.participant_attributes) > 16 or any(
        k.startswith("lk.") or len(k) > 128 or len(v) > 1024
        for k, v in body.participant_attributes.items()
    ):
        raise HTTPException(422, "Invalid participant attributes")
    allowed = {a.strip() for a in settings.allowed_agents.split(",") if a.strip()}
    raw = body.room_config or {"agents": [{"agent_name": settings.agent_name}]}
    if (
        set(raw) - {"agents"}
        or not isinstance(raw.get("agents"), list)
        or len(raw["agents"]) != 1
    ):
        raise HTTPException(422, "Exactly one allowed agent is required")
    for agent in raw["agents"]:
        if (
            not isinstance(agent, dict)
            or set(agent) - {"agent_name", "metadata"}
            or agent.get("agent_name") not in allowed
        ):
            raise HTTPException(403, "Agent is not allowed")
        if (
            not isinstance(agent.get("metadata", ""), str)
            or len(agent.get("metadata", "").encode()) > 4096
        ):
            raise HTTPException(422, "Agent metadata too large")
    agent = raw["agents"][0]
    try:
        metadata = merge_options(
            agent.get("metadata", ""),
            avatar_id=body.avatar_id,
            tts_voice=body.tts_voice,
            config=load_pipeline(),
        )
    except ValueError as exc:
        raise HTTPException(422, "Invalid avatar, voice or session metadata") from exc
    raw = {**raw, "agents": [{**agent, "metadata": metadata}]}
    try:
        room_config = ParseDict(raw, api.RoomConfiguration())
    except Exception as exc:
        raise HTTPException(422, "Invalid room_config") from exc
    jwt = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(body.participant_identity)
        .with_name(body.participant_name)
        .with_metadata(body.participant_metadata)
        .with_attributes(body.participant_attributes)
        .with_ttl(timedelta(minutes=15))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=body.room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_room_config(room_config)
        .to_jwt()
    )
    response = {
        "server_url": settings.livekit_url,
        "participant_token": jwt,
        "room_name": body.room_name,
    }
    public_url = settings.rtc_public_url or request.headers.get("x-avatarweave-rtc-url")
    if public_url:
        response["server_url"] = public_url
    return response


@app.get("/api/avatars")
async def avatars():
    document = load_avatars()
    return {
        "version": document.version,
        "default_avatar": document.default_avatar,
        "avatars": [
            {"id": key, **value.model_dump(exclude_none=True)}
            for key, value in document.avatars.items()
        ],
    }


@app.get("/api/tts/voices")
async def tts_voices():
    return voice_service().describe()


class VoicePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice: str = Field(min_length=1, max_length=128)


@app.post("/api/tts/preview", dependencies=[Depends(authenticate)])
async def preview(body: VoicePreview):
    service = voice_service()
    if service.spec is None:
        raise HTTPException(409, "TTS preview is not enabled")
    try:
        service.select(body.voice)
    except ValueError as exc:
        raise HTTPException(422, "Unsupported TTS voice") from exc
    try:
        playback = await open_preview(service, body.voice)
    except Exception as exc:
        raise HTTPException(503, "TTS preview is unavailable") from exc
    return PreviewResponse(playback)


class PreviewResponse(StreamingResponse):
    def __init__(self, playback):
        self.playback = playback
        super().__init__(
            playback.chunks(), media_type="audio/pcm", headers=playback.headers
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.playback.close()
