# @file    services/subledger_catalog.py
#
# Customer / vendor master options for ontology mapping and optional
# GL-entry filtering during Flux generation.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import (
    ErpCustomer,
    ErpGeneralLedgerEntry,
    ErpSyncRun,
    ErpVendor,
    OntologyMapping,
    SYNC_CUSTOMER_LEDGER_ENTRIES,
    SYNC_VENDOR_LEDGER_ENTRIES,
)
from app.services.erp.safe_errors import (
    BC_CUSTOMER_LEDGER_ENDPOINT_NOT_FOUND,
    BC_VENDOR_LEDGER_ENDPOINT_NOT_FOUND,
)

TOKEN_CUSTOMER = "customer"
TOKEN_VENDOR = "vendor"

_OPTIONAL_LEDGER_CODES = frozenset({
    BC_CUSTOMER_LEDGER_ENDPOINT_NOT_FOUND,
    BC_VENDOR_LEDGER_ENDPOINT_NOT_FOUND,
})

_LEDGER_OPTIONAL_WARNING = (
    "Customer/Vendor ledger entries are not exposed in this Business Central API. "
    "Custom API pages are required for ledger-level details."
)

_ONTOLOGY_UNAVAILABLE_MESSAGE = (
    "Sync Customers and Vendors from Business Central Integrations to enable "
    "Customer/Vendor mapping."
)

_SUBLEDGER_FILTER_WARNING = (
    "Customer/Vendor filtering could not be applied because BC GL entries "
    "do not include customer/vendor references."
)


@dataclass(frozen=True)
class SubledgerOption:
    """One selectable customer or vendor row for the Ontology UI."""
    value: str
    label: str
    kind: str
    code: str
    name: str


