# @file    services/erp/business_central.py
#
# Microsoft Dynamics 365 Business Central connector.
#
# Auth model:
#   * OAuth 2.0 client_credentials grant against
#     https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token
#   * Resource scope:  https://api.businesscentral.dynamics.com/.default
#
# Data API:
#   * Base:    https://api.businesscentral.dynamics.com/v2.0/{environmentName}/api/v2.0
#   * Used:    /companies, /companies({id})/accounts,
#             /companies({id})/finfluxGlEntries (Tick & Tie Flux extension),
#             /companies({id})/generalLedgerEntries (fallback)
#
# Hard rules enforced here:
#   - Plaintext credentials NEVER leave this module.
#   - Tokens / Authorization headers are NEVER logged.
#   - Outgoing requests carry a fixed timeout; pagination follows @odata.nextLink.
#   - Every public function returns plain dicts/lists — caller chooses what to
#     persist or surface.

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, List, Optional

import httpx

from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret, mask_secret
from app.models.models import BusinessCentralCredential

from .safe_errors import (
    SafeError,
    BC_CUSTOMER_LEDGER_ENDPOINT_NOT_FOUND,
    BC_VENDOR_LEDGER_ENDPOINT_NOT_FOUND,
    ENDPOINT_NOT_FOUND,
    from_exception,
    safe_error,
    INVALID_CONFIG,
)

_log = logging.getLogger(__name__)

# Microsoft public endpoints — not secrets.
MICROSOFT_TOKEN_URL_TPL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
DEFAULT_BC_BASE_URL     = "https://api.businesscentral.dynamics.com"
DEFAULT_BC_SCOPE        = "https://api.businesscentral.dynamics.com/.default"


# ──────────────────────────────────────────────────────────────────────
# Decrypted snapshot used internally — NEVER serialize this dataclass.
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _DecryptedCredentials:
    tenant_id: str
    client_id: str
    client_secret: str
    environment_name: str
    scope: str
    base_url: str
    company_id: Optional[str]
    company_name: Optional[str]


def _decrypt_record(rec: BusinessCentralCredential) -> _DecryptedCredentials:
    try:
        return _DecryptedCredentials(
            tenant_id=decrypt_secret(rec.tenant_id_encrypted),
            client_id=decrypt_secret(rec.client_id_encrypted),
            client_secret=decrypt_secret(rec.client_secret_encrypted),
            environment_name=rec.environment_name,
            scope=rec.scope or DEFAULT_BC_SCOPE,
            base_url=(rec.base_url or DEFAULT_BC_BASE_URL).rstrip("/"),
            company_id=rec.company_id,
            company_name=rec.company_name,
        )
    except ValueError as e:
        # Caller maps this to INVALID_CONFIG; do not re-raise with secret data.
        raise ValueError("Stored Business Central credentials could not be decrypted") from e


def _decrypt_record_safe(rec: BusinessCentralCredential) -> tuple[Optional[_DecryptedCredentials], Optional[SafeError]]:
    """Decrypt stored row for connector calls. Never raises — wrong/rotated
    Fernet key (common after `uvicorn --reload` when no key is in `.env`)
    becomes a safe INVALID_CONFIG result instead of a 500."""
    try:
        return (_decrypt_record(rec), None)
    except ValueError:
        _log.warning("bc_credentials decrypt failed for entity_id=%s (key mismatch or corrupt ciphertext)",
                     rec.entity_id)
        return (
            None,
            safe_error(
                INVALID_CONFIG,
                "Stored credentials cannot be decrypted. The server encryption key may "
                "have changed (for example after a restart without CREDENTIAL_ENCRYPTION_KEY "
                "or FERNET_KEY in .env). Set a stable key, then re-enter and save credentials.",
            ),
        )


# ──────────────────────────────────────────────────────────────────────
# Public helpers
# ──────────────────────────────────────────────────────────────────────
def encrypt_credential_field(plain: str) -> str:
    """Public alias kept for the router so it doesn't import crypto directly."""
    return encrypt_secret(plain)


def mask_for_display(plain: str) -> str:
    """Returns the safe masked form for UI display only."""
    return mask_secret(plain)


def build_business_central_base_url(environment_name: str, base_url: Optional[str] = None) -> str:
    """Builds the v2.0 data-API base for a given BC environment.

    Example -> 'https://api.businesscentral.dynamics.com/v2.0/Production/api/v2.0'
    """
    base = (base_url or DEFAULT_BC_BASE_URL).rstrip("/")
    env  = (environment_name or "").strip()
    if not env:
        raise ValueError("environment_name is required to build Business Central base URL")
    return f"{base}/v2.0/{env}/api/v2.0"


