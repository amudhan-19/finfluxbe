# @file    services/erp/quickbooks_oauth.py
#
# Intuit OAuth 2.0 for QuickBooks Online — authorization code grant.
# App registration values are read from per-entity DB rows (Integrations tab),
# with optional .env fallbacks for local development only.
# Tokens and secrets are encrypted before persistence; never logged.

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret, mask_secret
from app.models.models import QuickBooksCredential

from .safe_errors import (
    INVALID_CONFIG,
    QUICKBOOKS_NOT_CONFIGURED,
    QUICKBOOKS_OAUTH_FAILED,
    QUICKBOOKS_REFRESH_FAILED,
    SafeError,
    safe_error,
)

_log = logging.getLogger(__name__)

DEFAULT_QB_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
DEFAULT_QB_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
DEFAULT_QB_SCOPE = "com.intuit.quickbooks.accounting"
DEFAULT_QB_REDIRECT_URI = (
    "http://localhost:5173/api/admin/integrations/quickbooks/callback"
)
DEFAULT_QB_API_BASE_SANDBOX = "https://sandbox-quickbooks.api.intuit.com"
DEFAULT_QB_API_BASE_PRODUCTION = "https://quickbooks.api.intuit.com"

INTUIT_USERINFO_URL = "https://accounts.platform.intuit.com/op/v1/users/me"


@dataclass(frozen=True)
class QbOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str
    auth_url: str
    token_url: str
    api_base_sandbox: str
    api_base_production: str
    environment: str


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: datetime


def encrypt_credential_field(plain: str) -> str:
    return encrypt_secret(plain)


def mask_for_display(plain: str) -> str:
    return mask_secret(plain)


def _decrypt_optional(cipher: str | None) -> str | None:
    if not cipher:
        return None
    try:
        return decrypt_secret(cipher)
    except ValueError:
        return None


def resolve_qb_oauth_config(rec: QuickBooksCredential | None) -> QbOAuthConfig:
    """Merge stored credential fields with optional .env dev defaults."""
    env_name = (
        (rec.environment if rec else None)
        or settings.QUICKBOOKS_ENVIRONMENT
        or "sandbox"
    ).strip().lower()
    client_id = (
        _decrypt_optional(rec.client_id_encrypted if rec else None)
        or settings.QUICKBOOKS_CLIENT_ID
        or ""
    ).strip()
    client_secret = (
        _decrypt_optional(rec.client_secret_encrypted if rec else None)
        or settings.QUICKBOOKS_CLIENT_SECRET
        or ""
    ).strip()
    redirect_uri = (
        (rec.redirect_uri if rec else None)
        or settings.QUICKBOOKS_REDIRECT_URI
        or DEFAULT_QB_REDIRECT_URI
    ).strip()
    scopes = (
        (rec.scopes if rec else None)
        or settings.QUICKBOOKS_SCOPES
        or DEFAULT_QB_SCOPE
    ).strip()
    auth_url = (
        (rec.auth_url if rec else None)
        or settings.QUICKBOOKS_AUTH_URL
        or DEFAULT_QB_AUTH_URL
    ).strip()
    token_url = (
        (rec.token_url if rec else None)
        or settings.QUICKBOOKS_TOKEN_URL
        or DEFAULT_QB_TOKEN_URL
    ).strip()
    api_base_sandbox = (
        (rec.api_base_sandbox if rec else None)
        or settings.QUICKBOOKS_API_BASE_SANDBOX
        or DEFAULT_QB_API_BASE_SANDBOX
    ).strip().rstrip("/")
    api_base_production = (
        (rec.api_base_production if rec else None)
        or settings.QUICKBOOKS_API_BASE_PRODUCTION
        or DEFAULT_QB_API_BASE_PRODUCTION
    ).strip().rstrip("/")
    return QbOAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=scopes,
        auth_url=auth_url,
        token_url=token_url,
        api_base_sandbox=api_base_sandbox,
        api_base_production=api_base_production,
        environment=env_name,
    )


def app_credentials_saved(rec: QuickBooksCredential | None) -> bool:
    if not rec:
        return False
    if rec.client_id_encrypted and rec.client_secret_encrypted:
        return True
    cfg = resolve_qb_oauth_config(rec)
    return bool(cfg.client_id and cfg.client_secret)


def oauth_ready(rec: QuickBooksCredential | None) -> bool:
    cfg = resolve_qb_oauth_config(rec)
    if not cfg.client_id or not cfg.client_secret:
        return False
    if not cfg.redirect_uri or not cfg.auth_url or not cfg.token_url:
        return False
    if cfg.environment == "production":
        return bool(cfg.api_base_production)
    return bool(cfg.api_base_sandbox)


