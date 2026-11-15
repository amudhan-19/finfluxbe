# @file    tests/test_ontology_automap.py
#
# Unit tests for the demo ontology auto-mapper. Uses the in-memory SQLite
# engine bound by `tests/conftest.py`. Each test seeds its own GL accounts
# under a dedicated entity ID so the suite stays parallel-safe.

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.models import Entity, ErpDimensionValue, ErpGlAccount, OntologyMapping
from app.services.ontology_automap import (
    auto_map_ontology_for_demo,
    find_matching_dimension_values,
    find_matching_gl_accounts,
)


_TEST_ENTITY_ID = "ENT-AUTOMAP-1"


def _seed_entity(db: Session) -> None:
    if not db.query(Entity).filter(Entity.id == _TEST_ENTITY_ID).first():
        admin_id = 1  # the conftest admin always has id=1 in fresh test DBs
        db.add(Entity(
            id=_TEST_ENTITY_ID, name="Auto-map Test",
            region="North America", supervisor_id=admin_id,
            period_from="2025-01-01", period_to="2025-12-31",
        ))


def _seed_gl_accounts(db: Session, accounts: list[tuple[str, str]]) -> None:
    for account_no, account_name in accounts:
        db.add(ErpGlAccount(
            entity_id=_TEST_ENTITY_ID, erp_type="bc",
            company_id="COMP-1",
            account_no=account_no, account_name=account_name,
        ))


def _seed_dimension(db: Session, dim_code: str, dim_name: str,
                    value_code: str, value_name: str) -> None:
    db.add(ErpDimensionValue(
        entity_id=_TEST_ENTITY_ID, erp_type="bc", company_id="COMP-1",
        dimension_code=dim_code, dimension_name=dim_name,
        value_code=value_code, value_name=value_name,
    ))


@pytest.fixture()
def db_session() -> Session:
    db = SessionLocal()
    try:
        _seed_entity(db)
        db.commit()
        yield db
    finally:
        # Clean test fixtures so back-to-back runs are deterministic.
        db.query(OntologyMapping).filter(
            OntologyMapping.entity_id == _TEST_ENTITY_ID,
        ).delete()
        db.query(ErpGlAccount).filter(
            ErpGlAccount.entity_id == _TEST_ENTITY_ID,
        ).delete()
        db.query(ErpDimensionValue).filter(
            ErpDimensionValue.entity_id == _TEST_ENTITY_ID,
        ).delete()
        db.commit()
        db.close()


def test_find_matching_gl_accounts_revenue(db_session: Session) -> None:
    """Per-caption lookup is intentionally permissive — the keyword "sales"
    in the Revenue list will match "Cost of Sales" too. Cross-caption
    deduplication happens in :func:`auto_map_ontology_for_demo`."""
    _seed_gl_accounts(db_session, [
        ("4000", "Sales Revenue"),
        ("4100", "Consulting Revenue"),
        ("5000", "Cost of Sales"),
    ])
    db_session.commit()

    accounts = db_session.query(ErpGlAccount).all()
    matches = find_matching_gl_accounts(accounts, "Revenue")
    assert "4000" in matches
    assert "4100" in matches


def test_auto_map_dedupes_cost_of_sales_to_cost_of_revenue(db_session: Session) -> None:
    """Verify the cross-caption resolver: 'Cost of Sales' must end up under
    'Cost of Revenue' (longer, more-specific keyword), NOT under Revenue."""
    _seed_gl_accounts(db_session, [
        ("4000", "Sales Revenue"),
        ("5000", "Cost of Sales"),
    ])
    db_session.commit()

    result = auto_map_ontology_for_demo(
        db_session, entity_id=_TEST_ENTITY_ID,
        statement="pl", dry_run=True, overwrite=False,
    )
    by_caption = {
        e.tflux_caption: list(e.matched_gl_accounts) for e in result.entries
    }
    assert "5000" in by_caption.get("Cost of Revenue", [])
    assert "5000" not in by_caption.get("Revenue", [])


