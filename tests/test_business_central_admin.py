# @file    tests/test_business_central_admin.py
#
# Smoke tests for the Business Central admin endpoints. NO real Microsoft
# secrets and NO outgoing HTTP — only RBAC, validation, and safe-response
# shape. Engine + user seeding live in tests/conftest.py.

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import SHARED_ENTITY_ID


client = TestClient(app)

# Synthetic GUIDs — NOT real Microsoft credentials.
FAKE_TENANT  = "11111111-2222-3333-4444-555555555555"
FAKE_CLIENT  = "66666666-7777-8888-9999-aaaaaaaaaaaa"
FAKE_SECRET  = "test-secret-not-real-AbCdEfGhIjKlMnOp"  # noqa: S105


def _login(email: str, password: str = "Test@12345") -> str:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {_login('admin@example.com')}"}


def _analyst_headers() -> dict:
    return {"Authorization": f"Bearer {_login('analyst@example.com')}"}


def test_unauthenticated_is_401():
    r = client.get(f"/api/admin/integrations/business-central/credentials/{SHARED_ENTITY_ID}")
    assert r.status_code == 401


def test_non_admin_forbidden():
    r = client.get(
        f"/api/admin/integrations/business-central/credentials/{SHARED_ENTITY_ID}",
        headers=_analyst_headers(),
    )
    assert r.status_code == 403


def test_get_when_not_configured_returns_404():
    r = client.get(
        f"/api/admin/integrations/business-central/credentials/{SHARED_ENTITY_ID}",
        headers=_admin_headers(),
    )
    assert r.status_code == 404


def test_first_save_requires_all_fields():
    r = client.post(
        "/api/admin/integrations/business-central/credentials",
        headers=_admin_headers(),
        json={"entity_id": SHARED_ENTITY_ID, "tenant_id": FAKE_TENANT},
    )
    assert r.status_code == 400
    assert "Missing required field" in r.json()["detail"]


def test_invalid_guid_rejected():
    r = client.post(
        "/api/admin/integrations/business-central/credentials",
        headers=_admin_headers(),
        json={
            "entity_id": SHARED_ENTITY_ID,
            "tenant_id": "not-a-guid",
            "client_id": FAKE_CLIENT,
            "client_secret": FAKE_SECRET,
            "environment_name": "Sandbox",
            "scope": "https://api.businesscentral.dynamics.com/.default",
            "base_url": "https://api.businesscentral.dynamics.com",
        },
    )
    assert r.status_code == 422