def build_finflux_gl_api_base_url(environment_name: str, base_url: Optional[str] = None) -> str:
    """Base URL for the Tick & Tie FinFlux custom GL API page."""
    base = (base_url or DEFAULT_BC_BASE_URL).rstrip("/")
    env = (environment_name or "").strip()
    if not env:
        raise ValueError("environment_name is required to build FinFlux GL API base URL")
    return f"{base}/v2.0/{env}/api/tickandtie/finflux/v1.0"


# ──────────────────────────────────────────────────────────────────────
# OAuth token (internal-only)
# ──────────────────────────────────────────────────────────────────────
def _get_access_token(creds: _DecryptedCredentials) -> str:
    """OAuth 2.0 client_credentials. Returned token is NEVER logged or returned
    further than the caller in this module."""
    token_url = MICROSOFT_TOKEN_URL_TPL.format(tenant_id=creds.tenant_id)
    data = {
        "grant_type":    "client_credentials",
        "client_id":     creds.client_id,
        "client_secret": creds.client_secret,
        "scope":         creds.scope or DEFAULT_BC_SCOPE,
    }
    timeout = httpx.Timeout(settings.BC_HTTP_TIMEOUT_SECONDS)
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(token_url, data=data)
        resp.raise_for_status()
        body = resp.json()
    token = body.get("access_token")
    if not token or not isinstance(token, str):
        raise httpx.HTTPError("Token response missing access_token")
    return token


# ──────────────────────────────────────────────────────────────────────
# Generic Business Central GET with pagination — never echoes the token.
# ──────────────────────────────────────────────────────────────────────
def _bc_request_pages(
    creds: _DecryptedCredentials,
    path: str,
    params: Optional[dict[str, str]] = None,
    *,
    api_base: Optional[str] = None,
    max_pages: int = 50,
) -> List[dict[str, Any]]:
    token = _get_access_token(creds)
    base = api_base or build_business_central_base_url(creds.environment_name, creds.base_url)
    url: Optional[str] = f"{base}{path}"

    items: List[dict[str, Any]] = []
    timeout = httpx.Timeout(settings.BC_HTTP_TIMEOUT_SECONDS)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    pages = 0
    with httpx.Client(timeout=timeout) as client:
        while url and pages < max_pages:
            resp = client.get(url, headers=headers, params=params if pages == 0 else None)
            resp.raise_for_status()
            payload = resp.json() if resp.content else {}
            page_items = payload.get("value")
            if isinstance(page_items, list):
                items.extend(page_items)
            url = payload.get("@odata.nextLink") if isinstance(payload, dict) else None
            pages += 1
    return items


# ──────────────────────────────────────────────────────────────────────
# Public connector operations
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ConnectorResult:
    ok: bool
    records: List[dict[str, Any]]
    error: Optional[SafeError] = None


def _safe_run(callable_fn) -> ConnectorResult:
    try:
        records = callable_fn()
        return ConnectorResult(ok=True, records=records)
    except (httpx.HTTPError, ValueError) as exc:
        # _log only safe metadata; never the exception args directly.
        _log.warning("Business Central call failed: %s", type(exc).__name__)
        return ConnectorResult(ok=False, records=[], error=from_exception(exc))
    except Exception as exc:                                  # noqa: BLE001
        _log.exception("Unexpected Business Central error: %s", type(exc).__name__)
        return ConnectorResult(ok=False, records=[], error=safe_error("BC_SERVICE_ERROR"))