def qb_api_base(rec: QuickBooksCredential | None) -> str:
    cfg = resolve_qb_oauth_config(rec)
    if cfg.environment == "production":
        return cfg.api_base_production or DEFAULT_QB_API_BASE_PRODUCTION
    return cfg.api_base_sandbox or DEFAULT_QB_API_BASE_SANDBOX


def generate_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def build_authorization_url(*, rec: QuickBooksCredential, state: str) -> str:
    cfg = resolve_qb_oauth_config(rec)
    if not oauth_ready(rec):
        raise ValueError("QuickBooks OAuth is not configured for this entity")
    params = {
        "client_id": cfg.client_id,
        "response_type": "code",
        "scope": cfg.scopes,
        "redirect_uri": cfg.redirect_uri,
        "state": state,
    }
    return f"{cfg.auth_url}?{urlencode(params)}"


def _token_request(cfg: QbOAuthConfig, data: dict[str, str]) -> dict:
    timeout = httpx.Timeout(settings.QUICKBOOKS_HTTP_TIMEOUT_SECONDS)
    auth = (cfg.client_id, cfg.client_secret)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(cfg.token_url, data=data, auth=auth, headers=headers)
        resp.raise_for_status()
        return resp.json()


def exchange_code_for_tokens(rec: QuickBooksCredential, code: str) -> TokenBundle:
    cfg = resolve_qb_oauth_config(rec)
    body = _token_request(cfg, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.redirect_uri,
    })
    return _parse_token_response(body)


def refresh_access_token(rec: QuickBooksCredential, refresh_token_plain: str) -> TokenBundle:
    cfg = resolve_qb_oauth_config(rec)
    body = _token_request(cfg, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token_plain,
    })
    return _parse_token_response(body)


def _parse_token_response(body: dict) -> TokenBundle:
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    if not access or not isinstance(access, str):
        raise httpx.HTTPError("Token response missing access_token")
    if not refresh or not isinstance(refresh, str):
        raise httpx.HTTPError("Token response missing refresh_token")
    expires_in = int(body.get("expires_in") or 3600)
    expires_at = datetime.utcnow() + timedelta(seconds=max(60, expires_in - 60))
    return TokenBundle(access_token=access, refresh_token=refresh, expires_at=expires_at)


def encrypt_token(plain: str) -> str:
    return encrypt_secret(plain)


def decrypt_token(cipher: str) -> str:
    return decrypt_secret(cipher)


def store_tokens(rec: QuickBooksCredential, bundle: TokenBundle) -> None:
    rec.access_token_encrypted = encrypt_token(bundle.access_token)
    rec.refresh_token_encrypted = encrypt_token(bundle.refresh_token)
    rec.token_expiry = bundle.expires_at


def clear_tokens(rec: QuickBooksCredential) -> None:
    rec.access_token_encrypted = None
    rec.refresh_token_encrypted = None
    rec.token_expiry = None


def get_valid_access_token(rec: QuickBooksCredential) -> tuple[Optional[str], Optional[SafeError]]:
    """Return a usable access token, refreshing when near expiry."""
    if not rec.access_token_encrypted or not rec.refresh_token_encrypted:
        return None, safe_error(QUICKBOOKS_NOT_CONFIGURED)
    try:
        access = decrypt_token(rec.access_token_encrypted)
        refresh = decrypt_token(rec.refresh_token_encrypted)
    except ValueError:
        _log.warning("quickbooks token decrypt failed entity_id=%s", rec.entity_id)
        return None, safe_error(INVALID_CONFIG, "Stored QuickBooks tokens cannot be decrypted.")

    now = datetime.utcnow()
    if rec.token_expiry and rec.token_expiry > now + timedelta(seconds=30):
        return access, None

    try:
        bundle = refresh_access_token(rec, refresh)
    except httpx.HTTPError:
        _log.warning("quickbooks token refresh failed entity_id=%s", rec.entity_id)
        return None, safe_error(QUICKBOOKS_REFRESH_FAILED)

    store_tokens(rec, bundle)
    return bundle.access_token, None


def fetch_user_info(access_token: str) -> dict:
    timeout = httpx.Timeout(settings.QUICKBOOKS_HTTP_TIMEOUT_SECONDS)
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(INTUIT_USERINFO_URL, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


def oauth_error_from_exception(exc: BaseException) -> SafeError:
    if isinstance(exc, httpx.TimeoutException):
        return safe_error(QUICKBOOKS_OAUTH_FAILED, "QuickBooks authorization timed out.")
    if isinstance(exc, httpx.HTTPError):
        return safe_error(QUICKBOOKS_OAUTH_FAILED)
    if isinstance(exc, ValueError):
        return safe_error(QUICKBOOKS_OAUTH_FAILED, str(exc) if str(exc) else None)
    return safe_error(QUICKBOOKS_OAUTH_FAILED)
