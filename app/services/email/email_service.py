# @file    services/email/email_service.py
#
# Core SMTP email sender. Supports Outlook/Office 365 STARTTLS (port 587)
# and implicit SSL (port 465). Never logs credentials, tokens, or PII.
#
# Returns a structured EmailSendResult so callers can distinguish
# "server accepted the message" from "transport failed" without ever
# treating SMTP's empty refused-recipient dict ({}) as a failure.

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.services.email.email_config import (
    EmailConfig,
    load_email_config,
    resolve_sender,
)
from app.services.email.email_config import (
    SMTP_CONFIG_INVALID,
    SMTP_FROM_MISMATCH,
)
from app.services.email.email_templates import (
    build_invite_email,
    build_password_changed_email,
    build_reset_email,
)

_log = logging.getLogger(__name__)

# Stable error codes returned to callers — never expose raw SMTP stack traces.
SMTP_CONFIG_MISSING = "SMTP_CONFIG_MISSING"
SMTP_HOST_RESOLUTION_FAILED = "SMTP_HOST_RESOLUTION_FAILED"
SMTP_AUTH_FAILED = "SMTP_AUTH_FAILED"
SMTP_RECIPIENT_REFUSED = "SMTP_RECIPIENT_REFUSED"
SMTP_CONNECTION_FAILED = "SMTP_CONNECTION_FAILED"
SMTP_SEND_FAILED = "SMTP_SEND_FAILED"
EMAIL_TEMPLATE_ERROR = "EMAIL_TEMPLATE_ERROR"

# Connection timeout (seconds) and DNS-preflight retry policy. Retries only
# ever happen BEFORE the SMTP server accepts the message, so they can never
# cause a duplicate delivery.
_CONNECT_TIMEOUT = 15
_DNS_RETRY_BACKOFFS = (1.0, 2.0)  # at most 2 retries: ~1s then ~2s

# User-facing safe messages keyed by error code. No secrets, no stack traces.
_ERROR_MESSAGES: dict[str, str] = {
    SMTP_CONFIG_MISSING: "Email service is not configured on the server.",
    SMTP_CONFIG_INVALID: "Email service configuration is invalid.",
    SMTP_FROM_MISMATCH: (
        "SMTP sender must match the authenticated Microsoft 365 account "
        "unless Send-As permission is configured."
    ),
    SMTP_HOST_RESOLUTION_FAILED: (
        "The mail server hostname could not be resolved. "
        "Please verify SMTP_HOST and network/DNS settings."
    ),
    SMTP_AUTH_FAILED: "Email authentication failed. Check SMTP_USER and SMTP_PASSWORD.",
    SMTP_RECIPIENT_REFUSED: "The mail server refused one or more recipients.",
    SMTP_CONNECTION_FAILED: "Could not connect to the mail server.",
    SMTP_SEND_FAILED: "The invitation email could not be delivered.",
    EMAIL_TEMPLATE_ERROR: "Invite email template could not be built.",
}


def safe_message_for(code: str | None) -> str:
    """Return a user-safe message for an error code (never secrets)."""
    if code is None:
        return "The email could not be sent."
    return _ERROR_MESSAGES.get(code, "The email could not be sent.")


@dataclass
class EmailSendResult:
    """Structured outcome of an email send attempt.

    ``accepted`` means the SMTP server acknowledged the message (DATA
    accepted). ``success`` mirrors ``accepted`` for callers that only care
    about the boolean. ``error_code`` / ``message`` are safe to surface to
    the API and frontend; they never contain credentials or tokens.
    """

    success: bool
    accepted: bool = False
    error_code: str | None = None
    message: str | None = None
    refused_recipients: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls) -> "EmailSendResult":
        return cls(success=True, accepted=True)

    @classmethod
    def fail(cls, code: str, refused: list[str] | None = None) -> "EmailSendResult":
        return cls(
            success=False,
            accepted=False,
            error_code=code,
            message=safe_message_for(code),
            refused_recipients=refused or [],
        )

    # Backwards-compatible tuple unpacking: ``ok, code = result`` still works.
    def __iter__(self):
        yield self.success
        yield self.error_code


