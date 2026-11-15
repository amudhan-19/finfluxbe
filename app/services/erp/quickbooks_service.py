# @file    services/erp/quickbooks_service.py
#
# QuickBooks Online v3 API connector — OAuth-backed HTTP only.

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, List, Optional
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.models.models import QuickBooksCredential

from .quickbooks_mapper import (
    normalize_accounts,
    normalize_customers,
    normalize_general_ledger_report,
    normalize_vendors,
)
from .quickbooks_oauth import get_valid_access_token, qb_api_base
from .safe_errors import (
    QUICKBOOKS_ACCOUNTS_SYNC_FAILED,
    QUICKBOOKS_COMPANY_INFO_FAILED,
    QUICKBOOKS_CUSTOMERS_SYNC_FAILED,
    QUICKBOOKS_GL_SYNC_FAILED,
    QUICKBOOKS_REALM_ID_MISSING,
    QUICKBOOKS_SERVICE_ERROR,
    QUICKBOOKS_VENDORS_SYNC_FAILED,
    SafeError,
    from_exception,
    safe_error,
)

_log = logging.getLogger(__name__)

MINOR_VERSION = "65"


@dataclass(frozen=True)
class ConnectorResult:
    ok: bool
    records: List[dict[str, Any]]
    error: Optional[SafeError] = None
    meta: Optional[dict[str, Any]] = None


def _minor_qs(extra: str = "") -> str:
    base = f"minorversion={MINOR_VERSION}"
    if extra:
        return f"{extra}&{base}" if "?" in extra else f"?{extra}&{base}"
    return f"?{base}"


def _safe_run(callable_fn) -> ConnectorResult:
    try:
        result = callable_fn()
        if isinstance(result, tuple) and len(result) == 2:
            records, meta = result
            return ConnectorResult(ok=True, records=records, meta=meta)
        return ConnectorResult(ok=True, records=result)
    except (httpx.HTTPError, ValueError) as exc:
        _log.warning("QuickBooks call failed: %s", type(exc).__name__)
        err = from_exception(exc)
        if err.code.startswith("BC_") or err.code in ("AUTH_FAILED", "ENDPOINT_NOT_FOUND"):
            err = safe_error(QUICKBOOKS_SERVICE_ERROR, err.message)
        return ConnectorResult(ok=False, records=[], error=err)
    except Exception as exc:  # noqa: BLE001
        _log.exception("Unexpected QuickBooks error: %s", type(exc).__name__)
        return ConnectorResult(ok=False, records=[], error=safe_error(QUICKBOOKS_SERVICE_ERROR))


def _require_realm(rec: QuickBooksCredential) -> str:
    realm = (rec.realm_id or "").strip()
    if not realm:
        raise ValueError("realmId is required")
    return realm


def _qb_get(rec: QuickBooksCredential, path: str) -> dict[str, Any]:
    token, err = get_valid_access_token(rec)
    if err:
        raise httpx.HTTPError(err.message)
    assert token
    base = qb_api_base(rec)
    url = f"{base}{path}"
    timeout = httpx.Timeout(settings.QUICKBOOKS_HTTP_TIMEOUT_SECONDS)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


def _qb_query(rec: QuickBooksCredential, realm_id: str, sql: str) -> list[dict[str, Any]]:
    encoded = quote(sql, safe="")
    path = f"/v3/company/{realm_id}/query?query={encoded}&minorversion={MINOR_VERSION}"
    payload = _qb_get(rec, path)
    qr = payload.get("QueryResponse", {})
    for key in ("Account", "Customer", "Vendor", "Invoice", "Bill"):
        rows = qr.get(key)
        if isinstance(rows, list):
            return rows
        if isinstance(rows, dict):
            return [rows]
    return []


def fetch_company_info(rec: QuickBooksCredential) -> ConnectorResult:
    def _run() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        realm = _require_realm(rec)
        path = f"/v3/company/{realm}/companyinfo/{realm}{_minor_qs()}"
        payload = _qb_get(rec, path)
        info = payload.get("CompanyInfo") or payload.get("companyInfo") or {}
        if isinstance(info, list):
            info = info[0] if info else {}
        name = info.get("CompanyName") or info.get("LegalName") or rec.company_name
        meta = {"realm_id": realm, "company_name": name, "raw": info}
        return ([{"company_id": realm, "company_name": name or realm}], meta)

    result = _safe_run(_run)
    if not result.ok and result.error and result.error.code == QUICKBOOKS_SERVICE_ERROR:
        return ConnectorResult(ok=False, records=[], error=safe_error(QUICKBOOKS_COMPANY_INFO_FAILED))
    return result


