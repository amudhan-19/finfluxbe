# @file    tests/test_flux_bs_subledger.py
#
# Balance Sheet uses cumulative balance-at-date; optional sub-ledger filter.

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.models import (
    Entity,
    ErpGeneralLedgerEntry,
    ErpGlAccount,
    FluxData,
    OntologyMapping,
    Threshold,
)
from app.services.flux_generation import preview_flux_generation
from app.services.subledger_catalog import (
    calculation_mode_for_statement,
    entry_matches_subledger,
    entry_subledger_tokens,
)


_TEST_ENTITY = "ENT-FLUX-BS-1"
_PERIOD_FROM = "May 2026"
_PERIOD_TO = "May 2025"


@pytest.fixture()
def db_session() -> Session:
    db = SessionLocal()
    try:
        if not db.query(Entity).filter(Entity.id == _TEST_ENTITY).first():
            db.add(Entity(
                id=_TEST_ENTITY, name="Flux BS Test",
                region="North America", supervisor_id=1,
                period_type="YoY",
                period_from=_PERIOD_FROM, period_to=_PERIOD_TO,
            ))
        db.query(FluxData).filter(FluxData.entity_id == _TEST_ENTITY).delete()
        db.query(OntologyMapping).filter(
            OntologyMapping.entity_id == _TEST_ENTITY,
        ).delete()
        db.query(ErpGeneralLedgerEntry).filter(
            ErpGeneralLedgerEntry.entity_id == _TEST_ENTITY,
        ).delete()
        db.query(ErpGlAccount).filter(
            ErpGlAccount.entity_id == _TEST_ENTITY,
        ).delete()
        db.query(Threshold).filter(Threshold.entity_id == _TEST_ENTITY).delete()
        db.add(Threshold(
            entity_id=_TEST_ENTITY, statement="bs",
            mode="both", pct_value=Decimal("10"),
            abs_value=Decimal("100000"), logic="or",
        ))
        db.add(ErpGlAccount(
            entity_id=_TEST_ENTITY, erp_type="bc", company_id="C1",
            account_no="1000", account_name="Cash",
        ))
        # Prior-year balance + in-period movement for current year.
        db.add(ErpGeneralLedgerEntry(
            entity_id=_TEST_ENTITY, erp_type="bc", company_id="C1",
            external_entry_no="BS1",
            posting_date=date(2025, 1, 10),
            account_no="1000", amount=Decimal("100000"),
            description="Opening",
        ))
        db.add(ErpGeneralLedgerEntry(
            entity_id=_TEST_ENTITY, erp_type="bc", company_id="C1",
            external_entry_no="BS2",
            posting_date=date(2026, 3, 5),
            account_no="1000", amount=Decimal("50000"),
            description="Deposit",
            raw_source_json={
                "sourceType": "Customer", "sourceNumber": "C-001",
                "sourceName": "Acme",
            },
        ))
        db.add(ErpGeneralLedgerEntry(
            entity_id=_TEST_ENTITY, erp_type="bc", company_id="C1",
            external_entry_no="BS3",
            posting_date=date(2026, 4, 1),
            account_no="1000", amount=Decimal("25000"),
            description="Other deposit",
            raw_source_json={
                "sourceType": "Customer", "sourceNumber": "C-002",
                "sourceName": "Beta",
            },
        ))
        db.add(OntologyMapping(
            entity_id=_TEST_ENTITY, statement="bs",
            tflux_caption="Cash", erp_gl_json=["1000"],
        ))
        db.commit()
        yield db
    finally:
        db.close()


def test_calculation_mode_for_bs() -> None:
    assert calculation_mode_for_statement("bs") == "balance_at_date"
    assert calculation_mode_for_statement("pl") == "net_change"


def test_bs_preview_uses_cumulative_balance(db_session: Session) -> None:
    entity = db_session.query(Entity).filter(Entity.id == _TEST_ENTITY).first()
    rows, _ = preview_flux_generation(db_session, entity, "bs")
    cash = next(r for r in rows if r.caption == "Cash")
    # Current (through May 2026): 100k + 50k + 25k
    assert cash.current_value == Decimal("175000.00")
    # Comparison (through May 2025): opening 100k only
    assert cash.comp_value == Decimal("100000.00")
    # Count is current-period matches + comparison-period matches (3 + 1).
    assert cash.matched_transaction_count == 4


def test_subledger_filter_limits_transactions(db_session: Session) -> None:
    entity = db_session.query(Entity).filter(Entity.id == _TEST_ENTITY).first()
    mapping = db_session.query(OntologyMapping).filter(
        OntologyMapping.entity_id == _TEST_ENTITY,
        OntologyMapping.tflux_caption == "Cash",
    ).first()
    mapping.erp_subledger_json = ["customer:C-001"]
    db_session.commit()

    rows, _ = preview_flux_generation(db_session, entity, "bs")
    cash = next(r for r in rows if r.caption == "Cash")
    assert cash.current_value == Decimal("50000.00")
    assert cash.comp_value == Decimal("0.00")
    assert cash.matched_transaction_count == 1
    assert "customer:C-001" in (cash.selected_subledger or [])


def test_entry_subledger_tokens_from_raw() -> None:
    entry = ErpGeneralLedgerEntry(
        entity_id=_TEST_ENTITY, erp_type="bc", company_id="C1",
        external_entry_no="T1",
        posting_date=date(2026, 1, 1),
        account_no="1000", amount=Decimal("1"),
        document_no="INV-9",
        description="Invoice",
        raw_source_json={
            "sourceType": "Customer", "sourceNumber": "C-001",
            "sourceName": "Acme",
        },
    )
    tokens = entry_subledger_tokens(entry)
    assert "customer:C-001" in tokens
    assert "document:INV-9" not in tokens
    assert entry_matches_subledger(entry, {"customer:C-001"})
    assert not entry_matches_subledger(entry, {"customer:C-999"})
