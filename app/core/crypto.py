import base64
import hashlib


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
    fallback_seed = f"{settings.APP_ENV}:finflux-credential-fallback"
    return _to_valid_fernet_key(fallback_seed)
