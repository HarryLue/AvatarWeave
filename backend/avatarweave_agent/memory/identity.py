"""OIDC Authorization Code + PKCE with Authlib; opaque revocable browser logins."""

import hashlib
import hmac
import secrets
from datetime import timedelta

from authlib.integrations.starlette_client import OAuth
from sqlalchemy import delete, select

from .execution import MemoryFault
from .service import utc
from .storage import Account, Login

AUTH_COOKIE = "__Host-avatarweave-account"
CSRF_COOKIE = "__Host-avatarweave-csrf"


def digest(secret: str):
    return hashlib.sha256(secret.encode()).hexdigest()


class IdentityService:
    def __init__(self, settings, memory):
        self.settings = settings
        self.memory = memory
        self.oauth = OAuth()
        self.oauth.register(
            "account",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret.get_secret_value(),
            server_metadata_url=settings.oidc_issuer + "/.well-known/openid-configuration",
            client_kwargs={"scope": "openid", "code_challenge_method": "S256", "timeout": 10.0},
        )

    async def establish(self, claims, response):
        """Claims must be the validated id_token result from Authlib, never userinfo/body."""
        if (
            claims.get("iss") != self.settings.oidc_issuer
            or not isinstance(claims.get("sub"), str)
            or not claims["sub"]
            or len(claims["sub"]) > 255
        ):
            raise MemoryFault("unauthorized", "Invalid identity")
        account_id = await self.memory.resolve_account(claims["iss"], claims["sub"])
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        expires = self.memory.clock() + timedelta(seconds=self.settings.login_ttl_seconds)
        async with self.memory.database.sessions.begin() as db:
            db.add(
                Login(
                    token_digest=digest(token),
                    account_id=account_id,
                    csrf_digest=digest(csrf),
                    expires_at=expires,
                )
            )
        for name, value, http_only in [(AUTH_COOKIE, token, True), (CSRF_COOKIE, csrf, False)]:
            response.set_cookie(
                name,
                value,
                max_age=self.settings.login_ttl_seconds,
                secure=True,
                httponly=http_only,
                samesite="lax",
                path="/",
            )
        return account_id

    async def authenticate(self, request, *, write=False):
        token = request.cookies.get(AUTH_COOKIE, "")
        if not token or len(token) > 128:
            raise MemoryFault("unauthorized", "Account login required")
        async with self.memory.database.sessions() as db:
            login = await db.get(Login, digest(token))
            if login is None or utc(login.expires_at) <= self.memory.clock():
                raise MemoryFault("unauthorized", "Account login required")
            if write:
                csrf = request.headers.get("x-csrf-token", "")
                if (
                    request.headers.get("origin") != self.settings.public_origin
                    or len(csrf) > 128
                    or not csrf
                    or not hmac.compare_digest(login.csrf_digest, digest(csrf))
                ):
                    raise MemoryFault("unauthorized", "Origin or CSRF validation failed")
            account = await db.scalar(
                select(Account).where(
                    Account.id == login.account_id,
                    Account.workspace_id == self.settings.workspace_id,
                )
            )
            if account is None:
                raise MemoryFault("unauthorized", "Account login required")
            return account

    async def logout(self, request):
        await self.authenticate(request, write=True)
        async with self.memory.database.sessions.begin() as db:
            await db.execute(
                delete(Login).where(Login.token_digest == digest(request.cookies[AUTH_COOKIE]))
            )