def _str_val(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _token(kind: str, code: str, name: str | None = None) -> tuple[str, str]:
    code = code.strip()
    if not code:
        return "", ""
    value = f"{kind}:{code}"
    if name:
        label = f"{kind.capitalize()}: {code} — {name}"
    else:
        label = f"{kind.capitalize()}: {code}"
    return value, label


def _party_tokens_from_raw(raw: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Extract customer/vendor tokens from a BC GL raw payload."""
    out: list[tuple[str, str, str]] = []

    source_type = (_str_val(raw.get("sourceType")) or "").lower()
    source_no = (
        _str_val(raw.get("sourceNumber"))
        or _str_val(raw.get("sourceNo"))
        or _str_val(raw.get("source_no"))
    )
    source_name = _str_val(raw.get("sourceName")) or _str_val(raw.get("source_name"))
    if source_type == "customer" and source_no:
        out.append((TOKEN_CUSTOMER, source_no, source_name or ""))
        return out
    if source_type == "vendor" and source_no:
        out.append((TOKEN_VENDOR, source_no, source_name or ""))
        return out

    bal_type = (
        _str_val(raw.get("balAccountType"))
        or _str_val(raw.get("balancingAccountType"))
        or ""
    ).lower()
    bal_no = (
        _str_val(raw.get("balAccountNumber"))
        or _str_val(raw.get("balAccountNo"))
        or _str_val(raw.get("balancingAccountNumber"))
        or _str_val(raw.get("balancingAccountNo"))
    )
    if "customer" in bal_type and bal_no:
        out.append((TOKEN_CUSTOMER, bal_no, ""))
        return out
    if "vendor" in bal_type and bal_no:
        out.append((TOKEN_VENDOR, bal_no, ""))
        return out

    cust_name = _str_val(raw.get("customer_name"))
    vend_name_qb = _str_val(raw.get("vendor_name"))
    if cust_name:
        out.append((TOKEN_CUSTOMER, cust_name, cust_name))
    if vend_name_qb:
        out.append((TOKEN_VENDOR, vend_name_qb, vend_name_qb))

    return out


def entry_subledger_tokens(entry: ErpGeneralLedgerEntry) -> set[str]:
    """Matchable customer/vendor tokens on one GL entry (no document tokens)."""
    from app.services.flux_ai.gl_entry_party import resolve_gl_entry_party

    tokens: set[str] = set()
    ref = resolve_gl_entry_party(entry)
    if ref is not None:
        val, _ = _token(ref.party_type, ref.party_no, ref.party_name)
        if val:
            tokens.add(val)
            return tokens
    raw = entry.raw_source_json if isinstance(entry.raw_source_json, dict) else {}
    if raw:
        for kind, code, name in _party_tokens_from_raw(raw):
            val, _ = _token(kind, code, name or None)
            if val:
                tokens.add(val)
    return tokens


def extract_mapped_subledger_values(mapping: OntologyMapping) -> set[str]:
    """Read selected customer/vendor tokens from ontology mapping JSON."""
    raw = getattr(mapping, "erp_subledger_json", None)
    if not raw:
        raw = mapping.erp_subgl_json
    return _extract_subledger_tokens_list(raw)


def _extract_subledger_tokens_list(raw: Any) -> set[str]:
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            s = item.strip()
            if s and ":" in s:
                kind = s.split(":", 1)[0].lower()
                if kind in (TOKEN_CUSTOMER, TOKEN_VENDOR):
                    out.add(s)
            continue
        if isinstance(item, dict):
            kind = _str_val(item.get("kind") or item.get("type"))
            code = _str_val(item.get("code") or item.get("value") or item.get("no"))
            if kind and code and kind.lower() in (TOKEN_CUSTOMER, TOKEN_VENDOR):
                out.add(f"{kind.lower()}:{code}")
    return out


def subledger_selection_labels(raw: Any) -> list[str]:
    """Human-readable labels for AI commentary / diagnostics."""
    if not isinstance(raw, list):
        return []
    labels: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            label = _str_val(item.get("label"))
            if label:
                labels.append(label)
                continue
            kind = _str_val(item.get("type") or item.get("kind"))
            code = _str_val(item.get("code"))
            name = _str_val(item.get("name"))
            if kind and code:
                _, lbl = _token(kind.lower(), code, name)
                if lbl:
                    labels.append(lbl)
            continue
        if isinstance(item, str) and ":" in item:
            kind, code = item.split(":", 1)
            _, lbl = _token(kind.lower(), code, None)
            if lbl:
                labels.append(lbl)
    return labels


def entry_matches_subledger(
    entry: ErpGeneralLedgerEntry, selected: set[str],
) -> bool:
    """True when no filter, or entry matches at least one selected token."""
    if not selected:
        return True
    return bool(entry_subledger_tokens(entry) & selected)


def gl_entries_support_party_filter(entries: list[ErpGeneralLedgerEntry]) -> bool:
    """False when none of the entries expose customer/vendor references."""
    if not entries:
        return False
    return any(entry_subledger_tokens(e) for e in entries)


def subledger_filter_warning(
    selected: set[str],
    entries: list[ErpGeneralLedgerEntry],
) -> str | None:
    if not selected:
        return None
    if not gl_entries_support_party_filter(entries):
        return _SUBLEDGER_FILTER_WARNING
    return None


def _last_sync_run(db: Session, entity_id: str, sync_type: str) -> ErpSyncRun | None:
    return (
        db.query(ErpSyncRun)
        .filter(
            ErpSyncRun.entity_id == entity_id,
            ErpSyncRun.sync_type == sync_type,
        )
        .order_by(ErpSyncRun.started_at.desc())
        .first()
    )


def _ledger_endpoint_unavailable(db: Session, entity_id: str, sync_type: str) -> bool:
    run = _last_sync_run(db, entity_id, sync_type)
    return bool(run and run.safe_error_code in _OPTIONAL_LEDGER_CODES)


def ledger_endpoints_unavailable(db: Session, entity_id: str) -> bool:
    """True when optional customer or vendor ledger API is not exposed."""
    return (
        _ledger_endpoint_unavailable(db, entity_id, SYNC_CUSTOMER_LEDGER_ENTRIES)
        or _ledger_endpoint_unavailable(db, entity_id, SYNC_VENDOR_LEDGER_ENTRIES)
    )


def _add_party_option(
    seen: dict[tuple[str, str, str], SubledgerOption],
    kind: str,
    code: str,
    name: str | None,
) -> None:
    key = (kind, code, (name or "").strip())
    if key in seen:
        return
    val, label = _token(kind, code, name)
    if not val:
        return
    seen[key] = SubledgerOption(
        value=val, label=label, kind=kind, code=code, name=(name or "").strip(),
    )


def _resolve_erp_type(db: Session, entity_id: str) -> str | None:
    from app.models.models import EntityErpMapping, ERP_TYPE_QUICKBOOKS
    mapping = (
        db.query(EntityErpMapping.erp_key)
        .filter(EntityErpMapping.entity_id == entity_id)
        .first()
    )
    if not mapping:
        return None
    if mapping.erp_key == "bc":
        return "bc"
    if mapping.erp_key == ERP_TYPE_QUICKBOOKS:
        return ERP_TYPE_QUICKBOOKS
    return None


def _options_from_customer_master(
    db: Session, entity_id: str, *, erp_type: str | None = None,
) -> list[SubledgerOption]:
    q = db.query(ErpCustomer.customer_no, ErpCustomer.display_name).filter(
        ErpCustomer.entity_id == entity_id,
    )
    if erp_type:
        q = q.filter(ErpCustomer.erp_type == erp_type)
    rows = q.all()
    seen: dict[tuple[str, str, str], SubledgerOption] = {}
    for cust_no, display_name in rows:
        if cust_no:
            _add_party_option(seen, TOKEN_CUSTOMER, str(cust_no), display_name)
    return sorted(seen.values(), key=lambda o: o.label)


def _options_from_vendor_master(
    db: Session, entity_id: str, *, erp_type: str | None = None,
) -> list[SubledgerOption]:
    q = db.query(ErpVendor.vendor_no, ErpVendor.display_name).filter(
        ErpVendor.entity_id == entity_id,
    )
    if erp_type:
        q = q.filter(ErpVendor.erp_type == erp_type)
    rows = q.all()
    seen: dict[tuple[str, str, str], SubledgerOption] = {}
    for vend_no, display_name in rows:
        if vend_no:
            _add_party_option(seen, TOKEN_VENDOR, str(vend_no), display_name)
    return sorted(seen.values(), key=lambda o: o.label)


def build_subledger_options_response(db: Session, entity_id: str) -> dict[str, Any]:
    """Structured customer/vendor lists for the Ontology dropdown API."""
    warnings: list[str] = []
    cust_ledger_unavail = _ledger_endpoint_unavailable(
        db, entity_id, SYNC_CUSTOMER_LEDGER_ENTRIES,
    )
    vend_ledger_unavail = _ledger_endpoint_unavailable(
        db, entity_id, SYNC_VENDOR_LEDGER_ENTRIES,
    )

    erp_type = _resolve_erp_type(db, entity_id)
    customers = _options_from_customer_master(db, entity_id, erp_type=erp_type)
    vendors = _options_from_vendor_master(db, entity_id, erp_type=erp_type)

    if cust_ledger_unavail or vend_ledger_unavail:
        warnings.append(_LEDGER_OPTIONAL_WARNING)

    message: str | None = None
    if not customers and not vendors:
        message = _ONTOLOGY_UNAVAILABLE_MESSAGE

    def _row(opt: SubledgerOption) -> dict[str, str]:
        return {
            "type":  opt.kind,
            "code":  opt.code,
            "name":  opt.name,
            "label": opt.label,
        }

    return {
        "customers": [_row(o) for o in customers],
        "vendors":   [_row(o) for o in vendors],
        "message":   message,
        "warnings":  warnings,
    }


def build_subledger_catalog(
    db: Session, entity_id: str, *, limit_rows: int = 50000,
) -> list[SubledgerOption]:
    """Flat customer + vendor options (legacy BC integration endpoint)."""
    resp = build_subledger_options_response(db, entity_id)
    out: list[SubledgerOption] = []
    for row in resp["customers"] + resp["vendors"]:
        out.append(SubledgerOption(
            value=f"{row['type']}:{row['code']}",
            label=row["label"],
            kind=row["type"],
            code=row["code"],
            name=row.get("name") or "",
        ))
    return out


def calculation_mode_for_statement(statement: str) -> str:
    if (statement or "").strip().lower() == "bs":
        return "balance_at_date"
    return "net_change"


def calculation_mode_label(mode: str) -> str:
    if mode == "balance_at_date":
        return "Calculated using Balance at Date"
    return "Calculated using Net Change"
