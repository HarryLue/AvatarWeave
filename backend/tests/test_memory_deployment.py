"""Deployment initialization must be repeatable without changing live credentials."""

import argparse
import importlib.util
import json
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/memory-deploy.py"
    spec = importlib.util.spec_from_file_location("memory_deployment_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in tuple(os.environ):
        if key.startswith("MEMORY_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "PROFILE", tmp_path / ".runtime/memory-deployment.env")
    monkeypatch.setattr(module, "INFO", tmp_path / ".runtime/memory-deployment.json")
    monkeypatch.setattr(module, "CREDENTIALS", tmp_path / ".runtime/memory-auth/credentials.json")
    (tmp_path / ".env").write_text(
        "MEMORY_ENABLED=false\nMEMORY_RTC_ENABLED=false\n"
        "MEMORY_DATABASE_URL=postgresql+asyncpg://test:private@db.test/dedicated_memory\n"
        "MEMORY_EMBEDDING_API_KEY=provider-private-key\n"
    )
    return module


def arguments(**overrides):
    return argparse.Namespace(
        **{
            "origin": "https://memory.example.test",
            "local_auth": True,
            "external_auth": False,
            **overrides,
        }
    )


def test_local_init_keeps_credentials_and_provider_environment(deployment, capsys):
    module = deployment
    original = (module.ROOT / ".env").read_bytes()
    module.initialize(arguments())
    first_profile = module.PROFILE.read_bytes()
    first_credentials = module.CREDENTIALS.read_bytes()
    module.initialize(arguments())
    assert module.PROFILE.read_bytes() == first_profile
    assert module.CREDENTIALS.read_bytes() == first_credentials
    assert (module.ROOT / ".env").read_bytes() == original
    profile = dotenv_values(module.PROFILE)
    assert profile["MEMORY_ENABLED"] == profile["MEMORY_RTC_ENABLED"] == "true"
    assert "MEMORY_EMBEDDING_API_KEY" not in profile
    assert len(profile["MEMORY_SESSION_SECRET"]) >= 32
    assert profile["MEMORY_OIDC_ISSUER"].endswith("/idp/realms/avatarweave-memory")
    realm = json.loads(Path(profile["MEMORY_REALM_IMPORT_FILE"]).read_text())
    client = realm["clients"][0]
    assert client["publicClient"] and not client["directAccessGrantsEnabled"]
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["redirectUris"] == ["https://memory.example.test/api/v2/auth/callback"]
    assert not realm["registrationAllowed"]
    assert json.loads(first_credentials)["password"] not in capsys.readouterr().out
    for path in (module.PROFILE, module.INFO, module.CREDENTIALS):
        assert path.stat().st_mode & 0o777 == 0o600
    assert module.CREDENTIALS.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError, match="origin changed"):
        module.initialize(arguments(origin="https://changed.example.test"))
    assert module.PROFILE.read_bytes() == first_profile


def test_invalid_configuration_creates_no_deployment_files(deployment, monkeypatch):
    module = deployment
    with pytest.raises(ValidationError):
        module.initialize(arguments(origin="http://memory.example.test"))
    assert not module.PROFILE.exists() and not module.CREDENTIALS.exists()
    from avatarweave_agent.config import settings

    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://test:private@db.test/dedicated_memory"
    )
    with pytest.raises(ValueError, match="distinct"):
        module.initialize(arguments())
    assert not module.INFO.exists()


def test_external_auth_switch_preserves_local_data_and_stops_owned_auth(deployment, monkeypatch):
    module = deployment
    module.initialize(arguments())
    credentials = module.CREDENTIALS.read_bytes()
    monkeypatch.setenv("MEMORY_OIDC_ISSUER", "https://login.example.test/realm")
    monkeypatch.setenv("MEMORY_OIDC_CLIENT_ID", "external-memory")
    module.initialize(arguments(local_auth=False, external_auth=True))
    info = json.loads(module.INFO.read_text())
    assert info["auth_managed"] and not info["local_auth"]
    profile = dotenv_values(module.PROFILE)
    assert profile["MEMORY_OIDC_CLIENT_ID"] == "external-memory"
    assert Path(profile["MEMORY_REALM_IMPORT_FILE"]).exists()
    calls = []

    async def checked(*args, **kwargs):
        pass

    monkeypatch.setattr(module, "check", checked)
    monkeypatch.setattr(module, "compose", lambda action, env: calls.append(action))
    monkeypatch.setattr(module, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(module.sys, "argv", ["memory-deploy.py", "start"])
    module.main()
    assert calls[0] == ["stop"]
    assert "--env-file" in calls[1]
    assert module.CREDENTIALS.read_bytes() == credentials


def test_external_auth_requires_real_issuer_before_writing_profile(deployment):
    with pytest.raises(ValidationError):
        deployment.initialize(arguments(local_auth=False, external_auth=True))
    assert not deployment.PROFILE.exists()
