# @file    crypto.py
#
# Fernet wrapper for ERP credential encryption.

from __future__ import annotations
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_log = logging.getLogger(__name__)


def _to_valid_fernet_key(candidate: str) -> bytes:
    cleaned = candidate.strip().encode("utf-8")
    try:
        Fernet(cleaned)
        return cleaned
    except Exception:
        digest = hashlib.sha256(cleaned).digest()
        return base64.urlsafe_b64encode(digest)


def _resolve_key() -> bytes:
    raw = settings.CREDENTIAL_ENCRYPTION_KEY or settings.FERNET_KEY
    if raw:
        return _to_valid_fernet_key(raw)

    if settings.is_dev and settings.ALLOW_EPHEMERAL_DEV_KEY:
        key = Fernet.generate_key()
        _log.warning(
            "Generated an EPHEMERAL Fernet key because ALLOW_EPHEMERAL_DEV_KEY=true. "
            "Stored credentials will become unreadable after the next restart. "
            "Set CREDENTIAL_ENCRYPTION_KEY in .env for stable storage."
        )
        return key

    _log.warning(
        "Neither CREDENTIAL_ENCRYPTION_KEY nor FERNET_KEY is set; deriving a fallback key. "
        "Set CREDENTIAL_ENCRYPTION_KEY in Azure App Settings for production."
    )
    app_env = getattr(settings, "APP_ENV", "default")
    return _to_valid_fernet_key(f"{app_env}:finflux-credential-fallback")


_fernet = Fernet(_resolve_key())


def encrypt(plain: str) -> str:
    """Encrypt a plaintext string with the configured Fernet key."""
    return _fernet.encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a Fernet token. Never log the result."""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Could not decrypt secret (wrong key or corrupt data)") from e


def mask_secret(plain: str) -> str:
    """Returns last-4 only with stars: '********1234'. Empty string for empty input."""
    if not plain:
        return ""
    if len(plain) <= 4:
        return "*" * len(plain)
    return "*" * 8 + plain[-4:]


encrypt_secret = encrypt
decrypt_secret = decrypt
