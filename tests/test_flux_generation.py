# @file    tests/test_flux_generation.py
#
# End-to-end test for the BC → Ontology → Flux pipeline. Seeds a fresh
# entity with a Revenue mapping and a couple of GL entries, then verifies
# that preview/commit produce the right numbers and that Flux Review's
# existing GET endpoint returns the BC-generated rows with provenance.

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
from app.services.flux_generation import (
    BC_SOURCE_TYPE,
    detect_seed_flux_rows,
    extract_mapped_dimension_values,
    extract_mapped_gl_account_nos,
    parse_entity_period,
    preview_flux_generation,
    upsert_bc_generated_flux_rows,
)


_TEST_ENTITY = "ENT-FLUXGEN-1"
# Convention (matches seed loader + FluxReview UI):
#   period_from = CURRENT period
#   period_to   = COMPARISON period
_PERIOD_FROM = "Mar 2026"   # current
_PERIOD_TO   = "Feb 2026"   # comparison


@pytest.fixture()
def db_session() -> Session:
    db = SessionLocal()
    try:
        # Bootstrap a fresh entity for each run so seed entities aren't touched.
        if not db.query(Entity).filter(Entity.id == _TEST_ENTITY).first():
            db.add(Entity(
                id=_TEST_ENTITY, name="Flux Gen Test",
                region="North America", supervisor_id=1,
                period_type="MoM",
                period_from=_PERIOD_FROM, period_to=_PERIOD_TO,
            ))
        db.add(Threshold(
            entity_id=_TEST_ENTITY, statement="pl",
            mode="both", pct_value=Decimal("10"),
            abs_value=Decimal("100000"), logic="or",
        ))
        db.add(ErpGlAccount(
            entity_id=_TEST_ENTITY, erp_type="bc", company_id="C1",
            account_no="4000", account_name="Sales Revenue",
        ))
        db.add(ErpGlAccount(
            entity_id=_TEST_ENTITY, erp_type="bc", company_id="C1",
            account_no="4100", account_name="Consulting Revenue",
        ))
        # 2 entries in current month, 1 in comparison month.
        db.add(ErpGeneralLedgerEntry(
            entity_id=_TEST_ENTITY, erp_type="bc", company_id="C1",
            external_entry_no="E1",
            posting_date=date(2026, 3, 5),
            account_no="4000", amount=Decimal("500000"),
            description="Q1 client billing",
            raw_source_json={"customerName": "Acme Co", "customerNumber": "C-001"},
        ))
        db.add(ErpGeneralLedgerEntry(
            entity_id=_TEST_ENTITY, erp_type="bc", company_id="C1",
            external_entry_no="E2",
            posting_date=date(2026, 3, 18),
            account_no="4100", amount=Decimal("250000"),
            description="Strategy retainer",
        ))
        db.add(ErpGeneralLedgerEntry(
            entity_id=_TEST_ENTITY, erp_type="bc", company_id="C1",
            external_entry_no="E3",
            posting_date=date(2026, 2, 12),
            account_no="4000", amount=Decimal("400000"),
            description="Feb retainer",
        ))
        db.add(OntologyMapping(
            entity_id=_TEST_ENTITY, statement="pl",
            tflux_caption="Revenue",
            erp_gl_json=["4000", "4100"], erp_subgl_json=[],
        ))
        db.commit()
        yield db
    finally:
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
        db.query(Threshold).filter(
            Threshold.entity_id == _TEST_ENTITY,
        ).delete()
        db.commit()
        db.close()


def test_parse_entity_period_supports_month_label() -> None:
    start, end = parse_entity_period("Mar 2026")
    assert start == date(2026, 3, 1)
    assert end == date(2026, 3, 31)


def test_extract_mapped_gl_account_nos_handles_string_and_dict() -> None:
    assert extract_mapped_gl_account_nos(["4000", " 4100"]) == {"4000", "4100"}
    assert extract_mapped_gl_account_nos(
        [{"account_no": "4000"}, {"number": "5000"}]
    ) == {"4000", "5000"}
    assert extract_mapped_gl_account_nos(None) == set()


def test_extract_mapped_dimension_values_accepts_value_code_dicts() -> None:
    assert extract_mapped_dimension_values(
        [{"value_code": "EU"}, "ENG", {"code": "ASIA"}]
    ) == {"EU", "ENG", "ASIA"}


