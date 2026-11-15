# @file    tests/test_invite_email.py
#
# Covers the SMTP success-detection / cleanup-safety fixes and the
# partial-success invite contract. Engine + admin seeding live in
# tests/conftest.py. No real network or SMTP server is used.

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.email import (
    EmailSendResult,
    SMTP_AUTH_FAILED,
    SMTP_CONFIG_MISSING,
    SMTP_HOST_RESOLUTION_FAILED,
    SMTP_RECIPIENT_REFUSED,
)
from app.services.email import email_service
from app.services.email.email_config import (
    EmailConfig,
    SMTP_CONFIG_INVALID,
    normalize_host,
    resolve_sender,
)


client = TestClient(app)


def _admin_token() -> str:
    r = client.post("/api/auth/login", json={
        "email": "admin@example.com", "password": "Test@12345",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_admin_token()}"}


def _office365_config(from_email: str = "contact@tickandtie.com") -> EmailConfig:
    return EmailConfig(
        host="smtp.office365.com",
        port=587,
        username="contact@tickandtie.com",
        password="secret-never-logged",
        from_email=from_email,
        from_name="FinFlux",
        use_tls=True,
        frontend_url="https://app.example.com",
        invite_ttl_hours=48,
        reset_ttl_hours=4,
    )


class _FakeSMTP:
    """Minimal stand-in for smtplib.SMTP used by the send tests."""

    def __init__(self, *_args, **_kwargs):
        self.refused: dict = {}
        self.quit_raises = False
        self.send_message_calls = 0

    def ehlo(self):
        return (250, b"ok")

    def starttls(self, *_a, **_k):
        return (220, b"ready")

    def login(self, *_a, **_k):
        return (235, b"authenticated")

    def send_message(self, *_a, **_k):
        self.send_message_calls += 1
        return dict(self.refused)

    def quit(self):
        if self.quit_raises:
            raise smtplib_error()

    def close(self):
        pass


def smtplib_error() -> Exception:
    import smtplib
    return smtplib.SMTPServerDisconnected("connection unexpectedly closed")


# ── email_config: normalization + validation ──────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("smtp.office365.com", "smtp.office365.com"),
    ("  smtp.office365.com  ", "smtp.office365.com"),
    ("https://smtp.office365.com", "smtp.office365.com"),
    ("smtp://smtp.office365.com", "smtp.office365.com"),
    ("https://smtp.office365.com/send", "smtp.office365.com"),
    ("smtp.office365.com:587", "smtp.office365.com"),
])
def test_normalize_host_strips_scheme_path_and_whitespace(raw, expected):
    assert normalize_host(raw) == expected


def test_validate_rejects_missing_host():
    cfg = _office365_config()
    cfg = EmailConfig(**{**cfg.__dict__, "host": ""})
    ok, code, _ = cfg.validate()
    assert not ok and code == SMTP_CONFIG_INVALID


def test_validate_accepts_clean_office365():
    ok, code, _ = _office365_config().validate()
    assert ok and code is None


def test_resolve_sender_normalizes_office365_mismatch_to_user():
    cfg = _office365_config(from_email="veeraputhiran@tickandtie.com")
    envelope, header = resolve_sender(cfg)
    assert envelope == "contact@tickandtie.com"
    assert "contact@tickandtie.com" in header


def test_resolve_sender_keeps_matching_sender():
    cfg = _office365_config(from_email="contact@tickandtie.com")
    envelope, _ = resolve_sender(cfg)
    assert envelope == "contact@tickandtie.com"


# ── send_email: SMTP semantics ─────────────────────────────────────────────────

def test_empty_refused_dict_is_success(monkeypatch):
    fake = _FakeSMTP()
    monkeypatch.setattr(email_service, "load_email_config", _office365_config)
    monkeypatch.setattr(email_service.socket, "getaddrinfo", lambda *a, **k: [()])
    monkeypatch.setattr(email_service.smtplib, "SMTP", lambda *a, **k: fake)

    result = email_service.send_email("user@gmail.com", "Hi", "<b>hi</b>", "hi")
    assert result.success is True
    assert result.accepted is True
    assert result.error_code is None
    assert fake.send_message_calls == 1


def test_refused_recipient_dict_is_failure(monkeypatch):
    fake = _FakeSMTP()
    fake.refused = {"user@gmail.com": (550, b"User unknown")}
    monkeypatch.setattr(email_service, "load_email_config", _office365_config)
    monkeypatch.setattr(email_service.socket, "getaddrinfo", lambda *a, **k: [()])
    monkeypatch.setattr(email_service.smtplib, "SMTP", lambda *a, **k: fake)

    result = email_service.send_email("user@gmail.com", "Hi", "<b>hi</b>")
    assert result.success is False
    assert result.error_code == SMTP_RECIPIENT_REFUSED
    assert result.refused_recipients == ["user@gmail.com"]


def test_quit_failure_after_accept_is_still_success(monkeypatch):
    fake = _FakeSMTP()
    fake.quit_raises = True  # cleanup blows up after DATA accepted
    monkeypatch.setattr(email_service, "load_email_config", _office365_config)
    monkeypatch.setattr(email_service.socket, "getaddrinfo", lambda *a, **k: [()])
    monkeypatch.setattr(email_service.smtplib, "SMTP", lambda *a, **k: fake)

    result = email_service.send_email("user@gmail.com", "Hi", "<b>hi</b>")
    assert result.success is True
    assert result.accepted is True
    assert fake.send_message_calls == 1  # never retried after acceptance


