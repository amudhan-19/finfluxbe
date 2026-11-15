# @file    services/flux_ai/party_context.py
#
# Customer/vendor master vs ledger context for Flux AI commentary.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import (
    ErpCustomer,
    ErpCustomerLedgerEntry,
    ErpGeneralLedgerEntry,
    ErpSyncRun,
    ErpVendor,
    ErpVendorLedgerEntry,
    OntologyMapping,
    SYNC_CUSTOMER_LEDGER_ENTRIES,
    SYNC_VENDOR_LEDGER_ENTRIES,
)
from app.services.subledger_catalog import (
    entry_matches_subledger,
    entry_subledger_tokens,
    ledger_endpoints_unavailable,
)

_STALE_LEDGER_PHRASES = (
    "customer/vendor ledger-level detail is not available",
    "customer/vendor-level detail is not available",
    "customer-level detail is not available",
)


@dataclass
class PartyAiContext:
    selected_customers: list[str] = field(default_factory=list)
    selected_vendors: list[str] = field(default_factory=list)
    customer_vendor_master_available: bool = False
    customer_vendor_ledger_available: bool = False
    ledger_endpoints_unavailable: bool = False
    transaction_level_party_evidence: bool = False
    customers_synced_count: int = 0
    vendors_synced_count: int = 0
    matched_transaction_count: int = 0
    wording_note: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _split_party_labels(labels: list[str]) -> tuple[list[str], list[str]]:
    customers: list[str] = []
    vendors: list[str] = []
    for lbl in labels:
        if lbl.startswith("Customer:"):
            customers.append(lbl)
        elif lbl.startswith("Vendor:"):
            vendors.append(lbl)
    return customers, vendors


def _count_master_synced(db: Session, entity_id: str) -> tuple[int, int]:
    cust = db.query(ErpCustomer).filter(ErpCustomer.entity_id == entity_id).count()
    vend = db.query(ErpVendor).filter(ErpVendor.entity_id == entity_id).count()
    return cust, vend


def _ledger_data_present(db: Session, entity_id: str) -> bool:
    cust = db.query(ErpCustomerLedgerEntry).filter(
        ErpCustomerLedgerEntry.entity_id == entity_id,
    ).limit(1).first()
    if cust:
        return True
    vend = db.query(ErpVendorLedgerEntry).filter(
        ErpVendorLedgerEntry.entity_id == entity_id,
    ).limit(1).first()
    return vend is not None


def _last_ledger_success(db: Session, entity_id: str, sync_type: str) -> ErpSyncRun | None:
    return (
        db.query(ErpSyncRun)
        .filter(
            ErpSyncRun.entity_id == entity_id,
            ErpSyncRun.sync_type == sync_type,
            ErpSyncRun.status == "success",
        )
        .order_by(ErpSyncRun.started_at.desc())
        .first()
    )


def customer_vendor_ledger_available(db: Session, entity_id: str) -> bool:
    """True when ledger rows exist and endpoints were not marked unavailable."""
    if ledger_endpoints_unavailable(db, entity_id):
        return False
    if _ledger_data_present(db, entity_id):
        return True
    for stype in (SYNC_CUSTOMER_LEDGER_ENTRIES, SYNC_VENDOR_LEDGER_ENTRIES):
        run = _last_ledger_success(db, entity_id, stype)
        if run and (run.records_processed or 0) > 0:
            return True
    return False


def _count_matched_transactions(
    db: Session,
    entity_id: str,
    account_nos: list[str],
    selected_tokens: set[str],
    *,
    limit: int = 500,
) -> tuple[int, bool]:
    """Count GL rows for mapped accounts; detect party refs on matched rows."""
    if not account_nos:
        return 0, False
    rows = (
        db.query(ErpGeneralLedgerEntry)
        .filter(
            ErpGeneralLedgerEntry.entity_id == entity_id,
            ErpGeneralLedgerEntry.account_no.in_(account_nos),
        )
        .limit(limit)
        .all()
    )
    matched = 0
    party_on_match = False
    for row in rows:
        if selected_tokens and not entry_matches_subledger(row, selected_tokens):
            continue
        matched += 1
        if entry_subledger_tokens(row):
            party_on_match = True
    if not selected_tokens:
        party_on_match = any(entry_subledger_tokens(r) for r in rows)
    return matched, party_on_match


_MSG_GL_SOURCE_DERIVED = (
    "Customer/vendor context is derived from Business Central GL entry "
    "source transaction fields (sourceType/sourceNumber) where available."
)
_MSG_NO_GL_PARTY = (
    "Customer/vendor attribution is not available for these GL entries. "
    "Review is based on mapped GL accounts and document-level transaction "
    "details."
)
_MSG_BAL_FALLBACK = (
    "When sourceType/sourceNumber is not available, balancing-account "
    "Customer/Vendor fields may be used where present."
)
_MSG_SELECTED_MASTER = (
    "Selected customer/vendor context is available from Business Central "
    "master data."
)


