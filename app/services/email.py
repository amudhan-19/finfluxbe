# @file    email.py
#
# Email service. In dev (no SMTP_HOST) it console-prints the message so
# we can test the invite/reset flow without a real mail server.

from __future__ import annotations
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

_log = logging.getLogger(__name__)


def _print_to_console(to: str, subject: str, body: str) -> None:
    line = "─" * 70
    print(f"\n{line}\n[email] To: {to}\n[email] Subject: {subject}\n{line}\n{body}\n{line}\n")


def send_email(to: str, subject: str, body: str) -> bool:
    if not settings.SMTP_HOST:
        _print_to_console(to, subject, body)
        return True

    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as s:
            s.starttls()
            if settings.SMTP_USER:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:
        _log.error("Email send failed: %s", e)
        return False


def send_invite_email(to: str, name: str, token: str) -> None:
    link = f"{settings.APP_URL}/accept-invite?token={token}"
    body = (
        f"Hi {name},\n\n"
        f"You've been invited to join FinFlux.\n\n"
        f"Accept your invite and set a password:\n  {link}\n\n"
        f"This link expires in {settings.INVITE_TTL_HOURS} hours.\n\n"
        f"— Tick & Tie / FinFlux"
    )
    send_email(to, "Welcome to FinFlux — set your password", body)


def send_reset_email(to: str, name: str, token: str) -> None:
    link = f"{settings.APP_URL}/reset-password?token={token}"
    body = (
        f"Hi {name},\n\n"
        f"A password reset was requested for your FinFlux account.\n\n"
        f"Reset it here:\n  {link}\n\n"
        f"This link expires in {settings.RESET_TTL_HOURS} hours. If you didn't request this, ignore this email.\n\n"
        f"— Tick & Tie / FinFlux"
    )
    send_email(to, "FinFlux — password reset", body)
