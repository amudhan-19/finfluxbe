# @file    tests/test_subledger_catalog.py

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.models import (
    Entity,
    ErpCustomer,
    ErpGeneralLedgerEntry,
    ErpVendor,
)
from app.services.subledger_catalog import (
    build_subledger_options_response,
    entry_subledger_tokens,
)


@pytest.fixture()
def db_session() -> Session:
    db = SessionLocal()
    entity_id = "ENT-SUBLEDGER-1"
    try:
        if not db.query(Entity).filter(Entity.id == entity_id).first():
            db.add(Entity(
                id=entity_id, name="Subledger Catalog Test",
                region="North America", supervisor_id=1,
                period_type="MoM", period_from="Mar 2026", period_to="Feb 2026",
            ))
        db.query(ErpCustomer).filter(
            ErpCustomer.entity_id == entity_id,
        ).delete()
        db.query(ErpVendor).filter(
            ErpVendor.entity_id == entity_id,
        ).delete()
        db.query(ErpGeneralLedgerEntry).filter(
            ErpGeneralLedgerEntry.entity_id == entity_id,
        ).delete()
        db.add(ErpCustomer(
            entity_id=entity_id, erp_type="bc", company_id="C1",
            customer_no="C10010", display_name="ABC Traders",
        ))
        db.add(ErpVendor(
            entity_id=entity_id, erp_type="bc", company_id="C1",
            vendor_no="V20020", display_name="XYZ Suppliers",
        ))
        db.add(ErpGeneralLedgerEntry(
            entity_id=entity_id, erp_type="bc", company_id="C1",
            external_entry_no="GL1",
            posting_date=date(2026, 2, 1),
            account_no="1000", amount=Decimal("1"),
            document_no="DOC-ONLY",
            description="Direct Cost 20000 on 17/01/23",
            raw_source_json={"documentNumber": "DOC-ONLY"},
        ))
        db.commit()
        yield db, entity_id
    finally:
        db.close()


def test_subledger_options_customer_vendor_master_only(db_session) -> None:
    db, entity_id = db_session
    resp = build_subledger_options_response(db, entity_id)
    labels = [c["label"] for c in resp["customers"]] + [v["label"] for v in resp["vendors"]]
    assert "Customer: C10010 — ABC Traders" in labels
    assert "Vendor: V20020 — XYZ Suppliers" in labels
    assert not any(lbl.startswith("Document:") for lbl in labels)
    assert resp["message"] is None


def test_gl_entry_document_not_in_tokens() -> None:
    entry = ErpGeneralLedgerEntry(
        entity_id="X", erp_type="bc", company_id="C1",
        external_entry_no="GL1",
        posting_date=date(2026, 1, 1),
        account_no="1000", amount=Decimal("1"),
        document_no="DOC-ONLY",
        description="Should not appear as sub-ledger option",
    )
    assert "document:DOC-ONLY" not in entry_subledger_tokens(entry)