def build_attribution_coverage_lines(
    party_ctx: PartyAiContext,
    evidence: Any | None = None,
) -> list[str]:
    """Attribution coverage section — exact ERP-capability messaging."""
    lines: list[str] = []
    if evidence is not None:
        attr = getattr(evidence, "attribution_coverage", None)
        if attr is not None:
            if attr.transaction_party_link_available:
                largest = getattr(evidence, "largest_party_line", "") or ""
                if largest:
                    lines.append(largest)
                else:
                    for party in getattr(evidence, "top_parties", [])[:2]:
                        label = party.party_name or party.party_no
                        ptype = party.party_type.capitalize()
                        lines.append(
                            f"The largest matched entries are linked to "
                            f"{ptype} {party.party_no} — {label}, based on "
                            f"Business Central source transaction details.",
                        )
                if not lines:
                    lines.append(
                        "Transaction-level customer/vendor linkage is available "
                        "in the synced ERP payload.",
                    )
                if not attr.poster_source_available:
                    lines.append(
                        "Poster/source details are not exposed in the current ERP payload.",
                    )
                return lines
            if attr.customer_vendor_master_synced:
                lines.append(
                    "Customer/vendor master data is synced, but transaction-level "
                    "sourceType/sourceNumber linkage is not available for these "
                    "GL entries. Review is based on mapped GL accounts and "
                    "document-level transaction details.",
                )
                return lines
            lines.append(
                "Customer/vendor attribution is not available for these GL entries. "
                "Review is based on mapped GL accounts and document-level "
                "transaction details.",
            )
            return lines

    if party_ctx.customer_vendor_master_available:
        lines.append(
            "Customer/vendor master data is synced, but transaction-level "
            "linkage is not exposed in the current ERP payload. Attribution "
            "is therefore limited to mapped GL accounts and document-level "
            "transaction evidence.",
        )
    else:
        lines.append(
            "Customer/vendor-level detail is not available in the current "
            "synced ERP payload.",
        )
    if not party_ctx.transaction_level_party_evidence:
        lines.append(
            "Poster/source details are not exposed in the current ERP payload.",
        )
    return lines


def build_customer_vendor_context_lines_from_evidence(
    evidence: Any | None,
    party_ctx: PartyAiContext | None = None,
) -> list[str]:
    """Customer / Vendor context from GL source transaction fields."""
    from app.services.ai_text_clean import format_currency

    lines: list[str] = []
    has_gl_party = bool(
        evidence is not None and getattr(evidence, "has_gl_party_links", False),
    )

    if has_gl_party:
        lines.append(_MSG_GL_SOURCE_DERIVED)
        largest_line = getattr(evidence, "largest_party_line", "") or ""
        if largest_line:
            lines.append(largest_line)
        top_parties = getattr(evidence, "top_parties", None) or []
        for party in top_parties[:3]:
            label = party.party_name or party.party_no
            ptype = party.party_type.capitalize()
            lines.append(
                f"{ptype} {party.party_no} — {label}: "
                f"{party.transaction_count} matched entries associated with "
                f"{format_currency(party.total_amount)} total amount "
                "(document-level link only; not a variance driver claim).",
            )
        lines.append(_MSG_BAL_FALLBACK)
        return lines

    if party_ctx and (party_ctx.selected_customers or party_ctx.selected_vendors):
        lines.append(_MSG_SELECTED_MASTER)
        lines.extend(party_ctx.selected_customers)
        lines.extend(party_ctx.selected_vendors)
        return lines

    lines.append(_MSG_NO_GL_PARTY)
    return lines


def build_customer_vendor_context_lines(ctx: PartyAiContext) -> list[str]:
    """Legacy entry point when evidence is not available."""
    return build_customer_vendor_context_lines_from_evidence(None, ctx)


def build_customer_vendor_notes(ctx: PartyAiContext) -> list[str]:
    return build_customer_vendor_context_lines(ctx)


def build_party_ai_context(
    db: Session,
    entity_id: str,
    mapping: OntologyMapping | None,
    account_nos: list[str],
) -> PartyAiContext:
    labels: list[str] = []
    selected_tokens: set[str] = set()

    customers, vendors = _split_party_labels(labels)
    cust_count, vend_count = _count_master_synced(db, entity_id)
    master_available = (cust_count + vend_count) > 0
    ledger_unavail = ledger_endpoints_unavailable(db, entity_id)
    ledger_available = customer_vendor_ledger_available(db, entity_id)
    matched_count, txn_evidence = _count_matched_transactions(
        db, entity_id, account_nos, selected_tokens,
    )
    return PartyAiContext(
        selected_customers=customers,
        selected_vendors=vendors,
        customer_vendor_master_available=master_available,
        customer_vendor_ledger_available=ledger_available,
        ledger_endpoints_unavailable=ledger_unavail,
        transaction_level_party_evidence=txn_evidence,
        customers_synced_count=cust_count,
        vendors_synced_count=vend_count,
        matched_transaction_count=matched_count,
        wording_note="",
        diagnostics={
            "selectedCustomersCount": len(customers),
            "selectedVendorsCount": len(vendors),
            "customersSyncedCount": cust_count,
            "vendorsSyncedCount": vend_count,
            "ledgerLevelCustomerVendorAvailable": ledger_available,
            "customerVendorMasterAvailable": master_available,
            "transactionLevelPartyEvidence": txn_evidence,
            "matchedTransactionsCount": matched_count,
        },
    )


def is_stale_ledger_unavailable_line(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in _STALE_LEDGER_PHRASES)
