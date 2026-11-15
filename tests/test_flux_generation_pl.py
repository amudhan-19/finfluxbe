# @file    tests/test_flux_generation_pl.py
#
# Targeted tests for the PL-empty-while-BS-works demo scenario.
# Verifies:
#   • normalize_statement_key tolerates legacy "P&L" / "PL" / "pnl",
#   • normalize_account_number_token recovers the code from
#     "6100 - Revenue" labels,
#   • per-statement diagnostics break PL out of the combined totals,
#   • the save-and-generate route surfaces PL_NO_MATCHING_TRANSACTIONS
#     when BS regenerates cleanly but PL produces 0 rows.

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.main import app
from app.models.models import (
    Entity, ErpGeneralLedgerEntry, ErpGlAccount,
    FluxData, OntologyMapping, Threshold, User, UserEntityAccess,
)
from app.services.flux_generation import (
    extract_mapped_gl_account_nos,
    normalize_account_number_token,
    normalize_statement_key,
    preview_flux_generation,
    upsert_bc_generated_flux_rows,
)


_ENT = "ENT-PL-1"


# ── Pure helper tests ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value, expected",
    [
        ("pl", "pl"), ("PL", "pl"), ("Pl", "pl"),
        ("P&L", "pl"), ("p&l", "pl"),
        ("pnl", "pl"), ("profit_loss", "pl"),
        ("profit_and_loss", "pl"), ("income_statement", "pl"),
        ("bs", "bs"), ("BS", "bs"), ("balance_sheet", "bs"),
        ("both", "both"), ("BOTH", "both"),
        ("garbage", None), (None, None), ("", None),
    ],
)
def test_normalize_statement_key(value, expected) -> None:
    assert normalize_statement_key(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("6100", "6100"),
        ("  6100 ", "6100"),
        ("6100 - Revenue", "6100"),
        ("6100\u00a0-\u00a0Revenue", "6100"),
        ("6100 \u2014 Revenue", "6100"),    # em-dash
        ("6100 \u2013 Revenue", "6100"),    # en-dash
        ("CASH-IN-HAND", "CASH-IN-HAND"),    # no space around '-' → keep
        ("", None), (None, None), ("   ", None),
    ],
)
def test_normalize_account_number_token(value, expected) -> None:
    assert normalize_account_number_token(value) == expected


def test_extract_mapped_gl_account_nos_handles_label_strings() -> None:
    raw = ["6100 - Revenue", "  6710 \u2014 Consulting Revenue  ", "6800"]
    assert extract_mapped_gl_account_nos(raw) == {"6100", "6710", "6800"}


# ── Per-statement diagnostics ─────────────────────────────────────────
@pytest.fixture()
def db_session() -> Session:
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == "admin@example.com").first()
        if not db.query(Entity).filter(Entity.id == _ENT).first():
            db.add(Entity(
                id=_ENT, name="PL Test",
                region="North America", supervisor_id=admin.id,
                period_type="MoM",
                period_from="Mar 2026", period_to="Feb 2026",
            ))
        db.add(Threshold(
            entity_id=_ENT, statement="pl",
            mode="both", pct_value=Decimal("10"),
            abs_value=Decimal("100000"), logic="or",
        ))
        db.add(Threshold(
            entity_id=_ENT, statement="bs",
            mode="both", pct_value=Decimal("10"),
            abs_value=Decimal("100000"), logic="or",
        ))
        db.commit()
        yield db
    finally:
        db.rollback()
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
        db.query(Entity).filter(Entity.id == _ENT).delete()
        db.commit()
        db.close()


def test_per_statement_diagnostics_pl_empty_bs_ok(db_session: Session) -> None:
    """The exact reproduction of the demo bug: BS captions match BC
    transactions, PL captions don't, and the per-statement breakdown
    shows PL produced 0 rows while BS regenerated cleanly."""
    # BS: Cash mapping with matching transactions in current + comparison.
    db_session.add(ErpGlAccount(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        account_no="1100", account_name="Cash",
    ))
    db_session.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E1", posting_date=date(2026, 3, 5),
        account_no="1100", amount=Decimal("500000"),
    ))
    db_session.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E2", posting_date=date(2026, 2, 10),
        account_no="1100", amount=Decimal("300000"),
    ))
    db_session.add(OntologyMapping(
        entity_id=_ENT, statement="bs",
        tflux_caption="Cash",
        erp_gl_json=["1100"], erp_subgl_json=[],
    ))

    # PL: Revenue mapping with NO matching transactions (account 6100 has
    # no entries, even though account exists in the catalog).
    db_session.add(ErpGlAccount(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        account_no="6100", account_name="Revenue",
    ))
    db_session.add(OntologyMapping(
        entity_id=_ENT, statement="pl",
        tflux_caption="Revenue",
        erp_gl_json=["6100"], erp_subgl_json=[],
    ))
    db_session.commit()

    entity = db_session.query(Entity).filter(Entity.id == _ENT).first()
    rows, diag = preview_flux_generation(db_session, entity, "both")

    assert "pl" in diag.per_statement
    assert "bs" in diag.per_statement
    pl_diag = diag.per_statement["pl"]
    bs_diag = diag.per_statement["bs"]
    assert pl_diag.captions_total == 1
    assert pl_diag.captions_with_mappings == 1
    assert pl_diag.matched_transaction_total == 0
    assert "Revenue" in pl_diag.captions_with_zero_transactions
    assert bs_diag.captions_total == 1
    # Balance-at-date counts current-period + comparison-period cumulative
    # matches (E1+E2 through Mar 2026 = 2, E2 through Feb 2026 = 1).
    assert bs_diag.matched_transaction_total == 3

    inserted, updated, skipped = upsert_bc_generated_flux_rows(
        db_session, entity, rows, overwrite_existing=True, diag=diag,
    )
    db_session.commit()
    # BS row inserts; PL row is skipped (no matching transactions).
    assert inserted >= 1
    assert diag.per_statement["pl"].rows_inserted == 0
    assert diag.per_statement["pl"].rows_skipped >= 1
    assert diag.per_statement["bs"].rows_inserted >= 1