def test_find_matching_gl_accounts_no_keywords_no_results(db_session: Session) -> None:
    _seed_gl_accounts(db_session, [("9999", "Suspense Account")])
    db_session.commit()
    accounts = db_session.query(ErpGlAccount).all()
    assert find_matching_gl_accounts(accounts, "Revenue") == []


def test_find_matching_dimension_values_picks_known_dimensions(db_session: Session) -> None:
    _seed_dimension(db_session, "AREA", "Area", "EU", "Europe")
    _seed_dimension(db_session, "DEPT", "Department", "ENG", "Engineering")
    _seed_dimension(db_session, "OTHER", "Project", "P1", "Phoenix")
    db_session.commit()

    dim_values = db_session.query(ErpDimensionValue).all()
    matches = find_matching_dimension_values(dim_values, "Revenue")
    assert "EU" in matches
    assert "ENG" in matches
    assert "P1" not in matches


def test_auto_map_dry_run_does_not_persist(db_session: Session) -> None:
    _seed_gl_accounts(db_session, [
        ("4000", "Sales Revenue"),
        ("5000", "Cost of Sales"),
        ("6100", "Salaries and Wages"),
    ])
    db_session.commit()

    result = auto_map_ontology_for_demo(
        db_session, entity_id=_TEST_ENTITY_ID,
        statement="pl", dry_run=True, overwrite=False,
    )
    assert result.captions_mapped >= 1
    assert db_session.query(OntologyMapping).filter(
        OntologyMapping.entity_id == _TEST_ENTITY_ID,
    ).count() == 0


def test_auto_map_commit_writes_mappings(db_session: Session) -> None:
    _seed_gl_accounts(db_session, [
        ("4000", "Sales Revenue"),
        ("4100", "Consulting Revenue"),
        ("6100", "Salaries and Wages"),
    ])
    db_session.commit()

    result = auto_map_ontology_for_demo(
        db_session, entity_id=_TEST_ENTITY_ID,
        statement="pl", dry_run=False, overwrite=False,
    )
    db_session.commit()

    rows = db_session.query(OntologyMapping).filter(
        OntologyMapping.entity_id == _TEST_ENTITY_ID,
    ).all()
    captions = {r.tflux_caption: list(r.erp_gl_json or []) for r in rows}
    assert "Revenue" in captions
    assert "4000" in captions["Revenue"]
    assert result.captions_mapped >= 2


def test_auto_map_overwrite_false_preserves_existing(db_session: Session) -> None:
    _seed_gl_accounts(db_session, [("4000", "Sales Revenue")])
    db_session.add(OntologyMapping(
        entity_id=_TEST_ENTITY_ID, statement="pl",
        tflux_caption="Revenue", erp_gl_json=["MANUAL-1"],
    ))
    db_session.commit()

    result = auto_map_ontology_for_demo(
        db_session, entity_id=_TEST_ENTITY_ID,
        statement="pl", dry_run=False, overwrite=False,
    )
    db_session.commit()

    row = db_session.query(OntologyMapping).filter(
        OntologyMapping.entity_id == _TEST_ENTITY_ID,
        OntologyMapping.tflux_caption == "Revenue",
    ).first()
    assert row is not None
    assert list(row.erp_gl_json or []) == ["MANUAL-1"]
    assert any(
        e.tflux_caption == "Revenue" and
        e.skipped_reason == "existing_mapping_preserved"
        for e in result.entries
    )


