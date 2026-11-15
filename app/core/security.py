# @file    security.py
#
# Password hashing, JWT issue/verify, invite/reset token signing.

from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings


# ── Password hashing ────────────────────────────────────────────
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


# ── JWT ─────────────────────────────────────────────────────────
TokenType = Literal["access", "refresh", "invite", "reset"]


def _encode(payload: dict[str, Any], exp: timedelta) -> str:
    now = datetime.now(timezone.utc)
    to_encode = {**payload, "iat": int(now.timestamp()), "exp": int((now + exp).timestamp())}
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _decode(token: str, expected_type: TokenType) -> dict[str, Any]:
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("typ") != expected_type:
        raise JWTError(f"Wrong token type: expected {expected_type}, got {payload.get('typ')}")
    return payload


def create_access_token(user_id: int, role: str) -> str:
    return _encode(
        {"sub": str(user_id), "role": role, "typ": "access"},
        timedelta(minutes=settings.ACCESS_TTL_MIN),
    )


def create_refresh_token(user_id: int, jti: str) -> str:
    return _encode(
        {"sub": str(user_id), "jti": jti, "typ": "refresh"},
        timedelta(days=settings.REFRESH_TTL_DAYS),
    )


def create_invite_token(user_id: int, email: str) -> str:
    return _encode(
        {"sub": str(user_id), "email": email, "typ": "invite"},
        timedelta(hours=settings.INVITE_TTL_HOURS),
    )


def create_reset_token(user_id: int, email: str) -> str:
    return _encode(
        {"sub": str(user_id), "email": email, "typ": "reset"},
        timedelta(hours=settings.RESET_TTL_HOURS),
    )


def decode_token(token: str, expected_type: TokenType) -> Optional[dict[str, Any]]:
    try:
        return _decode(token, expected_type)
    except JWTError:
        return None
