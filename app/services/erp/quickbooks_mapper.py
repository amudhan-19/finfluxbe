# @file    services/erp/quickbooks_mapper.py
#
# Normalize QuickBooks Online API payloads into FinFlux ERP row dicts.

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def _str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _bool_active(raw: Any) -> bool:
    if raw is True:
        return True
    if raw is False:
        return False
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes")
    return bool(raw)


def _parse_date(val: Any) -> Optional[date]:
    s = _str(val)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(val: Any) -> Optional[Decimal]:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return None


def normalize_accounts(raw_accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in raw_accounts:
        acct_id = _str(raw.get("Id"))
        acct_num = _str(raw.get("AcctNum")) or acct_id
        if not acct_num:
            continue
        active = _bool_active(raw.get("Active", True))
        balance = _parse_decimal(raw.get("CurrentBalance"))
        out.append({
            "account_no": acct_num,
            "account_name": _str(raw.get("Name")),
            "display_name": _str(raw.get("FullyQualifiedName") or raw.get("Name")),
            "account_category": _str(raw.get("Classification")),
            "sub_category": _str(raw.get("AccountSubType")),
            "account_type": _str(raw.get("AccountType")),
            "blocked": not active,
            "external_id": acct_id,
            "raw_source_json": {
                **raw,
                "current_balance": str(balance) if balance is not None else None,
                "active": active,
            },
        })
    return out


def normalize_customers(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in raw_rows:
        cust_id = _str(raw.get("Id"))
        if not cust_id:
            continue
        out.append({
            "customer_no": cust_id,
            "display_name": _str(raw.get("DisplayName") or raw.get("FullyQualifiedName")),
            "external_id": cust_id,
            "raw_source_json": {
                **raw,
                "email": _str(raw.get("PrimaryEmailAddr", {}).get("Address")
                              if isinstance(raw.get("PrimaryEmailAddr"), dict) else None),
                "active": _bool_active(raw.get("Active", True)),
            },
        })
    return out


def normalize_vendors(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in raw_rows:
        vend_id = _str(raw.get("Id"))
        if not vend_id:
            continue
        out.append({
            "vendor_no": vend_id,
            "display_name": _str(raw.get("DisplayName") or raw.get("FullyQualifiedName")),
            "external_id": vend_id,
            "raw_source_json": {
                **raw,
                "email": _str(raw.get("PrimaryEmailAddr", {}).get("Address")
                              if isinstance(raw.get("PrimaryEmailAddr"), dict) else None),
                "active": _bool_active(raw.get("Active", True)),
            },
        })
    return out


def _col_value(col_data: list[dict[str, Any]], idx: int) -> Optional[str]:
    if idx < 0 or idx >= len(col_data):
        return None
    cell = col_data[idx]
    if not isinstance(cell, dict):
        return None
    return _str(cell.get("value"))


def _col_id(col_data: list[dict[str, Any]], idx: int) -> Optional[str]:
    if idx < 0 or idx >= len(col_data):
        return None
    cell = col_data[idx]
    if not isinstance(cell, dict):
        return None
    return _str(cell.get("id"))


def _parse_money(s: Optional[str]) -> Optional[Decimal]:
    if not s:
        return None
    cleaned = s.replace(",", "").replace("$", "").strip()
    if not cleaned or cleaned == "-":
        return None
    neg = cleaned.startswith("(") and cleaned.endswith(")")
    if neg:
        cleaned = cleaned[1:-1].strip()
    amt = _parse_decimal(cleaned)
    if amt is None:
        return None
    if neg:
        return -amt
    return amt


def _parse_signed_amount(s: Optional[str]) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """Return (amount, debit, credit) from a single signed amount column."""
    amt = _parse_money(s)
    if amt is None:
        return None, None, None
    if amt >= 0:
        return amt, amt, None
    return amt, None, abs(amt)


def _compute_transaction_amounts(
    debit_s: Optional[str],
    credit_s: Optional[str],
    amount_s: Optional[str],
) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    debit_amount = _parse_money(debit_s)
    credit_amount = _parse_money(credit_s)

    if debit_amount is not None or credit_amount is not None:
        if debit_amount is not None and credit_amount is None:
            return debit_amount, debit_amount, None
        if credit_amount is not None and debit_amount is None:
            return -credit_amount, None, credit_amount
        if debit_amount is not None and credit_amount is not None:
            return debit_amount - credit_amount, debit_amount, credit_amount

    if amount_s:
        return _parse_signed_amount(amount_s)

    return None, None, None


def _entry_key(parts: dict[str, Any]) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _col_key_from_meta(column: dict[str, Any]) -> Optional[str]:
    for meta in column.get("MetaData") or []:
        if isinstance(meta, dict) and meta.get("Name") == "ColKey":
            return _str(meta.get("Value"))
    return None


_GL_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "posting_date": ("transaction date", "date", "tx_date"),
    "account_name": ("distribution account", "account", "account_name"),
    "description": ("description", "memo/description", "memo"),
    "name": ("name",),
    "txn_type": ("transaction type", "txn type"),
    "document_no": ("document number", "doc num", "num", "no."),
    "debit_amount": ("debit",),
    "credit_amount": ("credit",),
    "balance": ("balance",),
    "amount": ("amount",),
    "split": ("split",),
}

_GL_COLKEY_ALIASES: dict[str, tuple[str, ...]] = {
    "posting_date": ("tx_date", "date"),
    "account_name": ("account_name", "account"),
    "description": ("memo", "description"),
    "name": ("name",),
    "txn_type": ("txn_type",),
    "document_no": ("doc_num", "num"),
    "debit_amount": ("debt_home_amt", "debit"),
    "credit_amount": ("credit_home_amt", "credit"),
    "balance": ("rbal_nat_amount", "balance"),
    "amount": ("subt_nat_amount", "subt_nat_home_amount", "amount"),
    "split": ("split_acc", "split"),
}


def _build_gl_column_map(columns: list[dict[str, Any]]) -> tuple[dict[str, int], list[str]]:
    col_titles = [_str(c.get("ColTitle")) or "" for c in columns if isinstance(c, dict)]
    lowered_titles = [t.lower() for t in col_titles]
    col_map: dict[str, int] = {field: -1 for field in _GL_FIELD_ALIASES}

    for field, colkeys in _GL_COLKEY_ALIASES.items():
        for i, col in enumerate(columns):
            if not isinstance(col, dict):
                continue
            # QB reports return ColType directly; MetaData[].ColKey is an older pattern
            ck = (_col_key_from_meta(col) or _str(col.get("ColType")) or "").lower()
            if ck and ck in colkeys:
                col_map[field] = i
                break

    for field, aliases in _GL_FIELD_ALIASES.items():
        if col_map[field] >= 0:
            continue
        for alias in aliases:
            for i, title in enumerate(lowered_titles):
                if alias in title:
                    col_map[field] = i
                    break
            if col_map[field] >= 0:
                break

    return col_map, col_titles


def _parse_section_account(header_col_data: list[Any]) -> tuple[Optional[str], Optional[str]]:
    val = _col_value(header_col_data, 0) if isinstance(header_col_data, list) else None
    if not val:
        return None, None
    match = re.match(r"^(\d+)\s+(.+)$", val)
    if match:
        return match.group(1), val
    return val, val


def _row_list_from_container(rows: Any) -> list[dict[str, Any]]:
    if not rows:
        return []
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    if isinstance(rows, dict):
        inner = rows.get("Row")
        if inner is None:
            return []
        if isinstance(inner, dict):
            return [inner]
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
    return []


def _is_beginning_balance(*texts: Optional[str]) -> bool:
    for text in texts:
        if text and text.strip().lower() == "beginning balance":
            return True
    return False


def _is_total_for(text: Optional[str]) -> bool:
    return bool(text and text.strip().lower().startswith("total for"))


def normalize_general_ledger_report(
    report: dict[str, Any],
    *,
    realm_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Flatten QuickBooks GeneralLedger report rows into GL entry dicts.

    Returns (entries, diagnostics) where diagnostics holds safe row-count metadata.
    """
    columns_raw = report.get("Columns", {}).get("Column", [])
    if isinstance(columns_raw, dict):
        columns = [columns_raw]
    elif isinstance(columns_raw, list):
        columns = [c for c in columns_raw if isinstance(c, dict)]
    else:
        columns = []

    col_map, col_titles = _build_gl_column_map(columns)
    entries: list[dict[str, Any]] = []
    seq = 0

    diagnostics: dict[str, Any] = {
        "top_level_row_count": 0,
        "nested_row_count": 0,
        "data_row_count": 0,
        "skipped_summary_row_count": 0,
        "skipped_beginning_balance_count": 0,
        "records_processed": 0,
        "column_titles": col_titles,
        "column_map": {k: v for k, v in col_map.items() if v >= 0},
    }

    def parse_rows(
        rows_container: Any,
        *,
        section_account_no: Optional[str],
        section_account_name: Optional[str],
        depth: int,
    ) -> None:
        nonlocal seq

        for row in _row_list_from_container(rows_container):
            if depth == 0:
                diagnostics["top_level_row_count"] += 1
            else:
                diagnostics["nested_row_count"] += 1

            row_type = (_str(row.get("type")) or "").lower()
            current_section_no = section_account_no
            current_section_name = section_account_name

            if row_type == "section" or row.get("Header"):
                header = row.get("Header", {})
                header_cols = header.get("ColData", []) if isinstance(header, dict) else []
                if isinstance(header_cols, list) and header_cols:
                    parsed_no, parsed_name = _parse_section_account(header_cols)
                    if parsed_name:
                        current_section_no = parsed_no or current_section_no
                        current_section_name = parsed_name or current_section_name
                nested = row.get("Rows")
                if nested:
                    parse_rows(
                        nested,
                        section_account_no=current_section_no,
                        section_account_name=current_section_name,
                        depth=depth + 1,
                    )
                if row.get("Summary"):
                    diagnostics["skipped_summary_row_count"] += 1
                continue

            if row_type == "summary" or (row.get("Summary") and not row.get("ColData")):
                diagnostics["skipped_summary_row_count"] += 1
                nested = row.get("Rows")
                if nested:
                    parse_rows(
                        nested,
                        section_account_no=current_section_no,
                        section_account_name=current_section_name,
                        depth=depth + 1,
                    )
                continue

            col_data = row.get("ColData")
            if not isinstance(col_data, list) or not col_data:
                nested = row.get("Rows")
                if nested:
                    parse_rows(
                        nested,
                        section_account_no=current_section_no,
                        section_account_name=current_section_name,
                        depth=depth + 1,
                    )
                continue

            diagnostics["data_row_count"] += 1

            idx = col_map
            posting_date = _parse_date(_col_value(col_data, idx["posting_date"]))
            txn_type = _col_value(col_data, idx["txn_type"])
            document_no = _col_value(col_data, idx["document_no"])
            party_name = _col_value(col_data, idx["name"])
            description = _col_value(col_data, idx["description"])
            split_acct = _col_value(col_data, idx["split"])
            distribution_acct = _col_value(col_data, idx["account_name"])
            balance_s = _col_value(col_data, idx["balance"])
            txn_id = _col_id(col_data, idx["txn_type"])

            amount, debit_amount, credit_amount = _compute_transaction_amounts(
                _col_value(col_data, idx["debit_amount"]),
                _col_value(col_data, idx["credit_amount"]),
                _col_value(col_data, idx["amount"]),
            )
            balance_amount = _parse_money(balance_s)

            if _is_beginning_balance(description, txn_type, _col_value(col_data, idx["posting_date"])):
                diagnostics["skipped_beginning_balance_count"] += 1
                continue
            if _is_total_for(description) or _is_total_for(txn_type):
                diagnostics["skipped_summary_row_count"] += 1
                continue

            account_name = distribution_acct or current_section_name or split_acct
            account_no = current_section_no or distribution_acct or split_acct or account_name
            if not account_name and not account_no:
                continue

            if (
                posting_date is None
                and debit_amount is None
                and credit_amount is None
                and amount is None
            ):
                continue

            if not account_no:
                account_no = account_name
            if not account_name:
                account_name = account_no

            seq += 1
            customer_name = party_name if party_name and txn_type and "invoice" in txn_type.lower() else None
            vendor_name = party_name if party_name and txn_type and "bill" in txn_type.lower() else None
            if party_name and not customer_name and not vendor_name:
                customer_name = party_name

            key_parts = {
                "realm_id": realm_id,
                "seq": seq,
                "account_no": account_no,
                "posting_date": posting_date.isoformat() if posting_date else None,
                "document_no": document_no,
                "txn_id": txn_id,
                "amount": str(amount) if amount is not None else None,
            }
            external_entry_no = _entry_key(key_parts)

            raw_payload = {
                "transaction_type": txn_type,
                "transaction_id": txn_id,
                "customer_name": customer_name,
                "vendor_name": vendor_name,
                "name": party_name,
                "split_account": split_acct,
                "distribution_account": distribution_acct,
                "section_account_no": current_section_no,
                "section_account_name": current_section_name,
                "balance": str(balance_amount) if balance_amount is not None else None,
                "col_titles": col_titles,
                "col_data": col_data,
            }

            entries.append({
                "external_entry_no": external_entry_no,
                "external_id": txn_id or document_no or external_entry_no,
                "entry_number": document_no,
                "posting_date": posting_date,
                "document_no": document_no,
                "account_id": account_no,
                "account_no": account_no,
                "debit_amount": debit_amount,
                "credit_amount": credit_amount,
                "amount": amount,
                "description": description or txn_type,
                "raw_source_json": raw_payload,
            })

            nested = row.get("Rows")
            if nested:
                parse_rows(
                    nested,
                    section_account_no=current_section_no,
                    section_account_name=current_section_name,
                    depth=depth + 1,
                )

    parse_rows(report.get("Rows"), section_account_no=None, section_account_name=None, depth=0)
    diagnostics["records_processed"] = len(entries)
    return entries, diagnostics
