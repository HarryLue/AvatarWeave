"""Memory rollout settings, independent of the existing provider configuration."""

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import ROOT


class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_prefix="MEMORY_", extra="ignore")
    enabled: bool = False
    rtc_enabled: bool = False
    database_url: str = Field(default="", repr=False)
    workspace_id: str = "default"
    companion_id: str = "companion"
    public_origin: str = ""
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    login_ttl_seconds: int = Field(default=86400, ge=300, le=604800)
    embedding_reference_env: Path | None = None
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = ""
    embedding_dimensions: int = Field(default=1024, ge=1, le=16000)
    embedding_revision: str = "configured.v1"
    archive_root: Path | None = None
    archive_key_root: Path | None = None
    archive_key_version: str = "v1"
    deletion_journal_path: Path | None = None

    @model_validator(mode="after")
    def configured_when_enabled(self):
        if not self.enabled:
            return self
        from urllib.parse import urlsplit

        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("Enabled memory requires an explicit PostgreSQL async database URL")
        for value in (self.public_origin, self.oidc_issuer):
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Memory origin and issuer require canonical HTTPS URLs")
        if urlsplit(self.public_origin).path not in {"", "/"}:
            raise ValueError("Memory public origin must not contain a path")
        self.public_origin = self.public_origin.rstrip("/")
        self.oidc_issuer = self.oidc_issuer.rstrip("/")
        if not self.oidc_client_id or len(self.session_secret.get_secret_value()) < 32:
            raise ValueError(
                "OIDC client ID and a random session secret of at least 32 chars required"
            )
        return self
