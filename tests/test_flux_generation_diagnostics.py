# @file    tests/test_flux_generation_diagnostics.py
#
# Integration tests for the new per-entity Flux generation routes:
#   • GET  /api/entities/{id}/flux-generation-diagnostics
#   • POST /api/entities/{id}/flux/save-period-and-generate
#
# Uses the in-memory SQLite engine bound by `tests/conftest.py`.

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.main import app
from app.models.models import (
    Entity,
    ErpGeneralLedgerEntry,
    ErpGlAccount,
    FluxData,
    OntologyMapping,
    Threshold,
    User,
    UserEntityAccess,
)


_ENT = "ENT-DIAG-1"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def supervisor_token(client: TestClient) -> str:
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == "admin@example.com").first()
        # Promote the seed analyst to supervisor for these tests so role-gated
        # routes accept the token; restored at teardown.
        analyst = db.query(User).filter(
            User.email == "analyst@example.com",
        ).first()
        analyst.role = "supervisor"
        if not db.query(Entity).filter(Entity.id == _ENT).first():
            db.add(Entity(
                id=_ENT, name="Diagnostics Test",
                region="North America", supervisor_id=admin.id,
                period_type="MoM",
                period_from="Mar 2026", period_to="Feb 2026",
            ))
        if not db.query(UserEntityAccess).filter(
            UserEntityAccess.user_id == analyst.id,
            UserEntityAccess.entity_id == _ENT,
        ).first():
            db.add(UserEntityAccess(
                user_id=analyst.id, entity_id=_ENT, granted_by=admin.id,
            ))
        db.add(Threshold(
            entity_id=_ENT, statement="pl",
            mode="both", pct_value=Decimal("10"),
            abs_value=Decimal("100000"), logic="or",
        ))
        db.commit()
        token = client.post("/api/auth/login", json={
            "email": "analyst@example.com", "password": "Test@12345",
        }).json()["access_token"]
        yield token
    finally:
        # Restore role + clean test data.
        analyst = db.query(User).filter(
            User.email == "analyst@example.com",
        ).first()
        if analyst:
            analyst.role = "analyst"
        db.query(FluxData).filter(FluxData.entity_id == _ENT).delete()
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
        db.query(UserEntityAccess).filter(
            UserEntityAccess.entity_id == _ENT,
        ).delete()
        db.query(Entity).filter(Entity.id == _ENT).delete()
        db.commit()
        db.close()


def _seed_bc_data(db: Session, *, current_total: Decimal,
                  comparison_total: Decimal) -> None:
    db.add(ErpGlAccount(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        account_no="4000", account_name="Sales Revenue",
    ))
    # Two entries in March 2026 (current) summing to current_total.
    db.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E1", posting_date=date(2026, 3, 5),
        account_no="4000", amount=current_total / 2,
    ))
    db.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E2", posting_date=date(2026, 3, 18),
        account_no="4000", amount=current_total / 2,
    ))
    # One entry in February 2026 (comparison) summing to comparison_total.
    db.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E3", posting_date=date(2026, 2, 12),
        account_no="4000", amount=comparison_total,
    ))
    db.add(OntologyMapping(
        entity_id=_ENT, statement="pl",
        tflux_caption="Revenue",
        erp_gl_json=["4000"], erp_subgl_json=[],
    ))
    db.commit()


def test_diagnostics_returns_parsed_period_and_bc_range(
    client: TestClient, supervisor_token: str,
) -> None:
    db = SessionLocal()
    try:
        _seed_bc_data(db,
                      current_total=Decimal("750000"),
                      comparison_total=Decimal("400000"))
    finally:
        db.close()

    headers = {"Authorization": f"Bearer {supervisor_token}"}
    resp = client.get(
        f"/api/entities/{_ENT}/flux-generation-diagnostics",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entity_id"] == _ENT
    assert body["current_label"] == "Mar 2026"
    assert body["comparison_label"] == "Feb 2026"
    assert body["bc_entry_count"] == 3
    assert body["gl_entries_in_current_range"] == 2
    assert body["gl_entries_in_comparison_range"] == 1
    assert body["ontology_mappings_total"] == 1
    assert body["captions_with_mapped_gl"] == 1
    assert body["is_selected_range_within_bc_data"] is True


def test_save_period_and_generate_writes_flux_data_for_new_window(
    client: TestClient, supervisor_token: str,
) -> None:
    db = SessionLocal()
    try:
        _seed_bc_data(db,
                      current_total=Decimal("750000"),
                      comparison_total=Decimal("400000"))
    finally:
        db.close()

    headers = {"Authorization": f"Bearer {supervisor_token}"}
    resp = client.post(
        f"/api/entities/{_ENT}/flux/save-period-and-generate",
        headers=headers,
        json={
            "period_type": "MoM",
            "period_from": "Mar 2026",
            "period_to":   "Feb 2026",
            "statement":   "pl",
            "overwriteExisting": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current_label"] == "Mar 2026"
    assert body["comparison_label"] == "Feb 2026"
    assert body["rows_inserted"] + body["rows_updated"] >= 1
    assert body["matched_transaction_total"] == 3
    assert body["safe_error_code"] is None

    # Flux Review API now sees the BC-generated row.
    db = SessionLocal()
    try:
        fd = db.query(FluxData).filter(
            FluxData.entity_id == _ENT,
            FluxData.caption == "Revenue",
        ).first()
        assert fd is not None
        assert fd.source_type == "business_central"
        assert float(fd.current_value) == 750000.0
        assert float(fd.comp_value) == 400000.0
    finally:
        db.close()


def test_save_period_and_generate_warns_when_no_bc_data(
    client: TestClient, supervisor_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {supervisor_token}"}
    resp = client.post(
        f"/api/entities/{_ENT}/flux/save-period-and-generate",
        headers=headers,
        json={
            "period_type": "MoM",
            "period_from": "Mar 2026",
            "period_to":   "Feb 2026",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["safe_error_code"] == "BC_TRANSACTIONS_NOT_SYNCED"


def test_save_period_handles_supervisor_csv_picker(
    client: TestClient, supervisor_token: str,
) -> None:
    db = SessionLocal()
    try:
        _seed_bc_data(db,
                      current_total=Decimal("500000"),
                      comparison_total=Decimal("250000"))
    finally:
        db.close()

    headers = {"Authorization": f"Bearer {supervisor_token}"}
    resp = client.post(
        f"/api/entities/{_ENT}/flux/save-period-and-generate",
        headers=headers,
        json={
            "period_type": "Custom",
            # Multi-month CSV from the supervisor MonthPicker.
            "period_from": "2026-01, 2026-02, 2026-03",
            "period_to":   "2025-01, 2025-02, 2025-03",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current_label"] == "Jan 2026 - Mar 2026"
    assert body["comparison_label"] == "Jan 2025 - Mar 2025"
