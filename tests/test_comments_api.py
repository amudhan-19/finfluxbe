# @file    tests/test_comments_api.py

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import SHARED_ENTITY_ID


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    token = client.post("/api/auth/login", json={
        "email": "admin@example.com",
        "password": "Test@12345",
    }).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_comments_query_param_supports_slash_caption(
    client: TestClient, auth_headers: dict[str, str],
) -> None:
    caption = "Fixed Assets / PP&E"
    r = client.get(
        f"/api/entities/{SHARED_ENTITY_ID}/comments",
        params={"caption": caption},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json() == []


def test_comments_query_param_add_and_list(
    client: TestClient, auth_headers: dict[str, str],
) -> None:
    caption = "Travel & Entertainment"
    post = client.post(
        f"/api/entities/{SHARED_ENTITY_ID}/comments",
        params={"caption": caption},
        json={"text": "Demo comment"},
        headers=auth_headers,
    )
    assert post.status_code == 201
    assert post.json()["caption"] == caption

    listed = client.get(
        f"/api/entities/{SHARED_ENTITY_ID}/comments",
        params={"caption": caption},
        headers=auth_headers,
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["text"] == "Demo comment"


def test_comments_legacy_path_still_works_for_simple_caption(
    client: TestClient, auth_headers: dict[str, str],
) -> None:
    r = client.get(
        f"/api/entities/{SHARED_ENTITY_ID}/comments/Cash",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert isinstance(r.json(), list)