def test_pl_label_format_resolves_via_normalization(db_session: Session) -> None:
    """Mapping stored as ``"6100 - Revenue"`` (label) still matches an
    entry whose ``account_no = "6100"``."""
    db_session.add(ErpGlAccount(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        account_no="6100", account_name="Revenue",
    ))
    db_session.add(ErpGeneralLedgerEntry(
        entity_id=_ENT, erp_type="bc", company_id="C1",
        external_entry_no="E1", posting_date=date(2026, 3, 5),
        account_no="6100", amount=Decimal("250000"),
    ))
    db_session.add(OntologyMapping(
        entity_id=_ENT, statement="pl",
        tflux_caption="Revenue",
        erp_gl_json=["6100 - Revenue"], erp_subgl_json=[],
    ))
    db_session.commit()

    entity = db_session.query(Entity).filter(Entity.id == _ENT).first()
    rows, diag = preview_flux_generation(db_session, entity, "pl")
    assert len(rows) == 1
    assert rows[0].caption == "Revenue"
    assert rows[0].current_value == Decimal("250000.00")


# ── Route-level surface ───────────────────────────────────────────────
@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def supervisor_token(client: TestClient) -> str:
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == "admin@example.com").first()
        analyst = db.query(User).filter(
            User.email == "analyst@example.com",
        ).first()
        analyst.role = "supervisor"
        if not db.query(Entity).filter(Entity.id == _ENT).first():
            db.add(Entity(
                id=_ENT, name="PL Test",
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
        db.commit()
        token = client.post("/api/auth/login", json={
            "email": "analyst@example.com", "password": "Test@12345",
        }).json()["access_token"]
        yield token
    finally:
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


def test_route_surfaces_pl_no_matching_transactions(
    client: TestClient, supervisor_token: str,
) -> None:
    db = SessionLocal()
    try:
        # BS has matching txns; PL has mapping but no txns.
        db.add(ErpGlAccount(
            entity_id=_ENT, erp_type="bc", company_id="C1",
            account_no="1100", account_name="Cash",
        ))
        db.add(ErpGlAccount(
            entity_id=_ENT, erp_type="bc", company_id="C1",
            account_no="6100", account_name="Revenue",
        ))
        db.add(ErpGeneralLedgerEntry(
            entity_id=_ENT, erp_type="bc", company_id="C1",
            external_entry_no="E1", posting_date=date(2026, 3, 5),
            account_no="1100", amount=Decimal("500000"),
        ))
        db.add(OntologyMapping(
            entity_id=_ENT, statement="bs",
            tflux_caption="Cash",
            erp_gl_json=["1100"], erp_subgl_json=[],
        ))
        db.add(OntologyMapping(
            entity_id=_ENT, statement="pl",
            tflux_caption="Revenue",
            erp_gl_json=["6100"], erp_subgl_json=[],
        ))
        db.add(Threshold(
            entity_id=_ENT, statement="pl",
            mode="both", pct_value=Decimal("10"),
            abs_value=Decimal("100000"), logic="or",
        ))
        db.commit()
    finally:
        db.close()

    resp = client.post(
        f"/api/entities/{_ENT}/flux/save-period-and-generate",
        headers={"Authorization": f"Bearer {supervisor_token}"},
        json={
            "period_type": "MoM",
            "period_from": "Mar 2026",
            "period_to":   "Feb 2026",
            "statement":   "both",
            "overwriteExisting": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["safe_error_code"] == "PL_NO_MATCHING_TRANSACTIONS"
    assert "pl" in body["statements"]
    assert body["statements"]["pl"]["rows_generated"] == 0
    assert body["statements"]["bs"]["rows_generated"] >= 1


def test_diagnostics_endpoint_filters_by_statement(
    client: TestClient, supervisor_token: str,
) -> None:
    db = SessionLocal()
    try:
        db.add(ErpGlAccount(
            entity_id=_ENT, erp_type="bc", company_id="C1",
            account_no="6100", account_name="Revenue",
        ))
        db.add(OntologyMapping(
            entity_id=_ENT, statement="pl",
            tflux_caption="Revenue",
            erp_gl_json=["6100"], erp_subgl_json=[],
        ))
        db.add(OntologyMapping(
            entity_id=_ENT, statement="bs",
            tflux_caption="Cash",
            erp_gl_json=["1100"], erp_subgl_json=[],
        ))
        db.commit()
    finally:
        db.close()

    headers = {"Authorization": f"Bearer {supervisor_token}"}
    resp_pl = client.get(
        f"/api/entities/{_ENT}/flux-generation-diagnostics",
        headers=headers, params={"statement": "pl"},
    )
    assert resp_pl.status_code == 200, resp_pl.text
    body_pl = resp_pl.json()
    assert body_pl["statement_filter"] == "pl"
    assert body_pl["ontology_mappings_total"] == 1
    pl_caption_names = [c["caption"] for c in body_pl["captions"]]
    assert pl_caption_names == ["Revenue"]

    resp_all = client.get(
        f"/api/entities/{_ENT}/flux-generation-diagnostics",
        headers=headers,
    )
    assert resp_all.status_code == 200, resp_all.text
    body_all = resp_all.json()
    assert body_all["statement_filter"] is None
    assert body_all["ontology_mappings_total"] == 2
