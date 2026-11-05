"""Configure and operate memory deployment without overwriting provider credentials."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import ssl
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from avatarweave_agent.memory.config import MemorySettings
from avatarweave_agent.memory.storage import MemoryDatabase
from avatarweave_agent.memory.utils import private_write
from dotenv import dotenv_values
from pydantic import ValidationError

PROFILE = ROOT / ".runtime/memory-deployment.env"
INFO = ROOT / ".runtime/memory-deployment.json"
CREDENTIALS = ROOT / ".runtime/memory-auth/credentials.json"
REALM = "avatarweave-memory"


def environment(include_profile=True):
    values = {
        key: value
        for key, value in dotenv_values(ROOT / ".env").items()
        if value is not None
    }
    if include_profile and PROFILE.exists():
        values.update(
            {
                key: value
                for key, value in dotenv_values(PROFILE).items()
                if value is not None
            }
        )
    values.update(os.environ)
    return values


def settings_for(values):
    return MemorySettings(
        _env_file=None,
        **{
            key.removeprefix("MEMORY_").lower(): value
            for key, value in values.items()
            if key.startswith("MEMORY_")
        },
    )


def run(command, *, env=None):
    return subprocess.run(
        [str(part) for part in command], cwd=ROOT, env=env, check=True
    )


def compose(action, env):
    run(
        [
            "docker",
            "compose",
            "--project-name",
            "avatarweave-memory-auth-"
            + hashlib.sha256(str(ROOT).encode()).hexdigest()[:8],
            "--env-file",
            PROFILE,
            "-f",
            ROOT / "infra/memory-auth.compose.yaml",
            *action,
        ],
        env=env,
    )


def initialize(args):
    from avatarweave_agent.config import settings as app_settings

    values = environment(include_profile=False)
    previous = dotenv_values(PROFILE) if PROFILE.exists() else {}
    for key in (
        "MEMORY_PUBLIC_ORIGIN",
        "MEMORY_SESSION_SECRET",
        "MEMORY_ARCHIVE_ROOT",
        "MEMORY_ARCHIVE_KEY_ROOT",
        "MEMORY_ARCHIVE_KEY_VERSION",
        "MEMORY_DELETION_JOURNAL_PATH",
    ):
        if not values.get(key) and previous.get(key):
            values[key] = previous[key]
    current = settings_for({**values, "MEMORY_ENABLED": "false"})
    origin = args.origin or values.get("MEMORY_PUBLIC_ORIGIN")
    if not origin:
        hosts = json.loads((ROOT / "infra/https.json").read_text())["hosts"]
        origin = "https://" + hosts[0]
    origin = origin.rstrip("/")
    old = json.loads(INFO.read_text()) if INFO.exists() else None
    local_auth = args.local_auth or (
        not args.external_auth and bool(old and old["local_auth"])
    )
    if local_auth and old and old["local_auth"] and origin != old["origin"]:
        raise ValueError(
            "Local issuer origin changed; migrate its redirect configuration explicitly before reinitializing"
        )
    if not current.database_url:
        raise ValueError(
            "Set MEMORY_DATABASE_URL in .env to a new dedicated PostgreSQL database first"
        )
    if current.database_url == app_settings.database_url:
        raise ValueError(
            "Memory must use a database distinct from legacy conversation history"
        )
    values.update(
        {
            "MEMORY_ENABLED": "true",
            "MEMORY_RTC_ENABLED": "true",
            "MEMORY_PUBLIC_ORIGIN": origin,
            "MEMORY_DATABASE_URL": current.database_url,
            "MEMORY_SESSION_SECRET": values.get("MEMORY_SESSION_SECRET")
            or secrets.token_urlsafe(48),
            "MEMORY_ARCHIVE_ROOT": values.get("MEMORY_ARCHIVE_ROOT")
            or str(ROOT / ".runtime/memory/objects"),
            "MEMORY_ARCHIVE_KEY_ROOT": values.get("MEMORY_ARCHIVE_KEY_ROOT")
            or str(ROOT / ".runtime/memory/keys"),
            "MEMORY_ARCHIVE_KEY_VERSION": values.get("MEMORY_ARCHIVE_KEY_VERSION")
            or "v1",
            "MEMORY_DELETION_JOURNAL_PATH": values.get("MEMORY_DELETION_JOURNAL_PATH")
            or str(ROOT / ".runtime/memory/deletion-journal.sqlite"),
            "RTC_PUBLIC_URL": origin.replace("https://", "wss://", 1) + "/realtime",
        }
    )
    if local_auth:
        credentials = (
            json.loads(CREDENTIALS.read_text())
            if CREDENTIALS.exists()
            else {"username": "memory-demo", "password": secrets.token_urlsafe(24)}
        )
        values.update(
            {
                "MEMORY_OIDC_ISSUER": origin + "/idp/realms/" + REALM,
                "MEMORY_OIDC_CLIENT_ID": "avatarweave-memory",
                "MEMORY_OIDC_CLIENT_SECRET": "",
            }
        )
    if not local_auth:
        values.update(
            {
                "MEMORY_OIDC_ISSUER": current.oidc_issuer,
                "MEMORY_OIDC_CLIENT_ID": current.oidc_client_id,
                "MEMORY_OIDC_CLIENT_SECRET": current.oidc_client_secret.get_secret_value(),
            }
        )
    settings_for(values)  # Validate before creating any profile/realm files.
    frozen = {
        "MEMORY_ENABLED",
        "MEMORY_RTC_ENABLED",
        "MEMORY_PUBLIC_ORIGIN",
        "MEMORY_DATABASE_URL",
        "MEMORY_SESSION_SECRET",
        "MEMORY_ARCHIVE_ROOT",
        "MEMORY_ARCHIVE_KEY_ROOT",
        "MEMORY_ARCHIVE_KEY_VERSION",
        "MEMORY_DELETION_JOURNAL_PATH",
        "MEMORY_OIDC_ISSUER",
        "MEMORY_OIDC_CLIENT_ID",
        "MEMORY_OIDC_CLIENT_SECRET",
        "RTC_PUBLIC_URL",
    }
    profile_values = {key: value for key, value in values.items() if key in frozen}
    # Keep the mount reference when switching away from local authentication so
    # start/stop can still stop the old project without removing its data volume.
    if previous.get("MEMORY_REALM_IMPORT_FILE"):
        profile_values["MEMORY_REALM_IMPORT_FILE"] = previous[
            "MEMORY_REALM_IMPORT_FILE"
        ]
    if local_auth:
        CREDENTIALS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        CREDENTIALS.parent.chmod(0o700)
        private_write(CREDENTIALS, json.dumps(credentials))
        realm_path = CREDENTIALS.parent / "realm.json"
        realm = {
            "realm": REALM,
            "enabled": True,
            "registrationAllowed": False,
            "bruteForceProtected": True,
            "sslRequired": "external",
            "clients": [
                {
                    "clientId": "avatarweave-memory",
                    "publicClient": True,
                    "standardFlowEnabled": True,
                    "directAccessGrantsEnabled": False,
                    "redirectUris": [origin + "/api/v2/auth/callback"],
                    "webOrigins": [origin],
                    "attributes": {"pkce.code.challenge.method": "S256"},
                }
            ],
            "users": [
                {
                    "username": credentials["username"],
                    "enabled": True,
                    "email": "memory-demo@example.invalid",
                    "emailVerified": True,
                    "firstName": "Memory",
                    "lastName": "Demo",
                    "requiredActions": [],
                    "credentials": [
                        {
                            "type": "password",
                            "value": credentials["password"],
                            "temporary": False,
                        }
                    ],
                }
            ],
        }
        private_write(realm_path, json.dumps(realm))
        # Direct bind mount is readable by Keycloak's container UID. The host
        # directory remains 0700; neither this file nor .runtime is web-served.
        realm_path.chmod(0o644)
        profile_values["MEMORY_REALM_IMPORT_FILE"] = str(realm_path)
    private_write(
        PROFILE,
        "".join(
            key + "=" + json.dumps(value, ensure_ascii=False) + "\n"
            for key, value in sorted(profile_values.items())
        ),
    )
    private_write(
        INFO,
        json.dumps(
            {
                "origin": origin,
                "local_auth": local_auth,
                "auth_managed": local_auth
                or bool(old and old.get("auth_managed", old.get("local_auth"))),
                "profile": str(PROFILE),
                "credentials_file": str(CREDENTIALS) if local_auth else None,
            }
        ),
    )
    print(
        json.dumps(
            {
                "state": "configured",
                "origin": origin,
                "local_auth": local_auth,
                "profile": str(PROFILE),
                "credentials_file": str(CREDENTIALS) if local_auth else None,
            }
        )
    )


async def check(env, online=False):
    settings = settings_for(env)
    if not settings.enabled:
        raise ValueError(
            "Run init first; the deployment profile must explicitly enable memory"
        )
    database = MemoryDatabase(settings.database_url)
    try:
        await database.check_schema()
    finally:
        await database.close()
    from avatarweave_agent.memory.archive import FileKeyProvider
    from avatarweave_agent.memory.embedding import EmbeddingGateway
    from avatarweave_agent.pipeline import load_pipeline

    if not settings.archive_root or not settings.archive_key_root:
        raise ValueError("Configure the archive and key directories, then run migrate")
    FileKeyProvider(settings.archive_key_root).get_key(settings.archive_key_version)
    if load_pipeline().mode == "mock" and settings.rtc_enabled:
        raise ValueError(
            "Private memory RTC needs PIPELINE_MODE=pipeline and real providers"
        )
    embedding = EmbeddingGateway.from_settings(settings)
    profile = embedding.profile.profile_id if embedding else "lexical-only"
    if embedding:
        await embedding.close()
    if online:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                settings.oidc_issuer + "/.well-known/openid-configuration"
            )
            response.raise_for_status()
            metadata = response.json()
            if metadata.get(
                "issuer"
            ) != settings.oidc_issuer or "S256" not in metadata.get(
                "code_challenge_methods_supported", []
            ):
                raise ValueError(
                    "OIDC discovery issuer/PKCE does not match the configured provider"
                )
    print(
        json.dumps(
            {
                "state": "configuration_ready",
                "schema_version": 3,
                "origin": settings.public_origin,
                "embedding_profile": profile,
                "rtc_enabled": settings.rtc_enabled,
                "oidc_online_checked": online,
                "joint_release_acceptance": "not_measured",
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "init",
            "migrate",
            "check",
            "auth-up",
            "start",
            "restart",
            "stop",
            "status",
            "credentials",
        ],
    )
    auth_options = parser.add_mutually_exclusive_group()
    auth_options.add_argument(
        "--local-auth",
        action="store_true",
        help="Optional self-hosted development Keycloak",
    )
    auth_options.add_argument(
        "--external-auth", action="store_true", help="Use OIDC settings from .env"
    )
    parser.add_argument("--origin", help="Canonical browser-facing HTTPS origin")
    parser.add_argument(
        "--online", action="store_true", help="Also verify OIDC discovery over HTTPS"
    )
    args = parser.parse_args()
    if args.action == "init":
        initialize(args)
        return
    env = environment()
    if args.action == "credentials":
        # Only this explicit command prints the development login credential.
        print(CREDENTIALS.read_text())
        return
    info = json.loads(INFO.read_text()) if INFO.exists() else {"local_auth": False}
    if args.action == "migrate":
        run(
            [
                ROOT / "backend/.venv/bin/python",
                ROOT / "scripts/memory-schema.py",
                "--apply",
            ],
            env=env,
        )
        settings = settings_for(env)
        run(
            [
                ROOT / "backend/.venv/bin/python",
                ROOT / "scripts/memory-admin.py",
                "init-store",
                "--root",
                ROOT / ".runtime/memory",
                "--key-version",
                settings.archive_key_version,
                "--objects",
                settings.archive_root,
                "--keys",
                settings.archive_key_root,
            ],
            env=env,
        )
        return
    if args.action == "check":
        asyncio.run(check(env, args.online))
        return
    if args.action in {"auth-up", "start", "restart"}:
        asyncio.run(check(env))
        if info["local_auth"]:
            compose(["up", "-d"], env)
        elif info.get("auth_managed"):
            compose(["stop"], env)
        if args.action == "auth-up":
            return
        run(
            [
                ROOT / "backend/.venv/bin/python",
                ROOT / "scripts/dev.py",
                "--env-file",
                PROFILE,
                *(["--restart"] if args.action == "restart" else []),
            ],
            env=env,
        )
    elif args.action == "stop":
        run(
            [ROOT / "backend/.venv/bin/python", ROOT / "scripts/dev.py", "--stop"],
            env=env,
        )
        if info.get("auth_managed", info["local_auth"]):
            compose(["stop"], env)
    elif args.action == "status":
        run(
            [ROOT / "backend/.venv/bin/python", ROOT / "scripts/dev.py", "--status"],
            env=env,
        )
        if info.get("auth_managed", info["local_auth"]):
            compose(["ps"], env)


if __name__ == "__main__":
    try:
        main()
    except ValidationError:
        raise SystemExit(
            "Memory configuration is incomplete or invalid; check .env, origin and OIDC client settings"
        ) from None
    except (ValueError, OSError, ssl.SSLError) as error:
        raise SystemExit(str(error)) from None
