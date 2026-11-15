# @file    tests/test_party_context.py

from __future__ import annotations

from app.services.flux_ai.flux_ai_evidence import FluxAiEvidence
from app.services.flux_ai.gl_entry_party import PartySummaryEvidence
from app.services.flux_ai.party_context import (
    build_customer_vendor_context_lines_from_evidence,
    build_customer_vendor_notes,
    is_stale_ledger_unavailable_line,
    PartyAiContext,
)


def test_stale_ledger_line_detection() -> None:
    assert is_stale_ledger_unavailable_line(
        "Customer/vendor ledger-level detail is not available in the API",
    )
    assert not is_stale_ledger_unavailable_line(
        "Selected customer/vendor context is available from master data",
    )


def test_customer_vendor_notes_with_selection() -> None:
    ctx = PartyAiContext(
        selected_customers=["Customer: C10010 — ABC Traders"],
        selected_vendors=["Vendor: V20020 — XYZ Suppliers"],
        customer_vendor_master_available=True,
        customer_vendor_ledger_available=False,
    )
    notes = build_customer_vendor_notes(ctx)
    assert any("Selected customer/vendor context" in n for n in notes)
    assert "Customer: C10010" in " ".join(notes)


def test_customer_vendor_notes_none_selected_master_synced() -> None:
    ctx = PartyAiContext(
        customer_vendor_master_available=True,
    )
    notes = build_customer_vendor_notes(ctx)
    assert any("not available for these GL entries" in n for n in notes)


def test_context_from_gl_balancing_fields() -> None:
    evidence = FluxAiEvidence(
        calculation_mode="net_change",
        calculation_mode_label="Net Change",
        current_period_label="Jan-Dec 2025",
        comparison_period_label="Jan-Dec 2024",
        has_gl_party_links=True,
        largest_party_line=(
            "The largest matched GL entries are linked to "
            "Customer C10010 — ABC Traders."
        ),
        top_parties=[
            PartySummaryEvidence(
                party_type="customer",
                party_no="C10010",
                party_name="ABC Traders",
                transaction_count=5,
                total_amount=45000.0,
            ),
        ],
    )
    lines = build_customer_vendor_context_lines_from_evidence(evidence)
    assert any("sourceType/sourceNumber" in ln for ln in lines)
    assert any("balancing-account" in ln for ln in lines)
    assert any("C10010" in ln for ln in lines)
    assert any("largest matched GL entries" in ln for ln in lines)
