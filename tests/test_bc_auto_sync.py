# @file    tests/test_bc_auto_sync.py
#
# Unit tests for the BC auto-sync orchestrator. We mock the BC HTTP layer
# and the replace helpers so the tests run against the in-memory SQLite
# engine bound by `tests/conftest.py` and never touch real BC.

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.models import (
    BusinessCentralCredential,
    Entity,
    EntityErpMapping,
    ErpGeneralLedgerEntry,
    ErpGlAccount,
    ErpSyncRun,
    OntologyMapping,
    SYNC_AUTO_BC_SYNC,
    SYNC_DIMENSIONS,
    SYNC_GENERATE_FLUX_FROM_BC,
    SYNC_GL_ACCOUNTS,
    SYNC_GL_ENTRIES,
    Threshold,
    User,
)
from app.services.erp.auto_sync_service import (
    acquire_sync_lock,
    calculate_incremental_date_range,
    get_eligible_business_central_entities,
    run_business_central_auto_sync_for_entity,
    run_auto_sync_tick,
)


_ENT = "ENT-AUTOSYNC-1"


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch) -> Session:
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == "admin@example.com").first()
        if admin is None:
            pytest.skip("conftest admin missing")
        if not db.query(Entity).filter(Entity.id == _ENT).first():
            db.add(Entity(
                id=_ENT, name="Auto-sync Test",
                region="North America", supervisor_id=admin.id,
                period_type="MoM",
                period_from="Feb 2026", period_to="Mar 2026",
            ))
        db.add(EntityErpMapping(
            entity_id=_ENT, erp_key="bc",
            status="connected", connected_by=admin.id,
        ))
        db.add(BusinessCentralCredential(
            entity_id=_ENT, erp_type="bc",
            auth_type="oauth2_client_credentials",
            tenant_id_encrypted="enc", client_id_encrypted="enc",
            client_secret_encrypted="enc",
            environment_name="prod",
            scope="https://api.businesscentral.dynamics.com/.default",
            base_url="https://api.businesscentral.dynamics.com",
            company_id="C1", company_name="Acme",
            status="verified",
        ))
        db.add(Threshold(
            entity_id=_ENT, statement="pl",
            mode="both", pct_value=Decimal("10"),
            abs_value=Decimal("100000"), logic="or",
        ))
        db.commit()
        yield db
    finally:
        # Always start teardown with a rollback so a half-flushed test
        # session doesn't poison the cleanup deletes.
        db.rollback()
        db.query(ErpSyncRun).filter(ErpSyncRun.entity_id == _ENT).delete()
        db.query(BusinessCentralCredential).filter(
            BusinessCentralCredential.entity_id == _ENT,
        ).delete()
        db.query(EntityErpMapping).filter(
            EntityErpMapping.entity_id == _ENT,
        ).delete()
        db.query(OntologyMapping).filter(
            OntologyMapping.entity_id == _ENT,
        ).delete()
        db.query(ErpGlAccount).filter(
            ErpGlAccount.entity_id == _ENT,
        ).delete()
        db.query(ErpGeneralLedgerEntry).filter(
            ErpGeneralLedgerEntry.entity_id == _ENT,
        ).delete()
        db.query(Threshold).filter(Threshold.entity_id == _ENT).delete()
        db.commit()
        db.close()


# ── Eligibility ────────────────────────────────────────────────────────────
def test_eligibility_includes_verified_entity(db_session: Session) -> None:
    eligible = get_eligible_business_central_entities(db_session)
    assert _ENT in eligible


def test_eligibility_skips_when_company_missing(db_session: Session) -> None:
    cred = db_session.query(BusinessCentralCredential).filter(
        BusinessCentralCredential.entity_id == _ENT,
    ).first()
    cred.company_id = None
    db_session.commit()
    eligible = get_eligible_business_central_entities(db_session)
    assert _ENT not in eligible


