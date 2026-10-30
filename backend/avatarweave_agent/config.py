from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    database_url: str = f"sqlite+aiosqlite:///{ROOT / 'avatar.db'}"
    livekit_url: str = "ws://127.0.0.1:7880"
    rtc_public_url: str = ""
    livekit_api_key: str = "devkey"
    livekit_api_secret: str = "avatar-kit-local-development-secret-2026"
    mock_tts_preview: bool = False
    kokoro_managed: bool = True
    avatar_config: Path = ROOT / "config/avatars.yaml"
    voice_config: Path = ROOT / "config/voices.yaml"
    agent_history_limit: int = Field(default=100, ge=1, le=2000)
    kokoro_base_url: str = "http://127.0.0.1:8880/v1"
    kokoro_model_dir: Path = ROOT / "models/kokoro"
    kokoro_precision: Literal["fp32"] = "fp32"
    kokoro_device: Literal["cpu", "gpu"] = "cpu"
    kokoro_gpu_id: int = Field(default=0, ge=0)
    kokoro_cuda_lib_dir: str | None = None
    kokoro_threads: int = Field(default=4, ge=1, le=64)
    agent_http_proxy: str | None = None
    agent_name: str = "avatar-assistant"
    agent_health_port: int = Field(default=8091, ge=1, le=65535)
    practice_daily_limit: int = Field(default=50, ge=1, le=10000)
    practice_daily_attempt_limit: int = Field(default=250, ge=1, le=10000)
    api_access_key: str = ""
    allowed_agents: str = "avatar-assistant"
    agent_instructions: str = "你是一位友好的中文数字人助手。用简短、自然的口语回答。"

    @model_validator(mode="after")
    def validate_config(self):
        if self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
        if not self.database_url.startswith(("sqlite+aiosqlite://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL must use SQLite or PostgreSQL async driver")
        return self


settings = Settings()