def _optional_ledger_error(path: str, exc: BaseException) -> SafeError | None:
    """Map 404 on optional customer/vendor ledger paths to dedicated codes."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    if exc.response is None or exc.response.status_code != 404:
        return None
    if "customerLedgerEntries" in path:
        return safe_error(BC_CUSTOMER_LEDGER_ENDPOINT_NOT_FOUND)
    if "vendorLedgerEntries" in path:
        return safe_error(BC_VENDOR_LEDGER_ENDPOINT_NOT_FOUND)
    err = from_exception(exc)
    if err.code == ENDPOINT_NOT_FOUND:
        if "customerLedger" in path:
            return safe_error(BC_CUSTOMER_LEDGER_ENDPOINT_NOT_FOUND)
        if "vendorLedger" in path:
            return safe_error(BC_VENDOR_LEDGER_ENDPOINT_NOT_FOUND)
    return None


def _safe_run_optional_ledger(path: str, callable_fn) -> ConnectorResult:
    """Optional BC ledger sync — endpoint-missing must not fail the integration."""
    try:
        records = callable_fn()
        return ConnectorResult(ok=True, records=records)
    except (httpx.HTTPError, ValueError) as exc:
        _log.warning("Business Central optional ledger call failed: %s", type(exc).__name__)
        mapped = _optional_ledger_error(path, exc)
        if mapped:
            return ConnectorResult(ok=False, records=[], error=mapped)
        return ConnectorResult(ok=False, records=[], error=from_exception(exc))
    except Exception as exc:                                  # noqa: BLE001
        _log.exception("Unexpected Business Central optional ledger error: %s", type(exc).__name__)
        return ConnectorResult(ok=False, records=[], error=safe_error("BC_SERVICE_ERROR"))


def test_connection(rec: BusinessCentralCredential) -> ConnectorResult:
    """Validates credentials by hitting /companies. Returns the company list
    so the UI can immediately let the admin pick one."""
    creds, decrypt_err = _decrypt_record_safe(rec)
    if decrypt_err:
        return ConnectorResult(ok=False, records=[], error=decrypt_err)
    if not creds.environment_name:
        return ConnectorResult(ok=False, records=[],
                               error=safe_error(INVALID_CONFIG, "environment_name is required"))
    return _safe_run(lambda: _normalize_companies(_bc_request_pages(creds, "/companies")))


def fetch_companies(rec: BusinessCentralCredential) -> ConnectorResult:
    creds, decrypt_err = _decrypt_record_safe(rec)
    if decrypt_err:
        return ConnectorResult(ok=False, records=[], error=decrypt_err)
    return _safe_run(lambda: _normalize_companies(_bc_request_pages(creds, "/companies")))


def sync_gl_accounts(rec: BusinessCentralCredential, company_id: str) -> ConnectorResult:
    if not company_id:
        return ConnectorResult(ok=False, records=[],
                               error=safe_error(INVALID_CONFIG, "company_id is required"))
    creds, decrypt_err = _decrypt_record_safe(rec)
    if decrypt_err:
        return ConnectorResult(ok=False, records=[], error=decrypt_err)
    path  = f"/companies({company_id})/accounts"
    return _safe_run(lambda: _normalize_accounts(_bc_request_pages(creds, path)))


def sync_general_ledger_entries(
    rec: BusinessCentralCredential,
    company_id: str,
    *,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> ConnectorResult:
    if not company_id:
        return ConnectorResult(ok=False, records=[],
                               error=safe_error(INVALID_CONFIG, "company_id is required"))
    creds, decrypt_err = _decrypt_record_safe(rec)
    if decrypt_err:
        return ConnectorResult(ok=False, records=[], error=decrypt_err)

    def _run() -> List[dict[str, Any]]:
        params = _build_gl_entry_filter(from_date, to_date)
        finflux_path = f"/companies({company_id})/finfluxGlEntries"
        finflux_base = build_finflux_gl_api_base_url(
            creds.environment_name, creds.base_url,
        )
        try:
            rows = _bc_request_pages(
                creds, finflux_path, params, api_base=finflux_base,
            )
            _log.info(
                "bc.sync_gl_entries source=finflux company=%s rows=%d",
                company_id, len(rows),
            )
            return _normalize_gl_entries(rows)
        except httpx.HTTPStatusError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            std_path = f"/companies({company_id})/generalLedgerEntries"
            rows = _bc_request_pages(creds, std_path, params)
            _log.info(
                "bc.sync_gl_entries source=standard company=%s rows=%d",
                company_id, len(rows),
            )
            return _normalize_gl_entries(rows)

    return _safe_run(_run)


def sync_customers(rec: BusinessCentralCredential, company_id: str) -> ConnectorResult:
    if not company_id:
        return ConnectorResult(ok=False, records=[],
                               error=safe_error(INVALID_CONFIG, "company_id is required"))
    creds, decrypt_err = _decrypt_record_safe(rec)
    if decrypt_err:
        return ConnectorResult(ok=False, records=[], error=decrypt_err)
    path = f"/companies({company_id})/customers"
    return _safe_run(lambda: _normalize_customers(_bc_request_pages(creds, path)))


def sync_vendors(rec: BusinessCentralCredential, company_id: str) -> ConnectorResult:
    if not company_id:
        return ConnectorResult(ok=False, records=[],
                               error=safe_error(INVALID_CONFIG, "company_id is required"))
    creds, decrypt_err = _decrypt_record_safe(rec)
    if decrypt_err:
        return ConnectorResult(ok=False, records=[], error=decrypt_err)
    path = f"/companies({company_id})/vendors"
    return _safe_run(lambda: _normalize_vendors(_bc_request_pages(creds, path)))


def sync_customer_ledger_entries(
    rec: BusinessCentralCredential, company_id: str,
) -> ConnectorResult:
    if not company_id:
        return ConnectorResult(ok=False, records=[],
                               error=safe_error(INVALID_CONFIG, "company_id is required"))
    creds, decrypt_err = _decrypt_record_safe(rec)
    if decrypt_err:
        return ConnectorResult(ok=False, records=[], error=decrypt_err)
    path = f"/companies({company_id})/customerLedgerEntries"
    return _safe_run_optional_ledger(
        path, lambda: _normalize_customer_ledger_entries(_bc_request_pages(creds, path)),
    )


def sync_vendor_ledger_entries(
    rec: BusinessCentralCredential, company_id: str,
) -> ConnectorResult:
    if not company_id:
        return ConnectorResult(ok=False, records=[],
                               error=safe_error(INVALID_CONFIG, "company_id is required"))
    creds, decrypt_err = _decrypt_record_safe(rec)
    if decrypt_err:
        return ConnectorResult(ok=False, records=[], error=decrypt_err)
    path = f"/companies({company_id})/vendorLedgerEntries"
    return _safe_run_optional_ledger(
        path, lambda: _normalize_vendor_ledger_entries(_bc_request_pages(creds, path)),
    )


def sync_dimensions(rec: BusinessCentralCredential, company_id: str) -> ConnectorResult:
    """Fetches Business Central dimension *values* for the given company —
    the FinFlux Sub-GL layer.

    Primary source: `/companies({id})/dimensionValues` (flat list).
    Secondary (best-effort) lookup: `/companies({id})/dimensions` to enrich
    each value with its parent dimension's `code` + `displayName`. The BC v2.0
    value payload only carries `dimensionId` (GUID), not the human-readable
    parent code, so without enrichment the ontology UI would only see GUIDs.

    The dimensions lookup is best-effort: any failure there is swallowed and
    the values are still stored, keyed by `dimensionId`."""
    if not company_id:
        return ConnectorResult(ok=False, records=[],
                               error=safe_error(INVALID_CONFIG, "company_id is required"))
    creds, decrypt_err = _decrypt_record_safe(rec)
    if decrypt_err:
        return ConnectorResult(ok=False, records=[], error=decrypt_err)

    values_path = f"/companies({company_id})/dimensionValues"
    page_params = {"$top": "500"}

    def _run() -> List[dict[str, Any]]:
        raw_values = _bc_request_pages(creds, values_path, page_params)
        dim_lookup = _fetch_dimension_lookup(creds, company_id)
        normalized = _normalize_dimension_values(raw_values, dim_lookup)
        _log.info(
            "bc.sync_dimensions company=%s path=%s raw_rows=%d normalized_rows=%d "
            "dimension_lookup_size=%d",
            company_id, values_path, len(raw_values), len(normalized), len(dim_lookup),
        )
        return normalized

    return _safe_run(_run)


def _fetch_dimension_lookup(
    creds: _DecryptedCredentials, company_id: str,
) -> dict[str, dict[str, Optional[str]]]:
    """Best-effort `dimensionId -> {code, name}` map from `/dimensions`.
    Returns an empty dict on any failure so the values sync still proceeds."""
    path = f"/companies({company_id})/dimensions"
    try:
        rows = _bc_request_pages(creds, path, {"$top": "500"})
    except (httpx.HTTPError, ValueError):
        _log.warning("bc.dimension_lookup_failed company=%s path=%s", company_id, path)
        return {}
    lookup: dict[str, dict[str, Optional[str]]] = {}
    for r in rows:
        dim_id = _str(r.get("id"))
        if not dim_id:
            continue
        lookup[dim_id] = {
            "code": _str(r.get("code")),
            "name": _str(r.get("displayName") or r.get("name")),
        }
    return lookup


def _build_gl_entry_filter(from_date: Optional[str], to_date: Optional[str]) -> Optional[dict[str, str]]:
    """Builds an OData $filter on `postingDate` from optional ISO dates.
    Returns None when no filter is needed so callers don't add empty params."""
    parts: List[str] = []
    if from_date:
        parts.append(f"postingDate ge {from_date}")
    if to_date:
        parts.append(f"postingDate le {to_date}")
    if not parts:
        return None
    return {"$filter": " and ".join(parts)}