def test_connection(rec: QuickBooksCredential) -> ConnectorResult:
    if not rec.realm_id:
        return ConnectorResult(ok=False, records=[], error=safe_error(QUICKBOOKS_REALM_ID_MISSING))
    return fetch_company_info(rec)


def sync_accounts(rec: QuickBooksCredential) -> ConnectorResult:
    def _run() -> list[dict[str, Any]]:
        realm = _require_realm(rec)
        rows = _qb_query(rec, realm, "select * from Account")
        return normalize_accounts(rows)

    result = _safe_run(_run)
    if not result.ok:
        return ConnectorResult(
            ok=False, records=[], error=safe_error(QUICKBOOKS_ACCOUNTS_SYNC_FAILED, result.error.message if result.error else None),
        )
    return result


def sync_customers(rec: QuickBooksCredential) -> ConnectorResult:
    def _run() -> list[dict[str, Any]]:
        realm = _require_realm(rec)
        rows = _qb_query(rec, realm, "select * from Customer")
        return normalize_customers(rows)

    result = _safe_run(_run)
    if not result.ok:
        return ConnectorResult(
            ok=False, records=[], error=safe_error(QUICKBOOKS_CUSTOMERS_SYNC_FAILED, result.error.message if result.error else None),
        )
    return result


def sync_vendors(rec: QuickBooksCredential) -> ConnectorResult:
    def _run() -> list[dict[str, Any]]:
        realm = _require_realm(rec)
        rows = _qb_query(rec, realm, "select * from Vendor")
        return normalize_vendors(rows)

    result = _safe_run(_run)
    if not result.ok:
        return ConnectorResult(
            ok=False, records=[], error=safe_error(QUICKBOOKS_VENDORS_SYNC_FAILED, result.error.message if result.error else None),
        )
    return result


def sync_general_ledger(
    rec: QuickBooksCredential,
    *,
    start_date: str,
    end_date: str,
    accounting_method: str = "Accrual",
) -> ConnectorResult:
    def _run() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        realm = _require_realm(rec)
        path = (
            f"/v3/company/{realm}/reports/GeneralLedger"
            f"?start_date={start_date}&end_date={end_date}"
            f"&accounting_method={accounting_method}&minorversion={MINOR_VERSION}"
        )
        _log.info(
            "[QB GL SYNC] QuickBooks request entity=%s realm=%s start=%s end=%s accounting_method=%s path=%s",
            rec.entity_id, realm, start_date, end_date, accounting_method, path,
        )
        report = _qb_get(rec, path)
        header = report.get("Header") or {}
        _log.info(
            "[QB GL SYNC] Raw QB response keys=%s header=%s",
            list(report.keys()),
            json.dumps(header, default=str)[:800],
        )
        normalized, parse_diag = normalize_general_ledger_report(report, realm_id=realm)
        diagnostics: dict[str, Any] = {
            "realmId": realm,
            "environment": rec.environment,
            "start_date": start_date,
            "end_date": end_date,
            "accounting_method": accounting_method,
            "report_name": header.get("ReportName") or "GeneralLedger",
            **parse_diag,
        }
        sample = normalized[:3]
        _log.info(
            "[QB GL SYNC] Diagnostics=%s sample(first %d)=%s",
            json.dumps({k: diagnostics[k] for k in (
                "realmId", "environment", "start_date", "end_date", "accounting_method",
                "report_name", "top_level_row_count", "nested_row_count", "data_row_count",
                "skipped_summary_row_count", "skipped_beginning_balance_count", "records_processed",
            ) if k in diagnostics}, default=str),
            len(sample),
            json.dumps(sample, default=str),
        )
        return normalized, diagnostics

    result = _safe_run(_run)
    if not result.ok:
        err_msg = result.error.message if result.error else None
        err_code = result.error.code if result.error else None
        _log.warning(
            "[QB GL SYNC] Failed entity=%s code=%s message=%s",
            rec.entity_id, err_code, err_msg,
        )
        return ConnectorResult(
            ok=False, records=[], error=safe_error(QUICKBOOKS_GL_SYNC_FAILED, err_msg),
        )
    _log.info(
        "[QB GL SYNC] Success entity=%s records=%d",
        rec.entity_id, len(result.records),
    )
    return result


def fetch_report(
    rec: QuickBooksCredential,
    report_name: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    accounting_method: str = "Accrual",
) -> ConnectorResult:
    def _run() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        realm = _require_realm(rec)
        params = [f"accounting_method={accounting_method}"]
        if start_date:
            params.append(f"start_date={start_date}")
        if end_date:
            params.append(f"end_date={end_date}")
        qs = "?" + "&".join(params) + _minor_qs("&")
        path = f"/v3/company/{realm}/reports/{report_name}{qs}"
        payload = _qb_get(rec, path)
        return ([], {"report": payload, "report_name": report_name})

    return _safe_run(_run)