def test_save_then_get_returns_safe_metadata_only():
    payload = {
        "entity_id": SHARED_ENTITY_ID,
        "tenant_id": FAKE_TENANT,
        "client_id": FAKE_CLIENT,
        "client_secret": FAKE_SECRET,
        "environment_name": "Sandbox",
        "scope": "https://api.businesscentral.dynamics.com/.default",
        "base_url": "https://api.businesscentral.dynamics.com",
    }
    r = client.post(
        "/api/admin/integrations/business-central/credentials",
        headers=_admin_headers(), json=payload,
    )
    assert r.status_code == 201, r.text
    body = r.json()

    assert body["entity_id"] == SHARED_ENTITY_ID
    assert body["erp_type"] == "bc"
    assert body["status"] == "saved"
    assert body["environment_name"] == "Sandbox"
    assert body["base_url"].startswith("https://api.businesscentral.dynamics.com")
    assert body["masked_client_id"].startswith("*")
    assert body["masked_client_id"].endswith(FAKE_CLIENT[-4:])
    assert body["masked_client_secret"].startswith("*")
    assert body["masked_client_secret"].endswith(FAKE_SECRET[-4:])

    for forbidden in (
        "client_secret", "tenant_id", "client_id",
        "tenant_id_encrypted", "client_id_encrypted", "client_secret_encrypted",
    ):
        assert forbidden not in body, f"response leaked field {forbidden}"
    assert FAKE_TENANT not in r.text
    assert FAKE_SECRET not in r.text

    r = client.get(
        f"/api/admin/integrations/business-central/credentials/{SHARED_ENTITY_ID}",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    assert FAKE_TENANT not in r.text
    assert FAKE_SECRET not in r.text


def test_partial_update_keeps_existing_secret():
    client.post(
        "/api/admin/integrations/business-central/credentials",
        headers=_admin_headers(),
        json={
            "entity_id": SHARED_ENTITY_ID,
            "tenant_id": FAKE_TENANT,
            "client_id": FAKE_CLIENT,
            "client_secret": FAKE_SECRET,
            "environment_name": "Sandbox",
            "scope": "https://api.businesscentral.dynamics.com/.default",
            "base_url": "https://api.businesscentral.dynamics.com",
        },
    )
    r = client.post(
        "/api/admin/integrations/business-central/credentials",
        headers=_admin_headers(),
        json={"entity_id": SHARED_ENTITY_ID, "environment_name": "Production"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["environment_name"] == "Production"
    assert body["masked_client_secret"].endswith(FAKE_SECRET[-4:])


def test_companies_list_empty_until_test_runs():
    r = client.get(
        f"/api/admin/integrations/business-central/companies/{SHARED_ENTITY_ID}",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    assert r.json() == []


# ──────────────────────────────────────────────────────────────────────
# Mapping guard / reset / re-entry behavior
# ──────────────────────────────────────────────────────────────────────
def _save_initial_credentials() -> None:
    r = client.post(
        "/api/admin/integrations/business-central/credentials",
        headers=_admin_headers(),
        json={
            "entity_id": SHARED_ENTITY_ID,
            "tenant_id": FAKE_TENANT,
            "client_id": FAKE_CLIENT,
            "client_secret": FAKE_SECRET,
            "environment_name": "Sandbox",
            "scope": "https://api.businesscentral.dynamics.com/.default",
            "base_url": "https://api.businesscentral.dynamics.com",
        },
    )
    assert r.status_code == 201, r.text


def _mark_invalid_config(entity_id: str) -> None:
    """Direct DB poke that mirrors what the connector does after a decrypt
    failure, so we can exercise the re-entry / reset paths without HTTP."""
    from tests.conftest import db_session  # type: ignore[attr-defined]
    from app.models.models import BusinessCentralCredential
    db = db_session.SessionLocal()
    try:
        rec = db.query(BusinessCentralCredential).filter(
            BusinessCentralCredential.entity_id == entity_id
        ).first()
        assert rec is not None
        rec.status = "failed"
        rec.last_safe_error_code = "INVALID_CONFIG"
        rec.last_safe_error_message = "Stored credentials cannot be decrypted."
        db.commit()
    finally:
        db.close()


def test_entity_without_bc_mapping_returns_409():
    # Use a fresh entity that has no BC mapping seeded.
    from tests.conftest import db_session  # type: ignore[attr-defined]
    from app.models.models import Entity, User
    other = "ENT-NO-BC"
    db = db_session.SessionLocal()
    try:
        admin = db.query(User).filter(User.email == "admin@example.com").first()
        if not db.query(Entity).filter(Entity.id == other).first():
            db.add(Entity(
                id=other, name="No-BC Entity", region="North America",
                supervisor_id=admin.id,
                period_from="2025-01-01", period_to="2025-12-31",
            ))
            db.commit()
    finally:
        db.close()

    r = client.get(
        f"/api/admin/integrations/business-central/credentials/{other}",
        headers=_admin_headers(),
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["safe_error_code"] == "ENTITY_ERP_MAPPING_REQUIRED"


def test_invalid_config_partial_update_is_rejected():
    _save_initial_credentials()
    _mark_invalid_config(SHARED_ENTITY_ID)

    r = client.post(
        "/api/admin/integrations/business-central/credentials",
        headers=_admin_headers(),
        json={"entity_id": SHARED_ENTITY_ID, "environment_name": "Production"},
    )
    assert r.status_code == 400
    assert "INVALID_CONFIG" in r.json()["detail"]


def test_invalid_config_full_resave_recovers():
    _save_initial_credentials()
    _mark_invalid_config(SHARED_ENTITY_ID)

    new_secret = "rotated-secret-not-real-AbCdEfGhIjKlMnOp"
    r = client.post(
        "/api/admin/integrations/business-central/credentials",
        headers=_admin_headers(),
        json={
            "entity_id": SHARED_ENTITY_ID,
            "tenant_id": FAKE_TENANT,
            "client_id": FAKE_CLIENT,
            "client_secret": new_secret,
            "environment_name": "Sandbox",
            "scope": "https://api.businesscentral.dynamics.com/.default",
            "base_url": "https://api.businesscentral.dynamics.com",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "saved"
    assert body["last_safe_error_code"] is None
    assert body["masked_client_secret"].endswith(new_secret[-4:])


def test_delete_credentials_removes_row():
    _save_initial_credentials()

    r = client.delete(
        f"/api/admin/integrations/business-central/credentials/{SHARED_ENTITY_ID}",
        headers=_admin_headers(),
    )
    assert r.status_code == 204

    r = client.get(
        f"/api/admin/integrations/business-central/credentials/{SHARED_ENTITY_ID}",
        headers=_admin_headers(),
    )
    assert r.status_code == 404