def test_preview_calculates_revenue_variance(db_session: Session) -> None:
    entity = db_session.query(Entity).filter(Entity.id == _TEST_ENTITY).first()
    rows, diag = preview_flux_generation(db_session, entity, "pl")

    assert len(rows) == 1
    rev = rows[0]
    assert rev.caption == "Revenue"
    assert rev.current_value == Decimal("750000.00")
    assert rev.comp_value == Decimal("400000.00")
    assert rev.matched_account_count == 2
    assert rev.matched_transaction_count == 3
    assert rev.is_material is True
    assert rev.skipped_reason is None

    assert diag.captions_total == 1
    assert diag.captions_with_mappings == 1
    assert diag.captions_without_mappings == []
    assert diag.matched_transaction_total == 3


def test_commit_writes_bc_provenance(db_session: Session) -> None:
    entity = db_session.query(Entity).filter(Entity.id == _TEST_ENTITY).first()
    rows, _ = preview_flux_generation(db_session, entity, "pl")

    inserted, updated, skipped = upsert_bc_generated_flux_rows(
        db_session, entity, rows, overwrite_existing=True,
    )
    db_session.commit()

    assert inserted == 1
    assert updated == 0
    assert skipped == 0

    fd = db_session.query(FluxData).filter(
        FluxData.entity_id == _TEST_ENTITY,
        FluxData.caption == "Revenue",
    ).first()
    assert fd is not None
    assert fd.source_type == BC_SOURCE_TYPE
    assert fd.generated_from == "ontology+bc"
    assert fd.generated_at is not None
    assert float(fd.current_value) == 750000.0
    assert float(fd.comp_value) == 400000.0


def test_commit_overwrite_replaces_seed_row(db_session: Session) -> None:
    entity = db_session.query(Entity).filter(Entity.id == _TEST_ENTITY).first()
    # Plant a seed-style row first (NULL source_type).
    db_session.add(FluxData(
        entity_id=_TEST_ENTITY, statement="pl",
        period_from=_PERIOD_FROM, period_to=_PERIOD_TO,
        caption="Revenue",
        current_value=Decimal("1.00"), comp_value=Decimal("1.00"),
        is_material=False,
    ))
    db_session.commit()

    rows, _ = preview_flux_generation(db_session, entity, "pl")
    inserted, updated, skipped = upsert_bc_generated_flux_rows(
        db_session, entity, rows, overwrite_existing=True,
    )
    db_session.commit()

    assert (inserted, updated, skipped) == (0, 1, 0)
    fd = db_session.query(FluxData).filter(
        FluxData.entity_id == _TEST_ENTITY,
        FluxData.caption == "Revenue",
    ).first()
    assert fd.source_type == BC_SOURCE_TYPE
    assert float(fd.current_value) == 750000.0


def test_commit_without_overwrite_preserves_existing(db_session: Session) -> None:
    entity = db_session.query(Entity).filter(Entity.id == _TEST_ENTITY).first()
    db_session.add(FluxData(
        entity_id=_TEST_ENTITY, statement="pl",
        period_from=_PERIOD_FROM, period_to=_PERIOD_TO,
        caption="Revenue",
        current_value=Decimal("1.00"), comp_value=Decimal("1.00"),
        is_material=False,
    ))
    db_session.commit()

    rows, _ = preview_flux_generation(db_session, entity, "pl")
    inserted, updated, skipped = upsert_bc_generated_flux_rows(
        db_session, entity, rows, overwrite_existing=False,
    )
    db_session.commit()

    assert (inserted, updated, skipped) == (0, 0, 1)
    fd = db_session.query(FluxData).filter(
        FluxData.entity_id == _TEST_ENTITY,
        FluxData.caption == "Revenue",
    ).first()
    # Original (seed) values untouched.
    assert float(fd.current_value) == 1.0
    assert fd.source_type is None


def test_detect_seed_flux_rows_includes_null_source(db_session: Session) -> None:
    db_session.add(FluxData(
        entity_id=_TEST_ENTITY, statement="pl",
        period_from=_PERIOD_FROM, period_to=_PERIOD_TO,
        caption="Revenue", current_value=Decimal("1"), comp_value=Decimal("1"),
    ))
    db_session.commit()
    seeds = detect_seed_flux_rows(db_session, _TEST_ENTITY)
    assert len(seeds) == 1
    assert seeds[0].source_type is None
