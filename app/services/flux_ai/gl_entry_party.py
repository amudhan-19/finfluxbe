# @file    services/flux_ai/gl_entry_party.py
#
# Resolve customer/vendor from BC GL entry source/balancing fields (read-only).

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import ErpCustomer, ErpGeneralLedgerEntry, ErpVendor


@dataclass(frozen=True)
class GlEntryPartyRef:
    party_type: str  # "customer" | "vendor"
    party_no: str
    party_name: str | None = None

    def display_label(self) -> str:
        prefix = "Customer" if self.party_type == "customer" else "Vendor"
        if self.party_name:
            return f"{prefix} {self.party_no} — {self.party_name}"
        return f"{prefix} {self.party_no}"


@dataclass
class PartySummaryEvidence:
    party_type: str
    party_no: str
    party_name: str
    transaction_count: int
    total_amount: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "party_type": self.party_type,
            "party_no": self.party_no,
            "party_name": self.party_name,
            "transaction_count": self.transaction_count,
            "total_amount": self.total_amount,
        }


def _str_val(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _raw_dict(entry: ErpGeneralLedgerEntry) -> dict[str, Any]:
    raw = entry.raw_source_json
    return raw if isinstance(raw, dict) else {}


def _pick_raw(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key in raw:
            val = _str_val(raw.get(key))
            if val:
                return val
    return None


def _entry_str(entry: ErpGeneralLedgerEntry, attr: str) -> str | None:
    return _str_val(getattr(entry, attr, None))


def _normalize_party_type(value: str | None) -> str | None:
    if not value:
        return None
    lower = value.lower().strip()
    if "customer" in lower:
        return "customer"
    if "vendor" in lower:
        return "vendor"
    return None


def resolve_gl_entry_party(entry: ErpGeneralLedgerEntry) -> GlEntryPartyRef | None:
    """Priority: sourceType/sourceNumber, then bal Customer/Vendor fields."""
    raw = _raw_dict(entry)

    source_type = _normalize_party_type(
        _entry_str(entry, "source_type")
        or _pick_raw(raw, "sourceType", "SourceType", "source_type"),
    )
    source_no = (
        _entry_str(entry, "source_number")
        or _pick_raw(raw, "sourceNumber", "sourceNo", "SourceNo", "source_no")
    )
    if source_type and source_no:
        return GlEntryPartyRef(party_type=source_type, party_no=source_no)

    bal_type = _normalize_party_type(
        _entry_str(entry, "bal_account_type")
        or _pick_raw(
            raw,
            "balAccountType",
            "BalAccountType",
            "balancingAccountType",
            "balancing_account_type",
        ),
    )
    bal_no = (
        _entry_str(entry, "bal_account_number")
        or _pick_raw(
            raw,
            "balAccountNumber",
            "BalAccountNo",
            "balAccountNo",
            "balancingAccountNumber",
            "balancingAccountNo",
        )
    )
    if bal_type and bal_no:
        return GlEntryPartyRef(party_type=bal_type, party_no=bal_no)

    return None


def load_party_name_maps(
    db: Session, entity_id: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """customer_no/vendor_no -> display_name from synced master tables."""
    cust_rows = (
        db.query(ErpCustomer.customer_no, ErpCustomer.display_name)
        .filter(ErpCustomer.entity_id == entity_id)
        .all()
    )
    vend_rows = (
        db.query(ErpVendor.vendor_no, ErpVendor.display_name)
        .filter(ErpVendor.entity_id == entity_id)
        .all()
    )
    customers = {
        str(no): (display or str(no)).strip()
        for no, display in cust_rows if no
    }
    vendors = {
        str(no): (display or str(no)).strip()
        for no, display in vend_rows if no
    }
    return customers, vendors


def enrich_party_ref(
    ref: GlEntryPartyRef,
    *,
    customer_names: dict[str, str],
    vendor_names: dict[str, str],
) -> GlEntryPartyRef:
    if ref.party_type == "customer":
        name = customer_names.get(ref.party_no)
    else:
        name = vendor_names.get(ref.party_no)
    if name and name != ref.party_no:
        return GlEntryPartyRef(
            party_type=ref.party_type,
            party_no=ref.party_no,
            party_name=name,
        )
    return ref


def build_top_party_summaries(
    entries: list[ErpGeneralLedgerEntry],
    *,
    customer_names: dict[str, str],
    vendor_names: dict[str, str],
    limit: int = 5,
) -> list[PartySummaryEvidence]:
    """Aggregate party-linked GL rows by party; rank by abs(total_amount)."""
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        ref = resolve_gl_entry_party(entry)
        if ref is None:
            continue
        ref = enrich_party_ref(
            ref, customer_names=customer_names, vendor_names=vendor_names,
        )
        key = (ref.party_type, ref.party_no)
        bucket = totals.setdefault(key, {
            "party_type": ref.party_type,
            "party_no": ref.party_no,
            "party_name": ref.party_name or ref.party_no,
            "transaction_count": 0,
            "total_amount": Decimal("0"),
        })
        bucket["transaction_count"] += 1
        if entry.amount is not None:
            bucket["total_amount"] += Decimal(str(entry.amount))

    ranked: list[PartySummaryEvidence] = []
    for bucket in totals.values():
        ranked.append(PartySummaryEvidence(
            party_type=bucket["party_type"],
            party_no=bucket["party_no"],
            party_name=bucket["party_name"],
            transaction_count=bucket["transaction_count"],
            total_amount=float(bucket["total_amount"]),
        ))
    ranked.sort(key=lambda p: abs(p.total_amount), reverse=True)
    return ranked[:limit]


def largest_party_link_line(
    party: GlEntryPartyRef | None,
    *,
    master_synced: bool = False,
) -> str:
    if party is None:
        return ""
    prefix = "Customer" if party.party_type == "customer" else "Vendor"
    if party.party_name:
        return (
            f"The largest matched entries are linked to {prefix} {party.party_no} — "
            f"{party.party_name}, based on Business Central source transaction details."
        )
    if master_synced:
        return (
            f"The largest matched entries are linked to {prefix} {party.party_no}. "
            f"{prefix} name is not available in the synced master data."
        )
    return (
        f"The largest matched entries are linked to {prefix} {party.party_no}, "
        f"based on Business Central source transaction details."
    )