def test_eligibility_skips_when_credential_not_verified(db_session: Session) -> None:
    cred = db_session.query(BusinessCentralCredential).filter(
        BusinessCentralCredential.entity_id == _ENT,
    ).first()
    cred.status = "failed"
    db_session.commit()
    eligible = get_eligible_business_central_entities(db_session)
    assert _ENT not in eligible


# ── Lock ───────────────────────────────────────────────────────────────────
def test_acquire_sync_lock_blocks_concurrent_run(db_session: Session) -> None:
    first = acquire_sync_lock(
        db_session, entity_id=_ENT, sync_type=SYNC_AUTO_BC_SYNC,
        lock_minutes=60,
    )
    db_session.commit()
    assert first is not None
    second = acquire_sync_lock(
        db_session, entity_id=_ENT, sync_type=SYNC_AUTO_BC_SYNC,
        lock_minutes=60,
    )
    assert second is None


def test_acquire_sync_lock_ignores_stale_running_row(db_session: Session) -> None:
    stale = ErpSyncRun(
        entity_id=_ENT, erp_type="bc", sync_type=SYNC_AUTO_BC_SYNC,
        status="running",
        started_at=datetime.utcnow() - timedelta(hours=24),
    )
    db_session.add(stale)
    db_session.commit()
    new_run = acquire_sync_lock(
        db_session, entity_id=_ENT, sync_type=SYNC_AUTO_BC_SYNC,
        lock_minutes=60,
    )
    assert new_run is not None


# ── Incremental window ─────────────────────────────────────────────────────
def test_incremental_range_uses_last_successful_finish(db_session: Session) -> None:
    last_finish = datetime(2026, 5, 18, 1, 0, 0)
    db_session.add(ErpSyncRun(
        entity_id=_ENT, erp_type="bc", sync_type=SYNC_GL_ENTRIES,
        status="success",
        started_at=last_finish - timedelta(minutes=5),
        finished_at=last_finish, records_processed=10,
    ))
    db_session.commit()
    from_d, to_d = calculate_incremental_date_range(
        db_session, entity_id=_ENT, fallback_days=7,
    )
    assert from_d == date(2026, 5, 17)  # 1-day overlap
    assert to_d == date.today()


def test_incremental_range_falls_back_to_window_when_no_history(
    db_session: Session,
) -> None:
    from_d, to_d = calculate_incremental_date_range(
        db_session, entity_id=_ENT, fallback_days=7,
    )
    assert (to_d - from_d).days == 7


# ── Per-entity runner — patched BC client ──────────────────────────────────
class _FakeResult:
    def __init__(self, ok: bool, records: list[dict[str, Any]] | None = None,
                 code: str | None = None):
        self.ok = ok
        self.records = records or []
        from app.services.erp.safe_errors import safe_error
        self.error = safe_error(code) if code else None


def _patch_bc_client(monkeypatch: pytest.MonkeyPatch, *,
                     gl_records: list[dict] | None = None,
                     dim_records: list[dict] | None = None,
                     gle_records: list[dict] | None = None,
                     fail_code: str | None = None) -> None:
    """Patch every BC service call used by the orchestrator. Each helper
    returns a FakeResult — the replace helpers are real (they hit the
    test DB) so we exercise the upsert path too."""
    from app.services.erp import auto_sync_service as autosync_mod

    def fake_gl(_cred, _company): return _FakeResult(
        ok=fail_code is None,
        records=gl_records or [], code=fail_code,
    )
    def fake_dim(_cred, _company): return _FakeResult(
        ok=fail_code is None,
        records=dim_records or [], code=fail_code,
    )
    def fake_gle(_cred, _company, **_kw): return _FakeResult(
        ok=fail_code is None,
        records=gle_records or [], code=fail_code,
    )
    monkeypatch.setattr(autosync_mod, "svc_sync_gl_accounts", fake_gl)
    monkeypatch.setattr(autosync_mod, "svc_sync_dimensions", fake_dim)
    monkeypatch.setattr(autosync_mod,
                        "svc_sync_general_ledger_entries", fake_gle)


