"""Authentication — bcrypt password hashing + JWT HTTP-only cookie sessions."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Cookie, HTTPException, Request, status

logger = logging.getLogger("fall_detection_web")

# Lazy imports so startup is fast if passlib/jose not yet installed
try:
    import bcrypt as _bcrypt
    _BCRYPT_OK = True
except ImportError:
    _BCRYPT_OK = False
    logger.error("bcrypt not installed — authentication unavailable")

# Secret for JWT signing — loaded from env or generated at startup
_JWT_SECRET: str = ""
_JWT_ALGORITHM = "HS256"
_SESSION_HOURS = 8

try:
    from jose import jwt as _jwt
    _JOSE_OK = True
except ImportError:
    _JOSE_OK = False
    logger.error("python-jose not installed — JWT sessions unavailable")


def configure_secret(secret: str) -> None:
    global _JWT_SECRET
    _JWT_SECRET = secret


def hash_password(plain: str) -> str:
    if not _BCRYPT_OK:
        raise RuntimeError("bcrypt not installed")
    # bcrypt requires bytes; truncate at 72 bytes (bcrypt limit)
    pw_bytes = plain.encode("utf-8")[:72]
    return _bcrypt.hashpw(pw_bytes, _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not _BCRYPT_OK:
        return False
    try:
        pw_bytes = plain.encode("utf-8")[:72]
        return _bcrypt.checkpw(pw_bytes, hashed.encode("utf-8"))
    except Exception:
        return False


def create_token(username: str, expire_hours: float | None = None) -> str:
    if not _JOSE_OK:
        raise RuntimeError("python-jose not installed")
    hours = expire_hours if expire_hours is not None else _SESSION_HOURS
    expire = datetime.now(timezone.utc) + timedelta(hours=hours)
    payload: dict[str, Any] = {"sub": username, "exp": expire}
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def decode_token(token: str) -> str | None:
    """Return username if token is valid, else None."""
    if not _JOSE_OK or not _JWT_SECRET:
        return None
    try:
        data = _jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        return str(data.get("sub", "")) or None
    except Exception:
        return None


# FastAPI dependency
# FastAPI automatically injects Request when typed as Request (no | None needed)
def require_auth(request: Request, session: str | None = Cookie(default=None)) -> str:
    username = decode_token(session or "")
    if not username:
        # For API calls, return 401 so JS can catch and redirect client-side.
        # For browser page loads, return 302 redirect to /login.
        is_api = str(request.url.path).startswith("/api/")
        if not is_api:
            accept = request.headers.get("accept", "")
            is_api = "application/json" in accept
        if is_api:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )
    return username