# ──────────────────────────────────────────────────────────────────────
# Normalizers — keep DB rows shape-stable regardless of BC payload drift.
# ──────────────────────────────────────────────────────────────────────
def _str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _normalize_companies(rows: Iterable[dict[str, Any]]) -> List[dict[str, Any]]:
    out: List[dict[str, Any]] = []
    for r in rows:
        cid   = _str(r.get("id"))
        cname = _str(r.get("displayName") or r.get("name"))
        if cid and cname:
            out.append({"company_id": cid, "company_name": cname})
    return out


def _normalize_accounts(rows: Iterable[dict[str, Any]]) -> List[dict[str, Any]]:
    """Maps Business Central /accounts items to FinFlux's normalized GL row.
    Original payload is preserved under `raw_source_json` so analysts can
    still inspect fields we don't promote to top-level columns."""
    out: List[dict[str, Any]] = []
    for r in rows:
        no = _str(r.get("number") or r.get("accountNumber"))
        if not no:
            continue
        display = _str(r.get("displayName") or r.get("name"))
        out.append({
            "account_no":       no,
            "account_name":     display,
            "display_name":     display,
            "account_category": _str(r.get("accountCategory") or r.get("category")),
            "sub_category":     _str(r.get("subCategory")),
            "account_type":     _str(r.get("accountType")),
            "blocked":          _bool(r.get("blocked")),
            "external_id":      _str(r.get("id")),
            "raw_source_json":  r,
        })
    return out


