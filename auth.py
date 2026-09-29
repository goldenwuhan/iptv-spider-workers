"""auth.py -- password login + signed-cookie session for the web console.

Matches the original IPTV-Spider, which protected everything behind a single
password:

  * One admin password, resolved in this order:
      1. ``ADMIN_PASSWORD`` from the Worker secret / environment (preferred),
      2. the ``admin_password`` row in the D1 ``settings`` table,
      3. a built-in default that the login page warns you to change.
  * On success we issue a *stateless* cookie: ``base64(json) + "." + HMAC``.
    No session table required; verification is constant-time.
  * The signing secret is generated once and kept in ``settings``.

Stdlib only, so it behaves identically on Workers and locally.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

COOKIE_NAME = "iptv_session"
SESSION_TTL = 7 * 24 * 3600          # 7 days
DEFAULT_PASSWORD = "iptv-spider"     # surfaced as a hint on the login page
SECRET_KEY = "session_secret"
PASSWORD_KEY = "admin_password"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse_cookies(header) -> dict:
    """Parse a raw ``Cookie`` header into a dict."""
    out: dict[str, str] = {}
    for part in str(header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue(secret: str, ttl: int = SESSION_TTL) -> str:
    """Create a signed session token."""
    payload = _b64(json.dumps({"exp": int(time.time()) + ttl}).encode())
    return f"{payload}.{_sign(secret, payload)}"


def verify(secret: str, token: str) -> bool:
    """Validate a session token: signature + expiry."""
    if not secret or not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return False
    try:
        data = json.loads(_unb64(payload))
    except Exception:
        return False
    return int(data.get("exp", 0)) > time.time()


# ---------------------------------------------------------------------------
# password / secret resolution (backed by the settings store)
# ---------------------------------------------------------------------------

async def get_password(settings, env=None) -> str:
    """Resolve the admin password (env wins over the stored one)."""
    if env is not None:
        env_pw = getattr(env, "ADMIN_PASSWORD", None)
        if env_pw:
            return str(env_pw)
    stored = await settings.get(PASSWORD_KEY)
    if stored:
        return stored
    await settings.set(PASSWORD_KEY, DEFAULT_PASSWORD)
    return DEFAULT_PASSWORD


async def set_password(settings, new_password: str) -> None:
    await settings.set(PASSWORD_KEY, new_password)


async def get_secret(settings) -> str:
    """Get (or lazily create) the cookie signing secret."""
    secret = await settings.get(SECRET_KEY)
    if not secret:
        secret = secrets.token_hex(32)
        await settings.set(SECRET_KEY, secret)
    return secret


def check_password(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(str(candidate or ""), str(expected or ""))


# ---------------------------------------------------------------------------
# request-level helpers
# ---------------------------------------------------------------------------

def cookie_header(token: str, ttl: int = SESSION_TTL) -> str:
    """Build the ``Set-Cookie`` value for a successful login."""
    return (f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={ttl}")


def clear_cookie() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def token_from_headers(headers) -> str:
    """Extract our session token from a request's headers (if any)."""
    cookies = parse_cookies(headers.get("Cookie") if hasattr(headers, "get") else None)
    return cookies.get(COOKIE_NAME, "")
