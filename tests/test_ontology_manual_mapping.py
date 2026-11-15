# @file    tests/test_ontology_manual_mapping.py
#
# Verifies manual-only ontology reset and disabled demo auto-map endpoint.

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.main import app
from app.models.models import CustomCaption, Entity, OntologyMapping, User, ApiCredential


_TEST_ENTITY = "ENT-ONT-MANUAL-1"


@pytest.fixture()
def db_session() -> Session:
    db = SessionLocal()
    try:
        if not db.query(Entity).filter(Entity.id == _TEST_ENTITY).first():
            admin = db.query(User).filter(User.role == "admin").first()
            db.add(Entity(
                id=_TEST_ENTITY, name="Ontology Manual Test",
                region="North America",
                supervisor_id=admin.id if admin else 1,
                period_type="MoM",
                period_from="Mar 2026",
                period_to="Feb 2026",
            ))
        db.query(OntologyMapping).filter(
            OntologyMapping.entity_id == _TEST_ENTITY,
        ).delete()
        db.query(CustomCaption).filter(
            CustomCaption.entity_id == _TEST_ENTITY,
        ).delete()
        db.query(ApiCredential).filter(
            ApiCredential.entity_id == _TEST_ENTITY,
        ).delete()
        for layer in ("gl", "sub_gl", "transaction"):
            db.add(ApiCredential(
                entity_id=_TEST_ENTITY, layer=layer, status="verified",
            ))
        db.add(OntologyMapping(
            entity_id=_TEST_ENTITY, statement="pl",
            tflux_caption="Revenue",
            erp_gl_json=["6110", "6995"], erp_subgl_json=["EU"],
        ))
        db.add(CustomCaption(
            entity_id=_TEST_ENTITY, statement="pl",
            name="Custom Line", erp_gl_json=["5000"], erp_subgl_json=[],
        ))
        db.commit()
        yield db
    finally:
        db.query(OntologyMapping).filter(
            OntologyMapping.entity_id == _TEST_ENTITY,
        ).delete()
        db.query(CustomCaption).filter(
            CustomCaption.entity_id == _TEST_ENTITY,
        ).delete()
        db.query(ApiCredential).filter(
            ApiCredential.entity_id == _TEST_ENTITY,
        ).delete()
        db.commit()
        db.close()


@pytest.fixture()
def admin_client(db_session: Session) -> TestClient:
    admin = db_session.query(User).filter(User.role == "admin").first()
    assert admin is not None
    client = TestClient(app)
    login = client.post("/api/auth/login", json={
        "email": "admin@example.com", "password": "Test@12345",
    })
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_demo_auto_map_endpoint_disabled(admin_client: TestClient) -> None:
    resp = admin_client.post(
        f"/api/admin/entities/{_TEST_ENTITY}/ontology/demo-auto-map",
        json={"statement": "pl", "dry_run": True},
    )
    assert resp.status_code == 410


def test_reset_mappings_clears_gl_json(
    admin_client: TestClient, db_session: Session,
) -> None:
    resp = admin_client.post(
        f"/api/admin/ontology/{_TEST_ENTITY}/reset-mappings",
        params={"statement": "pl"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["mappings_cleared"] >= 1

    db_session.expire_all()
    row = db_session.query(OntologyMapping).filter(
        OntologyMapping.entity_id == _TEST_ENTITY,
        OntologyMapping.tflux_caption == "Revenue",
    ).first()
    assert row is not None
    assert row.erp_gl_json == []
    assert row.erp_subgl_json == []

    cap = db_session.query(CustomCaption).filter(
        CustomCaption.entity_id == _TEST_ENTITY,
        CustomCaption.name == "Custom Line",
    ).first()
    assert cap is not None
    assert cap.erp_gl_json == []
