# @file    crypto.py
#
# Fernet wrapper for ERP credential encryption.
#
# Key resolution order:
#   1. CREDENTIAL_ENCRYPTION_KEY  (preferred)
#   2. FERNET_KEY                 (legacy fallback — keeps old data decryptable)
#   3. Ephemeral dev key          ONLY when APP_ENV=development AND
#                                 ALLOW_EPHEMERAL_DEV_KEY=true. Unsafe; do
#                                 NOT use for any data that must survive a
#                                 restart.
#
# IMPORTANT — the key MUST be stable across restarts. Rotating the key without
# re-encrypting stored ciphertext renders existing credentials unreadable
# (decrypt fails → callers map this to INVALID_CONFIG).

from __future__ import annotations
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_log = logging.getLogger(__name__)


def _resolve_key() -> bytes:
    raw = settings.CREDENTIAL_ENCRYPTION_KEY or settings.FERNET_KEY
    if raw:
        return raw.encode()

    if settings.is_dev and settings.ALLOW_EPHEMERAL_DEV_KEY:
        key = Fernet.generate_key()
        _log.warning(
            "Generated an EPHEMERAL Fernet key because ALLOW_EPHEMERAL_DEV_KEY=true. "
            "Stored credentials will become unreadable after the next restart. "
            "Set CREDENTIAL_ENCRYPTION_KEY in .env for stable storage."
        )
        return key

    raise RuntimeError(
        "Credential encryption key is not configured. "
        "Set CREDENTIAL_ENCRYPTION_KEY (preferred) or FERNET_KEY in your .env. "
        "Generate one with: "
        'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    )


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


# Aliases used by the Business Central credential flow so callers express intent
# (encrypt_secret / decrypt_secret) without binding to the legacy name.
encrypt_secret = encrypt
decrypt_secret = decrypt