# ── Low-level send ────────────────────────────────────────────────────────────

def _build_message(
    to: str,
    subject: str,
    html_body: str,
    text_body: str | None,
    header_from: str,
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = header_from
    msg["To"] = to
    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def _preflight_dns(host: str, port: int) -> bool:
    """Resolve ``host`` with a small bounded retry before connecting.

    Returns True if resolution succeeds within the retry budget. Retries
    happen only here, strictly before any message is sent, so they can
    never duplicate a delivery. Credentials are never logged.
    """
    attempts = 1 + len(_DNS_RETRY_BACKOFFS)
    for i in range(attempts):
        try:
            socket.getaddrinfo(host, port)
            return True
        except socket.gaierror as exc:
            is_last = i == attempts - 1
            _log.warning(
                "SMTP DNS resolution failed (attempt %d/%d) host=%s port=%s type=%s%s",
                i + 1, attempts, host, port, type(exc).__name__,
                "" if is_last else " — retrying",
            )
            if is_last:
                return False
            time.sleep(_DNS_RETRY_BACKOFFS[i])
    return False


def _open_connection(cfg: EmailConfig) -> smtplib.SMTP:
    """Open and authenticate an SMTP connection (no message sent yet)."""
    if cfg.port == 465:
        ctx = ssl.create_default_context()
        conn: smtplib.SMTP = smtplib.SMTP_SSL(
            cfg.host, cfg.port, timeout=_CONNECT_TIMEOUT, context=ctx,
        )
    else:
        conn = smtplib.SMTP(cfg.host, cfg.port, timeout=_CONNECT_TIMEOUT)
        conn.ehlo()
        conn.starttls(context=ssl.create_default_context())
        conn.ehlo()
    if cfg.username:
        conn.login(cfg.username, cfg.password)
    return conn


def _safe_close(conn: smtplib.SMTP | None, message_accepted: bool) -> None:
    """Close the connection without ever overriding a successful send."""
    if conn is None:
        return
    try:
        conn.quit()
    except Exception:  # noqa: BLE001 — cleanup must never raise
        if message_accepted:
            _log.warning(
                "SMTP connection cleanup failed after the message was already "
                "accepted by the server; treating the send as successful.",
            )
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def send_email(
    to: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
) -> EmailSendResult:
    """Send a single email. Returns a structured EmailSendResult.

    SMTP semantics: ``send_message`` returns a dict of *refused* recipients.
    An empty dict ({}) means every recipient was accepted — success.
    """
    cfg = load_email_config()

    if not cfg.is_configured:
        _log.warning("SMTP not configured — skipping email to %s", to)
        return EmailSendResult.fail(SMTP_CONFIG_MISSING)

    ok, code, _ = cfg.validate()
    if not ok:
        _log.error("SMTP configuration invalid: %s", code)
        return EmailSendResult.fail(code or SMTP_CONFIG_INVALID)

    envelope_from, header_from = resolve_sender(cfg)
    if not cfg.from_matches_user and cfg.is_office365:
        # We auto-normalize to the authenticated mailbox (see resolve_sender)
        # to avoid SendAsDenied; surface a non-secret heads-up in the log.
        _log.warning(
            "SMTP_FROM differs from SMTP_USER on Office 365; sending as the "
            "authenticated mailbox to avoid SendAsDenied. Set SMTP_FROM=SMTP_USER "
            "or grant Send-As in Microsoft 365 admin.",
        )

    msg = _build_message(to, subject, html_body, text_body, header_from)

    # Preflight DNS with bounded retry (before any message is sent).
    if not _preflight_dns(cfg.host, cfg.port):
        return EmailSendResult.fail(SMTP_HOST_RESOLUTION_FAILED)

    conn: smtplib.SMTP | None = None
    message_accepted = False
    try:
        conn = _open_connection(cfg)
        refused = conn.send_message(msg, from_addr=envelope_from, to_addrs=[to])
        if refused:
            _log.warning("SMTP refused recipient(s) for a message to %s", to)
            return EmailSendResult.fail(
                SMTP_RECIPIENT_REFUSED, refused=list(refused.keys()),
            )
        message_accepted = True
        _log.info("Email accepted by SMTP server (subject=%r)", subject)
        return EmailSendResult.ok()

    except smtplib.SMTPAuthenticationError:
        _log.error("SMTP authentication failed for host=%s", cfg.host)
        return EmailSendResult.fail(SMTP_AUTH_FAILED)

    except smtplib.SMTPRecipientsRefused as exc:
        _log.warning("SMTP refused all recipients for a message to %s", to)
        return EmailSendResult.fail(
            SMTP_RECIPIENT_REFUSED, refused=list(exc.recipients.keys()),
        )

    except socket.gaierror as exc:
        _log.error(
            "SMTP host resolution failed host=%s port=%s type=%s",
            cfg.host, cfg.port, type(exc).__name__,
        )
        return EmailSendResult.fail(SMTP_HOST_RESOLUTION_FAILED)

    except (smtplib.SMTPConnectError, ConnectionError, TimeoutError, OSError) as exc:
        # If the server already accepted the message, a later transport hiccup
        # must not be reported as a failure.
        if message_accepted:
            _log.warning(
                "SMTP transport error after the message was accepted (type=%s); "
                "treating the send as successful.", type(exc).__name__,
            )
            return EmailSendResult.ok()
        _log.error(
            "SMTP connection failed host=%s port=%s type=%s",
            cfg.host, cfg.port, type(exc).__name__,
        )
        return EmailSendResult.fail(SMTP_CONNECTION_FAILED)

    except Exception as exc:  # noqa: BLE001
        if message_accepted:
            _log.warning(
                "SMTP error after the message was accepted (type=%s); "
                "treating the send as successful.", type(exc).__name__,
            )
            return EmailSendResult.ok()
        _log.exception("SMTP send failed")
        return EmailSendResult.fail(SMTP_SEND_FAILED)

    finally:
        _safe_close(conn, message_accepted)


# ── High-level helpers ────────────────────────────────────────────────────────

def send_invite_email(to: str, first_name: str, token: str) -> EmailSendResult:
    """Send account activation / invite email to a newly created user."""
    cfg = load_email_config()
    if not cfg.is_configured:
        _log.warning("SMTP not configured — invite email not sent to %s", to)
        return EmailSendResult.fail(SMTP_CONFIG_MISSING)

    invite_link = f"{cfg.frontend_url}/accept-invite?token={token}"
    try:
        subject, html_body, text_body = build_invite_email(
            first_name=first_name,
            invite_link=invite_link,
            expire_hours=cfg.invite_ttl_hours,
        )
    except Exception:  # noqa: BLE001
        _log.exception("Failed to build invite email template")
        return EmailSendResult.fail(EMAIL_TEMPLATE_ERROR)

    return send_email(to, subject, html_body, text_body)


def send_reset_email(to: str, first_name: str, token: str) -> EmailSendResult:
    """Send password reset email triggered by /forgot-password."""
    cfg = load_email_config()
    if not cfg.is_configured:
        _log.warning("SMTP not configured — reset email not sent to %s", to)
        return EmailSendResult.fail(SMTP_CONFIG_MISSING)

    reset_link = f"{cfg.frontend_url}/reset-password?token={token}"
    try:
        subject, html_body, text_body = build_reset_email(
            first_name=first_name,
            reset_link=reset_link,
            expire_hours=cfg.reset_ttl_hours,
        )
    except Exception:  # noqa: BLE001
        _log.exception("Failed to build reset email template")
        return EmailSendResult.fail(EMAIL_TEMPLATE_ERROR)

    return send_email(to, subject, html_body, text_body)


def send_password_changed_email(to: str, first_name: str) -> EmailSendResult:
    """Send confirmation after a successful password reset."""
    try:
        subject, html_body, text_body = build_password_changed_email(first_name)
    except Exception:  # noqa: BLE001
        _log.exception("Failed to build password-changed email template")
        return EmailSendResult.fail(EMAIL_TEMPLATE_ERROR)

    return send_email(to, subject, html_body, text_body)