def _normalize_dimension_values(
    rows: Iterable[dict[str, Any]],
    dim_lookup: Optional[dict[str, dict[str, Optional[str]]]] = None,
) -> List[dict[str, Any]]:
    """BC v2.0 `dimensionValues` payload per item:
        { id, code, dimensionId, displayName, consolidationCode,
          lastModifiedDateTime }

    There is no `dimensionCode` or nested `dimension` object on the value, so
    the parent code is resolved via `dim_lookup` when available; when not, the
    `dimensionId` GUID is used as a stable fallback so the row is still
    mappable in ontology (the unique constraint is `(entity, erp_type, company,
    dimension_code, value_code)`).

    A row is dropped only when both `value_code` and any dimension identifier
    are missing — without those it is not addressable.

    Also de-duplicates within a single payload on
    `(dimension_code, value_code)` to keep `_replace_dimensions` insert-safe
    against the unique constraint."""
    lookup = dim_lookup or {}
    seen: set[tuple[str, str]] = set()
    out: List[dict[str, Any]] = []
    for r in rows:
        v_code = _str(r.get("code"))
        dim_id = _str(r.get("dimensionId") or (r.get("dimension") or {}).get("id"))
        legacy_d_code = _str(
            r.get("dimensionCode") or (r.get("dimension") or {}).get("code")
        )
        looked_up = lookup.get(dim_id) if dim_id else None
        d_code = legacy_d_code or (looked_up or {}).get("code") or dim_id
        if not v_code or not d_code:
            continue
        d_name = (
            _str(r.get("dimensionDisplayName") or r.get("dimensionName"))
            or (looked_up or {}).get("name")
        )
        v_name = _str(r.get("displayName") or r.get("name"))
        key = (d_code, v_code)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "dimension_id":   dim_id,
            "dimension_code": d_code,
            "dimension_name": d_name,
            "value_id":       _str(r.get("id")),
            "value_code":     v_code,
            "value_name":     v_name,
            "display_name":   v_name or v_code,
            "raw_source_json": r,
        })
    return out


