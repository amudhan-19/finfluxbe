# @file    tests/test_flux_generation_or_match.py
#
# Targeted regression tests for the OR-matching layer added to
# `flux_generation.py`. The bug fixed here: when a BC environment returns
# GL entries without `accountNumber`, only `account_id` (a GUID) is
# stored. The legacy matcher filtered on `account_no` only and produced
# zero matches even though the ontology mapping was correct.

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
    OntologyMapping,
    Threshold,
)
from app.services.flux_generation import (
    GlAccountIdentitySet,
    match_transactions_for_caption,
    preview_flux_generation,
    resolve_gl_account_identity,
)


_ENT = "ENT-OR-MATCH-1"


@pytest.fixture()
def db_session() -> Session:
    db = SessionLocal()
    try:
        if not db.query(Entity).filter(Entity.id == _ENT).first():
            db.add(Entity(
                id=_ENT, name="OR Match Test",
                region="North America", supervisor_id=1,
                period_type="MoM",
                period_from="Mar 2026", period_to="Feb 2026",
            ))
        db.add(Threshold(
            entity_id=_ENT, statement="pl",
            mode="both", pct_value=Decimal("10"),
            abs_value=Decimal("100000"), logic="or",
        ))
        db.commit()
        yield db
    finally:
        db.rollback()
        db.query(OntologyMapping).filter(
            OntologyMapping.entity_id == _ENT,
        ).delete()
        db.query(ErpGeneralLedgerEntry).filter(
            ErpGeneralLedgerEntry.entity_id == _ENT,
        ).delete()
        db.query(ErpGlAccount).filter(
            ErpGlAccount.entity_id == _ENT,
        ).delete()
        db.query(Threshold).filter(Threshold.entity_id == _ENT).delete()
        db.query(Entity).filter(Entity.id == _ENT).delete()
        db.commit()
        db.close()


# ── Identity resolver ─────────────────────────────────────────────────
def test_resolve_widens_codes_to_account_id_and_external_id(
    db_session: Session,
) -> None:
    db_session.add(ErpGlAccount(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        account_no="4000", account_name="Sales",
        external_id="bc-acct-guid-4000",
    ))
    db_session.commit()

    identity = resolve_gl_account_identity(db_session, _ENT, {"4000"})
    assert "4000" in identity.account_nos
    assert "bc-acct-guid-4000" in identity.external_ids
    assert "bc-acct-guid-4000" in identity.account_ids


def test_resolve_handles_external_id_supplied_in_mapping(
    db_session: Session,
) -> None:
    db_session.add(ErpGlAccount(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        account_no="4000", account_name="Sales",
        external_id="bc-acct-guid-4000",
    ))
    db_session.commit()

    # Supervisor saved the external_id (rare but supported).
    identity = resolve_gl_account_identity(
        db_session, _ENT, {"bc-acct-guid-4000"},
    )
    assert "4000" in identity.account_nos
    assert "bc-acct-guid-4000" in identity.external_ids


def test_resolve_returns_empty_for_no_codes(db_session: Session) -> None:
    identity = resolve_gl_account_identity(db_session, _ENT, set())
    assert identity.is_empty


# ── match_transactions_for_caption with OR-match ──────────────────────
def test_match_finds_entries_when_only_account_id_is_populated(
    db_session: Session,
) -> None:
    """The reproduction of the production bug: GL entries from a BC env
    with no `accountNumber` carry only `account_id`. The matcher must
    still find them via the catalog's external_id."""
    db_session.add(ErpGlAccount(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        account_no="4000", account_name="Sales",
        external_id="bc-acct-guid-4000",
    ))
    db_session.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E1", posting_date=date(2026, 3, 5),
        account_no=None,  # missing
        account_id="bc-acct-guid-4000",
        amount=Decimal("500000"),
    ))
    db_session.commit()

    identity = resolve_gl_account_identity(db_session, _ENT, {"4000"})
    rows = match_transactions_for_caption(
        db_session, _ENT, identity,
        date(2026, 3, 1), date(2026, 3, 31),
    )
    assert len(rows) == 1
    assert rows[0].external_entry_no == "E1"


def test_match_still_finds_legacy_account_no_only(db_session: Session) -> None:
    """Plain `set[str]` callers (legacy) keep working."""
    db_session.add(ErpGlAccount(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        account_no="4000", account_name="Sales",
    ))
    db_session.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E1", posting_date=date(2026, 3, 5),
        account_no="4000", amount=Decimal("250000"),
    ))
    db_session.commit()

    rows = match_transactions_for_caption(
        db_session, _ENT, {"4000"},
        date(2026, 3, 1), date(2026, 3, 31),
    )
    assert len(rows) == 1


# ── Full preview path ────────────────────────────────────────────────
def test_preview_handles_account_id_only_entries_end_to_end(
    db_session: Session,
) -> None:
    db_session.add(ErpGlAccount(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        account_no="4000", account_name="Sales",
        external_id="bc-acct-guid-4000",
    ))
    # Current period — March 2026 → 600,000 via account_id only.
    db_session.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E1", posting_date=date(2026, 3, 5),
        account_no=None, account_id="bc-acct-guid-4000",
        amount=Decimal("600000"),
    ))
    # Comparison period — February 2026 → 400,000 also account_id only.
    db_session.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E2", posting_date=date(2026, 2, 12),
        account_no=None, account_id="bc-acct-guid-4000",
        amount=Decimal("400000"),
    ))
    db_session.add(OntologyMapping(
        entity_id=_ENT, statement="pl",
        tflux_caption="Revenue",
        erp_gl_json=["4000"], erp_subgl_json=[],
    ))
    db_session.commit()

    entity = db_session.query(Entity).filter(Entity.id == _ENT).first()
    rows, diag = preview_flux_generation(db_session, entity, "pl")

    assert len(rows) == 1
    rev = rows[0]
    assert rev.caption == "Revenue"
    assert rev.current_value == Decimal("600000.00")
    assert rev.comp_value == Decimal("400000.00")
    assert diag.captions_with_mappings == 1
    assert diag.matched_transaction_total == 2
    assert diag.current_transactions_matched == 1
    assert diag.comparison_transactions_matched == 1


def test_diagnostics_split_current_vs_comparison_when_one_side_empty(
    db_session: Session,
) -> None:
    db_session.add(ErpGlAccount(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        account_no="4000", account_name="Sales",
    ))
    # Only a current-period entry.
    db_session.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E1", posting_date=date(2026, 3, 5),
        account_no="4000", amount=Decimal("500000"),
    ))
    db_session.add(OntologyMapping(
        entity_id=_ENT, statement="pl",
        tflux_caption="Revenue",
        erp_gl_json=["4000"], erp_subgl_json=[],
    ))
    db_session.commit()

    entity = db_session.query(Entity).filter(Entity.id == _ENT).first()
    _, diag = preview_flux_generation(db_session, entity, "pl")

    assert diag.current_transactions_matched == 1
    assert diag.comparison_transactions_matched == 0
    assert "Revenue" not in diag.captions_with_zero_transactions  # has current data