def test_dns_failure_returns_resolution_error_with_bounded_retries(monkeypatch):
    calls = {"n": 0}

    def _boom(*_a, **_k):
        calls["n"] += 1
        raise socket.gaierror(11002, "getaddrinfo failed")

    opened = {"smtp": False}

    def _should_not_open(*_a, **_k):
        opened["smtp"] = True
        return _FakeSMTP()

    monkeypatch.setattr(email_service, "load_email_config", _office365_config)
    monkeypatch.setattr(email_service.socket, "getaddrinfo", _boom)
    monkeypatch.setattr(email_service.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(email_service.smtplib, "SMTP", _should_not_open)

    result = email_service.send_email("user@gmail.com", "Hi", "<b>hi</b>")
    assert result.success is False
    assert result.error_code == SMTP_HOST_RESOLUTION_FAILED
    assert calls["n"] == 3  # 1 initial + 2 retries
    assert opened["smtp"] is False  # never tried to connect after DNS failure


def test_auth_failure_maps_to_auth_failed(monkeypatch):
    import smtplib

    class _AuthFail(_FakeSMTP):
        def login(self, *_a, **_k):
            raise smtplib.SMTPAuthenticationError(535, b"bad creds")

    monkeypatch.setattr(email_service, "load_email_config", _office365_config)
    monkeypatch.setattr(email_service.socket, "getaddrinfo", lambda *a, **k: [()])
    monkeypatch.setattr(email_service.smtplib, "SMTP", lambda *a, **k: _AuthFail())

    result = email_service.send_email("user@gmail.com", "Hi", "<b>hi</b>")
    assert result.success is False
    assert result.error_code == SMTP_AUTH_FAILED


def test_result_tuple_unpacking_backwards_compatible():
    ok, code = EmailSendResult.ok()
    assert ok is True and code is None
    ok2, code2 = EmailSendResult.fail(SMTP_CONFIG_MISSING)
    assert ok2 is False and code2 == SMTP_CONFIG_MISSING


# ── invite endpoint: partial-success contract ──────────────────────────────────

def _invite_body(email: str) -> dict:
    return {
        "email": email,
        "first_name": "Grace",
        "last_name": "Hopper",
        "role": "supervisor",
        "entity_ids": [],
    }


def test_invite_full_success(monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.users.send_invite_email",
        lambda *a, **k: EmailSendResult.ok(),
    )
    r = client.post(
        "/api/admin/users/invite",
        json=_invite_body("grace.success@example.com"),
        headers=_auth_headers(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_created"] is True
    assert body["email_sent"] is True
    assert body["email_status"] == "accepted"
    assert body["safe_error_code"] is None


def test_invite_partial_success_when_email_fails(monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.users.send_invite_email",
        lambda *a, **k: EmailSendResult.fail(SMTP_HOST_RESOLUTION_FAILED),
    )
    email = "grace.partial@example.com"
    r = client.post(
        "/api/admin/users/invite",
        json=_invite_body(email),
        headers=_auth_headers(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_created"] is True
    assert body["email_sent"] is False
    assert body["email_status"] == "failed"
    assert body["safe_error_code"] == SMTP_HOST_RESOLUTION_FAILED
    # User must still persist despite the email failure.
    listing = client.get("/api/admin/users", headers=_auth_headers())
    assert any(u["email"] == email for u in listing.json())


def test_invite_response_never_leaks_password(monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.users.send_invite_email",
        lambda *a, **k: EmailSendResult.fail(SMTP_AUTH_FAILED),
    )
    r = client.post(
        "/api/admin/users/invite",
        json=_invite_body("grace.nopw@example.com"),
        headers=_auth_headers(),
    )
    assert "secret-never-logged" not in r.text
    assert "password" not in r.text.lower() or r.json()["safe_error_code"] == SMTP_AUTH_FAILED


def test_duplicate_invite_same_email_conflicts(monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.users.send_invite_email",
        lambda *a, **k: EmailSendResult.ok(),
    )
    email = "grace.dup@example.com"
    first = client.post(
        "/api/admin/users/invite", json=_invite_body(email), headers=_auth_headers(),
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/admin/users/invite", json=_invite_body(email), headers=_auth_headers(),
    )
    assert second.status_code == 409


def test_resend_invite_success(monkeypatch):
    monkeypatch.setattr(
        "app.routers.admin.users.send_invite_email",
        lambda *a, **k: EmailSendResult.ok(),
    )
    email = "grace.resend@example.com"
    created = client.post(
        "/api/admin/users/invite", json=_invite_body(email), headers=_auth_headers(),
    )
    uid = created.json()["id"]
    r = client.post(f"/api/admin/users/{uid}/resend-invite", headers=_auth_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["email_sent"] is True
    assert body["email_status"] == "accepted"


def test_resend_invite_partial_failure(monkeypatch):
    email = "grace.resendfail@example.com"
    monkeypatch.setattr(
        "app.routers.admin.users.send_invite_email",
        lambda *a, **k: EmailSendResult.ok(),
    )
    created = client.post(
        "/api/admin/users/invite", json=_invite_body(email), headers=_auth_headers(),
    )
    uid = created.json()["id"]
    monkeypatch.setattr(
        "app.routers.admin.users.send_invite_email",
        lambda *a, **k: EmailSendResult.fail(SMTP_HOST_RESOLUTION_FAILED),
    )
    r = client.post(f"/api/admin/users/{uid}/resend-invite", headers=_auth_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["email_sent"] is False
    assert body["safe_error_code"] == SMTP_HOST_RESOLUTION_FAILED
