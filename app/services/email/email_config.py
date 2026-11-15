# @file    services/email/email_config.py
#
# Reads SMTP configuration from application settings. All values come
# exclusively from environment variables — never from source code.
#
# This module also normalizes and validates the SMTP settings so a
# malformed SMTP_HOST (scheme prefix, stray whitespace, trailing path)
# cannot turn into a socket.gaierror at connect time. Passwords are never
# logged or echoed back through any return value.

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import settings

# Stable configuration error codes — surfaced to callers, never secrets.
SMTP_CONFIG_INVALID = "SMTP_CONFIG_INVALID"
SMTP_FROM_MISMATCH = "SMTP_FROM_MISMATCH"

# Hosts that reject sending as an address other than the authenticated
# mailbox unless an explicit Send-As grant exists in the tenant.
_OFFICE365_HOST_HINTS = ("office365", "outlook", "microsoft")

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def normalize_host(raw: str) -> str:
    """Return a bare SMTP hostname.

    Strips surrounding whitespace, any ``scheme://`` prefix
    (``https://``, ``smtp://`` …), and any trailing path/port the user may
    have pasted. ``"  https://smtp.office365.com/foo "`` becomes
    ``"smtp.office365.com"``.
    """
    if not raw:
        return ""
    host = raw.strip()
    host = _SCHEME_RE.sub("", host)
    # Drop any trailing path component ("smtp.office365.com/send" → host).
    host = host.split("/", 1)[0]
    # Drop an inline ":port" if one was pasted into the host value.
    host = host.split(":", 1)[0]
    return host.strip()


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    username: str
    password: str        # never logged
    from_email: str
    from_name: str
    use_tls: bool
    frontend_url: str
    invite_ttl_hours: int
    reset_ttl_hours: int

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.from_email)

    @property
    def from_address(self) -> str:
        """RFC-5321 formatted sender: 'Name <email>' or bare email."""
        if self.from_name:
            return f"{self.from_name} <{self.from_email}>"
        return self.from_email

    @property
    def is_office365(self) -> bool:
        host = self.host.lower()
        return any(hint in host for hint in _OFFICE365_HOST_HINTS)

    @property
    def from_matches_user(self) -> bool:
        """True when no authenticated user is set, or it equals SMTP_FROM."""
        user = self.username.strip().lower()
        return not user or self.from_email.strip().lower() == user

    def validate(self) -> tuple[bool, str | None, str | None]:
        """Validate the loaded SMTP settings.

        Returns ``(ok, error_code, safe_message)``. Never includes
        passwords or tokens in the message.
        """
        if not self.is_configured:
            return False, SMTP_CONFIG_INVALID, (
                "SMTP is not configured. Set SMTP_HOST and SMTP_FROM."
            )
        if _SCHEME_RE.match(self.host) or "/" in self.host or " " in self.host:
            return False, SMTP_CONFIG_INVALID, (
                "SMTP_HOST must be a bare hostname such as smtp.office365.com "
                "(no scheme, path, or whitespace)."
            )
        if not isinstance(self.port, int) or self.port <= 0:
            return False, SMTP_CONFIG_INVALID, "SMTP_PORT must be a positive integer."
        # STARTTLS (587) or implicit SSL (465) are both acceptable. Any other
        # combination with TLS disabled is rejected to avoid clear-text auth.
        if self.port != 465 and not self.use_tls:
            return False, SMTP_CONFIG_INVALID, (
                "STARTTLS must be enabled for SMTP submission on this port."
            )
        return True, None, None


def resolve_sender(cfg: EmailConfig) -> tuple[str, str]:
    """Return ``(envelope_from, header_from)`` safe for the configured host.

    Microsoft 365 rejects mail when the authenticated mailbox
    (``SMTP_USER``) differs from the envelope / From address unless
    *Send As* is granted in the tenant (error 5.2.252 SendAsDenied).

    When we detect that mismatch on an O365 host we fall back to
    ``SMTP_USER`` for both envelope and header while keeping
    ``SMTP_FROM_NAME`` for the display name.
    """
    envelope = cfg.from_email
    header = cfg.from_address
    if cfg.from_matches_user or not cfg.is_office365:
        return envelope, header
    # Mismatch on O365 — use the authenticated mailbox.
    envelope = cfg.username
    if cfg.from_name:
        header = f"{cfg.from_name} <{cfg.username}>"
    else:
        header = cfg.username
    return envelope, header


def load_email_config() -> EmailConfig:
    """Load SMTP config from settings. No defaults are set here —
    everything comes from the .env file. Host and credential-adjacent
    string values are stripped/normalized defensively."""
    return EmailConfig(
        host=normalize_host(settings.SMTP_HOST),
        port=settings.SMTP_PORT,
        username=settings.SMTP_USER.strip(),
        password=settings.SMTP_PASSWORD,
        from_email=settings.SMTP_FROM.strip(),
        from_name=settings.SMTP_FROM_NAME.strip(),
        use_tls=settings.SMTP_USE_TLS,
        frontend_url=settings.APP_URL,
        invite_ttl_hours=settings.INVITE_TTL_HOURS,
        reset_ttl_hours=settings.RESET_TTL_HOURS,
    )