def _parse_date(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _decode_bc_enum_label(value: Optional[str]) -> Optional[str]:
    """Decode BC OData enum labels such as G_x002F_L_x0020_Account -> G/L Account."""
    s = _str(value)
    if not s:
        return None
    decoded = (
        s.replace("_x002F_", "/")
        .replace("_x0020_", " ")
        .replace("_x003A_", ":")
    )
    return decoded.strip() or None


def _normalize_gl_entries(rows: Iterable[dict[str, Any]]) -> List[dict[str, Any]]:
    """Maps BC / FinFlux GL rows to FinFlux's normalized transaction row."""
    out: List[dict[str, Any]] = []
    for r in rows:
        ext_no = _str(r.get("entryNumber") or r.get("number") or r.get("id"))
        if not ext_no:
            continue
        d = _parse_date(r.get("postingDate"))
        debit  = r.get("debitAmount")
        credit = r.get("creditAmount")
        amount = r.get("amount")
        if amount is None and (debit is not None or credit is not None):
            try:
                amount = (debit or 0) - (credit or 0)
            except TypeError:
                amount = None
        source_type = _str(r.get("sourceType"))
        source_number = _str(r.get("sourceNumber") or r.get("sourceNo"))
        bal_account_type = _decode_bc_enum_label(_str(r.get("balAccountType")))
        bal_account_number = _str(r.get("balAccountNumber") or r.get("balAccountNo"))
        external_document_number = _str(
            r.get("externalDocumentNumber") or r.get("externalDocumentNo"),
        )
        out.append({
            "external_entry_no": ext_no,
            "external_id":       _str(r.get("id")),
            "entry_number":      _str(r.get("entryNumber")),
            "posting_date":      d.date() if d else None,
            "document_no":       _str(r.get("documentNumber")),
            "account_id":        _str((r.get("account") or {}).get("id") or r.get("accountId")),
            "account_no":        _str(r.get("accountNumber") or (r.get("account") or {}).get("number")),
            "debit_amount":      debit,
            "credit_amount":     credit,
            "amount":            amount,
            "dimension_set_id":  _str(r.get("dimensionSetId") or r.get("dimensionSetID")),
            "description":       _str(r.get("description")),
            "source_type":       source_type,
            "source_number":     source_number,
            "bal_account_type":  bal_account_type,
            "bal_account_number": bal_account_number,
            "external_document_number": external_document_number,
            "raw_source_json":   r,
        })
    return out


def _normalize_customers(rows: Iterable[dict[str, Any]]) -> List[dict[str, Any]]:
    out: List[dict[str, Any]] = []
    for r in rows:
        no = _str(r.get("number"))
        if not no:
            continue
        display = _str(r.get("displayName") or r.get("name"))
        out.append({
            "customer_no":    no,
            "display_name":   display,
            "external_id":    _str(r.get("id")),
            "raw_source_json": r,
        })
    return out


def _normalize_vendors(rows: Iterable[dict[str, Any]]) -> List[dict[str, Any]]:
    out: List[dict[str, Any]] = []
    for r in rows:
        no = _str(r.get("number"))
        if not no:
            continue
        display = _str(r.get("displayName") or r.get("name"))
        out.append({
            "vendor_no":      no,
            "display_name":   display,
            "external_id":    _str(r.get("id")),
            "raw_source_json": r,
        })
    return out


def _normalize_customer_ledger_entries(
    rows: Iterable[dict[str, Any]],
) -> List[dict[str, Any]]:
    out: List[dict[str, Any]] = []
    for r in rows:
        ext_no = _str(r.get("entryNumber") or r.get("number") or r.get("id"))
        cust_no = _str(r.get("customerNumber") or (r.get("customer") or {}).get("number"))
        if not ext_no or not cust_no:
            continue
        d = _parse_date(r.get("postingDate"))
        out.append({
            "external_entry_no": ext_no,
            "customer_no":       cust_no,
            "customer_name":     _str(r.get("customerName") or (r.get("customer") or {}).get("displayName")),
            "document_no":       _str(r.get("documentNumber")),
            "posting_date":      d.date() if d else None,
            "amount":            r.get("amount"),
            "remaining_amount":  r.get("remainingAmount"),
            "raw_source_json":   r,
        })
    return out


def _normalize_vendor_ledger_entries(
    rows: Iterable[dict[str, Any]],
) -> List[dict[str, Any]]:
    out: List[dict[str, Any]] = []
    for r in rows:
        ext_no = _str(r.get("entryNumber") or r.get("number") or r.get("id"))
        vend_no = _str(r.get("vendorNumber") or (r.get("vendor") or {}).get("number"))
        if not ext_no or not vend_no:
            continue
        d = _parse_date(r.get("postingDate"))
        out.append({
            "external_entry_no": ext_no,
            "vendor_no":         vend_no,
            "vendor_name":       _str(r.get("vendorName") or (r.get("vendor") or {}).get("displayName")),
            "document_no":       _str(r.get("documentNumber")),
            "posting_date":      d.date() if d else None,
            "amount":            r.get("amount"),
            "remaining_amount":  r.get("remainingAmount"),
            "raw_source_json":   r,
        })
    return out