def test_auto_map_overwrite_true_replaces_existing(db_session: Session) -> None:
    _seed_gl_accounts(db_session, [("4000", "Sales Revenue")])
    db_session.add(OntologyMapping(
        entity_id=_TEST_ENTITY_ID, statement="pl",
        tflux_caption="Revenue", erp_gl_json=["MANUAL-1"],
    ))
    db_session.commit()

    auto_map_ontology_for_demo(
        db_session, entity_id=_TEST_ENTITY_ID,
        statement="pl", dry_run=False, overwrite=True,
    )
    db_session.commit()

    row = db_session.query(OntologyMapping).filter(
        OntologyMapping.entity_id == _TEST_ENTITY_ID,
        OntologyMapping.tflux_caption == "Revenue",
    ).first()
    assert row is not None
    assert "4000" in list(row.erp_gl_json or [])
    assert "MANUAL-1" not in list(row.erp_gl_json or [])


def _seed_typed_gl(
    db: Session,
    accounts: list[tuple[str, str, str, str]],
) -> None:
    """Seed GL rows with ``(account_no, name, account_type, category)``."""
    for account_no, account_name, account_type, category in accounts:
        db.add(ErpGlAccount(
            entity_id=_TEST_ENTITY_ID, erp_type="bc",
            company_id="COMP-1",
            account_no=account_no, account_name=account_name,
            account_type=account_type, account_category=category,
        ))


def test_auto_map_revenue_prefers_posting_sales_accounts(db_session: Session) -> None:
    _seed_typed_gl(db_session, [
        ("6110", "Sales, Retail - Dom.", "Posting", "Income"),
        ("6120", "Sales, Retail - EU", "Posting", "Income"),
        ("6130", "Sales, Retail - Export", "Posting", "Income"),
        ("5610", "Sales VAT 25 %", "Posting", "Liabilities"),
        ("6995", "Total Revenue", "End_x002D_Total", "Income"),
        ("5000", "Cost of Sales", "Posting", "Cost_x0020_of_x0020_Goods_x0020_Sold"),
    ])
    db_session.commit()

    result = auto_map_ontology_for_demo(
        db_session, entity_id=_TEST_ENTITY_ID,
        statement="pl", dry_run=True, overwrite=True,
    )
    by_caption = {
        e.tflux_caption: e for e in result.entries
    }
    revenue = by_caption["Revenue"]
    assert "6110" in revenue.matched_gl_accounts
    assert "6120" in revenue.matched_gl_accounts
    assert "6130" in revenue.matched_gl_accounts
    assert "5610" not in revenue.matched_gl_accounts
    assert "6995" not in revenue.matched_gl_accounts
    assert revenue.skipped_non_posting_accounts >= 1
    assert revenue.skipped_suspicious_category_accounts >= 1


def test_auto_map_fixed_assets_excludes_expense_accounts(db_session: Session) -> None:
    _seed_typed_gl(db_session, [
        ("10100", "PROPERTY, PLANT AND EQUIPMENT", "Posting", "Assets"),
        ("60410", "Utilities Expense - Power Plant", "Posting", "Expense"),
    ])
    db_session.commit()

    result = auto_map_ontology_for_demo(
        db_session, entity_id=_TEST_ENTITY_ID,
        statement="bs", dry_run=True, overwrite=True,
    )
    fa = next(e for e in result.entries if e.tflux_caption == "Fixed Assets / PP&E")
    assert "10100" in fa.matched_gl_accounts
    assert "60410" not in fa.matched_gl_accounts
    assert fa.skipped_suspicious_category_accounts >= 1


def test_auto_map_ar_excludes_consumables(db_session: Session) -> None:
    _seed_typed_gl(db_session, [
        ("2300", "Accounts Receivable", "Posting", "Assets"),
        ("10550", "CONSUMABLES, SPARES & TOOLS", "Posting", "Assets"),
    ])
    db_session.commit()

    result = auto_map_ontology_for_demo(
        db_session, entity_id=_TEST_ENTITY_ID,
        statement="bs", dry_run=True, overwrite=True,
    )
    ar = next(e for e in result.entries if e.tflux_caption == "Accounts Receivable")
    assert "2300" in ar.matched_gl_accounts
    assert "10550" not in ar.matched_gl_accounts
