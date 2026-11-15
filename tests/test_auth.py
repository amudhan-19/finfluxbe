# @file    tests/test_auth.py
#
# Smoke tests: login -> me -> logout, plus 401 for unauthenticated.
# Engine + admin seeding live in tests/conftest.py.

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_login_success_returns_token_and_me():
    r = client.post("/api/auth/login", json={
        "email": "admin@example.com", "password": "Test@12345",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["role"] == "admin"
    assert body["user"]["email"] == "admin@example.com"


def test_login_bad_password():
    r = client.post("/api/auth/login", json={
        "email": "admin@example.com", "password": "wrong",
    })
    assert r.status_code == 401


def test_me_requires_auth():
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_me_with_token():
    r = client.post("/api/auth/login", json={
        "email": "admin@example.com", "password": "Test@12345",
    })
    token = r.json()["access_token"]
    r2 = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert r2.json()["role"] == "admin"


def test_admin_users_endpoint_requires_admin():
    r = client.get("/api/admin/users")
    assert r.status_code == 401  # no token

    r = client.post("/api/auth/login", json={
        "email": "admin@example.com", "password": "Test@12345",
    })
    token = r.json()["access_token"]
    r2 = client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)


def test_director_queue_unauthenticated():
    r = client.get("/api/entities/director/queue")
    assert r.status_code == 401