def test_run_for_entity_success_writes_per_layer_runs(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bc_client(
        monkeypatch,
        gl_records=[{"account_no": "4000", "account_name": "Sales", "company_id": "C1"}],
        dim_records=[],  # dimensions are optional and may be empty
        gle_records=[{
            "external_entry_no": "E1", "account_no": "4000",
            "amount": Decimal("1000"), "company_id": "C1",
            # Use a real `date` so SQLite's strict Date column accepts it.
            "posting_date": date(2026, 3, 5),
        }],
    )
    res = run_business_central_auto_sync_for_entity(
        db_session, entity_id=_ENT, force=True, actor_id=None,
    )
    assert res.ok is True
    assert res.skipped is False
    sync_types = {s.sync_type for s in res.steps}
    assert {SYNC_GL_ACCOUNTS, SYNC_DIMENSIONS, SYNC_GL_ENTRIES} <= sync_types
    # Aggregate row stamped success.
    aggregate = (
        db_session.query(ErpSyncRun)
        .filter(
            ErpSyncRun.entity_id == _ENT,
            ErpSyncRun.sync_type == SYNC_AUTO_BC_SYNC,
        )
        .order_by(ErpSyncRun.started_at.desc()).first()
    )
    assert aggregate is not None
    assert aggregate.status == "success"


def test_run_for_entity_records_failure_when_layer_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bc_client(monkeypatch, fail_code="BC_AUTH_FAILED")
    res = run_business_central_auto_sync_for_entity(
        db_session, entity_id=_ENT, force=True, actor_id=None,
    )
    assert res.ok is False
    assert any(not s.ok for s in res.steps)
    aggregate = (
        db_session.query(ErpSyncRun)
        .filter(
            ErpSyncRun.entity_id == _ENT,
            ErpSyncRun.sync_type == SYNC_AUTO_BC_SYNC,
        )
        .order_by(ErpSyncRun.started_at.desc()).first()
    )
    assert aggregate is not None
    assert aggregate.status == "failed"
    assert aggregate.safe_error_code is not None


def test_run_for_entity_triggers_flux_when_mapping_present(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bc_client(monkeypatch,
                     gl_records=[{"account_no": "4000", "company_id": "C1"}])
    db_session.add(OntologyMapping(
        entity_id=_ENT, statement="pl",
        tflux_caption="Revenue", erp_gl_json=["4000"], erp_subgl_json=[],
    ))
    db_session.commit()
    res = run_business_central_auto_sync_for_entity(
        db_session, entity_id=_ENT, force=True, actor_id=None,
    )
    flux_step = next((s for s in res.steps
                      if s.sync_type == SYNC_GENERATE_FLUX_FROM_BC), None)
    assert flux_step is not None
    assert flux_step.ok is True


def test_run_for_entity_warns_when_mapping_missing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bc_client(monkeypatch,
                     gl_records=[{"account_no": "4000", "company_id": "C1"}])
    res = run_business_central_auto_sync_for_entity(
        db_session, entity_id=_ENT, force=True, actor_id=None,
    )
    flux_step = next((s for s in res.steps
                      if s.sync_type == SYNC_GENERATE_FLUX_FROM_BC), None)
    assert flux_step is not None
    assert flux_step.ok is False
    assert flux_step.safe_error_code == "ONTOLOGY_MAPPING_MISSING"


# ── Tick-level ─────────────────────────────────────────────────────────────
def test_run_auto_sync_tick_disabled_returns_safe_error(
    db_session: Session,
) -> None:
    # Settings default to disabled; force=False here (the manual run-now
    # endpoint passes force=True via the scheduler).
    tick = run_auto_sync_tick(db_session, force=False)
    assert tick.enabled is False
    assert tick.safe_error_code == "BC_AUTO_SYNC_DISABLED"
    assert tick.results == []


def test_run_auto_sync_tick_force_runs_for_eligible_entity(
    db_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bc_client(monkeypatch,
                     gl_records=[{"account_no": "4000", "company_id": "C1"}])
    tick = run_auto_sync_tick(db_session, force=True)
    assert _ENT in tick.eligible_entity_ids
    assert any(r.entity_id == _ENT for r in tick.results)
