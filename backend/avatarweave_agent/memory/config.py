"""Memory rollout settings, independent of the existing provider configuration."""

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import ROOT


class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_prefix="MEMORY_", extra="ignore")
    enabled: bool = False
    database_url: str = Field(default="", repr=False)
    workspace_id: str = "default"
    companion_id: str = "companion"
    public_origin: str = ""
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    login_ttl_seconds: int = Field(default=86400, ge=300, le=604800)

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
