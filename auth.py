"""Auth pieces: config loading, Google OAuth provider, allow-list gate, dev bypass,
and working out which assistant (claude or chatgpt) is calling."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from starlette.responses import JSONResponse

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token, get_http_headers

from store import ASSISTANTS

REQUIRED_SCOPES = ["openid", "email"]  # the only two the OAuth app asks Google for


def load_env(path: str | None) -> dict[str, str]:
    """Read KEY=VALUE lines from the .env file into os.environ (existing vars win)."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)
    return dict(os.environ)


class Config:
    def __init__(self) -> None:
        env = os.environ
        self.store_dir = env.get("STORE_DIR", "/home/pi/household")
        self.bind_host = env.get("BIND_HOST", "127.0.0.1")
        self.bind_port = int(env.get("BIND_PORT", "8000"))
        self.base_url = env.get("BASE_URL", "").rstrip("/")
        self.client_id = env.get("GOOGLE_CLIENT_ID", "")
        self.client_secret = env.get("GOOGLE_CLIENT_SECRET", "")
        self.allowed_emails = [e.strip().lower() for e in env.get("ALLOWED_EMAILS", "").split(",") if e.strip()]
        self.dev_bypass = env.get("DEV_BYPASS", "")
        # Optional display names for the attribution stamp: "owner@x.com=rob,partner@y.com=partner"
        self.stamp_names: dict[str, str] = {}
        for pair in env.get("STAMP_NAMES", "").split(","):
            if "=" in pair:
                e, n = pair.split("=", 1)
                self.stamp_names[e.strip().lower()] = n.strip()

    def validate(self) -> None:
        problems = []
        if not self.base_url:
            problems.append("BASE_URL is not set")
        if not self.allowed_emails:
            problems.append("ALLOWED_EMAILS is empty")
        if self.bind_host not in ("127.0.0.1", "localhost", "::1"):
            problems.append(f"BIND_HOST={self.bind_host!r}; the server must bind to loopback only")
        if self.dev_bypass:
            host = urlparse(self.base_url).hostname or ""
            if host not in ("localhost", "127.0.0.1", "::1"):
                problems.append("DEV_BYPASS is set but BASE_URL is not localhost; refusing to start")
        elif not (self.client_id and self.client_secret):
            problems.append("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set")
        if problems:
            for p in problems:
                print(f"config error: {p}", file=sys.stderr)
            sys.exit(2)

    def is_allowed(self, email: str | None) -> bool:
        return bool(email) and email.strip().lower() in self.allowed_emails

    def stamp_user(self, email: str) -> str:
        email = (email or "").lower()
        if email in self.stamp_names:
            return self.stamp_names[email]
        local = email.split("@", 1)[0]
        return re.split(r"[.+_]", local, 1)[0] or local or "unknown"

    def assistant_for_email(self, email: str | None) -> str | None:
        """Fallback: position in ALLOWED_EMAILS (first = owner/claude, second = partner/chatgpt)."""
        email = (email or "").lower()
        if email in self.allowed_emails:
            i = self.allowed_emails.index(email)
            if i < len(ASSISTANTS):
                return ASSISTANTS[i]
        return None


class DevTokenVerifier(TokenVerifier):
    """Local-only bypass. Token is DEV_BYPASS (acts as the first allowed email) or
    DEV_BYPASS + '@' + email, to exercise the allow-list and the chatgpt side."""

    def __init__(self, secret: str, default_email: str):
        super().__init__(required_scopes=[])
        self.secret, self.default_email = secret, default_email

    async def verify_token(self, token: str) -> AccessToken | None:
        if token == self.secret:
            email = self.default_email
        elif token.startswith(self.secret + "@"):
            email = token[len(self.secret) + 1:]
        else:
            return None
        return AccessToken(token=token, client_id="dev-bypass", scopes=REQUIRED_SCOPES, subject=email,
                           claims={"email": email, "email_verified": True, "sub": email})


def build_auth(cfg: Config):
    if cfg.dev_bypass:
        print("DEV_BYPASS active: Google OAuth is OFF (localhost only)", file=sys.stderr)
        return DevTokenVerifier(cfg.dev_bypass, cfg.allowed_emails[0])
    from fastmcp.server.auth.providers.google import GoogleProvider

    return GoogleProvider(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        base_url=cfg.base_url,
        required_scopes=REQUIRED_SCOPES,
        cache_ttl_seconds=300,
    )


class AllowListMiddleware:
    """Pure ASGI middleware, mounted inside FastMCP's AuthenticationMiddleware.
    If the bearer token verified but the Google email is not on the allow-list,
    answer 403 here, before the MCP layer sees the request."""

    def __init__(self, app, cfg: Config):
        self.app, self.cfg = app, cfg

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = scope.get("user")
            token = getattr(user, "access_token", None)
            if token is not None:
                email = (token.claims or {}).get("email")
                if not self.cfg.is_allowed(email):
                    resp = JSONResponse(
                        {"error": "forbidden", "error_description": "this Google account is not on the household allow-list"},
                        status_code=403,
                        headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
                    )
                    await resp(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class Caller:
    def __init__(self, email: str, user: str, assistant: str, how: str):
        self.email, self.user, self.assistant, self.how = email, user, assistant, how


_client_names: dict[str, str | None] = {}


def _classify(*hints: str | None) -> str | None:
    for h in hints:
        h = (h or "").lower()
        if not h:
            continue
        if "claude" in h or "anthropic" in h:
            return "claude"
        if "chatgpt" in h or "openai" in h:
            return "chatgpt"
    return None


async def current_caller(cfg: Config, auth) -> Caller:
    """Identify the signed-in email and the calling assistant for this tool call."""
    token = get_access_token()
    if token is None:
        raise PermissionError("not authenticated")
    email = (token.claims or {}).get("email") or ""
    if not cfg.is_allowed(email):
        raise PermissionError("this Google account is not on the household allow-list")

    client_name = None
    if hasattr(auth, "get_client") and token.client_id and token.client_id != "dev-bypass":
        if token.client_id not in _client_names:
            try:
                info = await auth.get_client(token.client_id)
                _client_names[token.client_id] = getattr(info, "client_name", None) if info else None
            except Exception:
                _client_names[token.client_id] = None
        client_name = _client_names[token.client_id]
    headers = get_http_headers(include_all=True)
    ua = headers.get("user-agent", "")

    assistant = _classify(client_name)
    how = "oauth-client"
    if assistant is None:
        assistant = _classify(ua)
        how = "user-agent"
    if assistant is None:
        assistant = cfg.assistant_for_email(email)
        how = "email-order"
    if assistant is None:
        assistant = "unknown"
        how = "unknown"
    return Caller(email=email, user=cfg.stamp_user(email), assistant=assistant, how=how)
