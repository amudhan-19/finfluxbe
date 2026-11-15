# @file    tests/conftest.py
#
# Shared test fixtures: forces a SQLite in-memory engine + bootstraps the
# admin user, an analyst user, and a sample entity used by every test file.
# Centralizing this avoids each test rebinding the engine and stepping on
# its siblings (which used to cause cross-file flakiness).

from __future__ import annotations
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# Set env BEFORE importing anything from app.* so pydantic-settings picks it up.
os.environ.update({
    "APP_ENV":     "test",
    "DB_HOST":     "ignored", "DB_PORT": "0", "DB_USER": "u",
    "DB_PASSWORD": "p", "DB_NAME": ":memory:",
    "JWT_SECRET":  "t" * 64,
    "FERNET_KEY":  "ZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmY=",
    "ADMIN_EMAIL": "admin@example.com",
    "ADMIN_PASSWORD": "Test@12345",
})

from app.db import session as db_session  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
db_session.engine = _engine
db_session.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

from app.db.session import Base  # noqa: E402
from app.core.security import hash_password  # noqa: E402
import app.models.models as models  # noqa: E402  registers all ORM models

Base.metadata.create_all(bind=_engine)


SHARED_ENTITY_ID = "ENT-BC-1"


@pytest.fixture(autouse=True)
def _seed_users_and_entity():
    """Idempotent seed for admin + analyst users and one test entity."""
    db = db_session.SessionLocal()
    try:
        admin = db.query(models.User).filter(
            models.User.email == "admin@example.com"
        ).first()
        if not admin:
            admin = models.User(
                email="admin@example.com",
                password=hash_password("Test@12345"),
                first_name="Test", last_name="Admin",
                initials="TA", color="#1e293b",
                role="admin", status="active",
            )
            db.add(admin)
            db.flush()

        analyst = db.query(models.User).filter(
            models.User.email == "analyst@example.com"
        ).first()
        if not analyst:
            analyst = models.User(
                email="analyst@example.com",
                password=hash_password("Test@12345"),
                first_name="Test", last_name="Analyst",
                initials="TN", color="#64748b",
                role="analyst", status="active",
            )
            db.add(analyst)
            db.flush()

        if not db.query(models.Entity).filter(models.Entity.id == SHARED_ENTITY_ID).first():
            db.add(models.Entity(
                id=SHARED_ENTITY_ID, name="BC Test Entity",
                region="North America", supervisor_id=admin.id,
                period_from="2025-01-01", period_to="2025-12-31",
            ))

        # Map the shared test entity to Business Central so the BC router's
        # mapping guard doesn't 409 every test in the suite.
        if not db.query(models.EntityErpMapping).filter(
            models.EntityErpMapping.entity_id == SHARED_ENTITY_ID,
            models.EntityErpMapping.erp_key == "bc",
        ).first():
            db.add(models.EntityErpMapping(
                entity_id=SHARED_ENTITY_ID, erp_key="bc",
                status="connected", connected_by=admin.id,
            ))
        db.commit()
    finally:
        db.close()
    yield
