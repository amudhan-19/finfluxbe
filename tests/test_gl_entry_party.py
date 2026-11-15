# @file    tests/test_gl_entry_party.py

from __future__ import annotations

from types import SimpleNamespace

from app.services.flux_ai.gl_entry_party import (
    GlEntryPartyRef,
    build_top_party_summaries,
    enrich_party_ref,
    largest_party_link_line,
    resolve_gl_entry_party,
)


def _entry(**raw_fields) -> SimpleNamespace:
    cols = {
        "account_no": "6110",
        "amount": 45000,
        "posting_date": None,
        "document_no": "INV-1021",
        "description": "Sales",
        "source_type": None,
        "source_number": None,
        "bal_account_type": None,
        "bal_account_number": None,
        "raw_source_json": raw_fields,
    }
    return SimpleNamespace(**cols)


def test_resolve_party_from_balancing_account_fields() -> None:
    entry = _entry(
        balAccountType="Customer",
        balAccountNumber="C10010",
    )
    ref = resolve_gl_entry_party(entry)
    assert ref is not None
    assert ref.party_type == "customer"
    assert ref.party_no == "C10010"


def test_resolve_party_from_source_type() -> None:
    entry = _entry(sourceType="Vendor", sourceNumber="V20020")
    ref = resolve_gl_entry_party(entry)
    assert ref is not None
    assert ref.party_type == "vendor"
    assert ref.party_no == "V20020"


def test_resolve_party_source_priority_over_bal() -> None:
    entry = _entry(
        sourceType="Vendor",
        sourceNumber="20000",
        balAccountType="Customer",
        balAccountNumber="C10010",
    )
    ref = resolve_gl_entry_party(entry)
    assert ref is not None
    assert ref.party_type == "vendor"
    assert ref.party_no == "20000"


def test_resolve_party_from_stored_columns() -> None:
    entry = _entry()
    entry.source_type = "Vendor"
    entry.source_number = "20000"
    ref = resolve_gl_entry_party(entry)
    assert ref is not None
    assert ref.party_no == "20000"


def test_resolve_party_pascal_case_raw_keys() -> None:
    entry = _entry(BalAccountType="Customer", BalAccountNo="C10010")
    ref = resolve_gl_entry_party(entry)
    assert ref is not None
    assert ref.party_no == "C10010"


def test_enrich_party_with_master_name() -> None:
    ref = GlEntryPartyRef(party_type="customer", party_no="C10010")
    enriched = enrich_party_ref(
        ref,
        customer_names={"C10010": "ABC Traders"},
        vendor_names={},
    )
    assert enriched.party_name == "ABC Traders"
    assert "ABC Traders" in enriched.display_label()


def test_largest_party_link_line_with_name() -> None:
    party = GlEntryPartyRef(party_type="vendor", party_no="20000", party_name="ABC Suppliers")
    line = largest_party_link_line(party)
    assert "Vendor 20000 — ABC Suppliers" in line
    assert "source transaction details" in line


def test_largest_party_link_line_without_name_master_synced() -> None:
    party = GlEntryPartyRef(party_type="vendor", party_no="20000")
    line = largest_party_link_line(party, master_synced=True)
    assert "Vendor 20000" in line
    assert "not available in the synced master data" in line


def test_build_top_party_summaries() -> None:
    entries = [
        _entry(
            sourceType="Customer",
            sourceNumber="C10010",
        ),
        _entry(
            sourceType="Customer",
            sourceNumber="C10010",
        ),
        _entry(
            sourceType="Vendor",
            sourceNumber="V20020",
        ),
    ]
    entries[0].amount = 30000
    entries[1].amount = 15000
    entries[2].amount = -5000

    summaries = build_top_party_summaries(
        entries,
        customer_names={"C10010": "ABC Traders"},
        vendor_names={"V20020": "XYZ Suppliers"},
    )
    assert len(summaries) == 2
    assert summaries[0].party_no == "C10010"
    assert summaries[0].transaction_count == 2
    assert summaries[0].party_name == "ABC Traders"
