# @file    services/erp/auto_sync_service.py
#
# Orchestrator for the Business Central scheduled auto-sync.
#
# Reuses the existing service-layer functions
# (`svc_sync_gl_accounts`, `svc_sync_dimensions`, `svc_sync_general_ledger_entries`)
# and the existing replace helpers exposed from
# `app/routers/admin/business_central.py`. The orchestrator itself never
# talks to Business Central directly; it only sequences the calls, applies
# locking, persists `ErpSyncRun` rows, and triggers the optional
# Generate-Flux-from-BC step.
#
# Hard rules enforced here:
#   • Plaintext credentials never appear in logs or return values.
#   • Authorization headers are never logged.
#   • A failed step never aborts the whole tick — the orchestrator logs the
#     failure and continues to the next entity.
#   • Manual sync endpoints are NOT touched; this module only adds new
#     entry points.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import desc
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import (
    BusinessCentralCredential,
    Entity,
    EntityErpMapping,
    ErpSyncRun,
    OntologyMapping,
    SYNC_AUTO_BC_SYNC,
    SYNC_DIMENSIONS,
    SYNC_GENERATE_FLUX_FROM_BC,
    SYNC_GL_ACCOUNTS,
    SYNC_GL_ENTRIES,
)
from app.services.erp.business_central import (
    sync_dimensions as svc_sync_dimensions,
    sync_general_ledger_entries as svc_sync_general_ledger_entries,
    sync_gl_accounts as svc_sync_gl_accounts,
)
from app.services.erp.safe_errors import (
    BC_AUTO_SYNC_DISABLED,
    BC_SERVICE_ERROR,
    COMPANY_NOT_SELECTED,
    CREDENTIAL_NOT_VERIFIED,
    DATABASE_ERROR,
    ENTITY_NOT_ELIGIBLE,
    FLUX_GENERATION_FAILED,
    ONTOLOGY_MAPPING_MISSING,
    SYNC_ALREADY_RUNNING,
    SafeError,
    safe_error,
)


_log = logging.getLogger(__name__)

ERP_TYPE_BC = "bc"


# ── Public result types ────────────────────────────────────────────────────
@dataclass
class StepResult:
    """One layer of the orchestrator pipeline (gl/dim/txn/flux)."""
    sync_type: str
    ok: bool
    records_processed: int = 0
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None


@dataclass
class EntitySyncResult:
    """Aggregate outcome for one entity in one orchestrator tick.
    ``ok`` is True only when every required step succeeded."""
    entity_id: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    ok: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None
    steps: list[StepResult] = field(default_factory=list)

    def step_summary(self) -> dict[str, int]:
        return {s.sync_type: s.records_processed for s in self.steps}


# ── Eligibility ────────────────────────────────────────────────────────────
def get_eligible_business_central_entities(
    db: Session, *, max_entities: Optional[int] = None,
) -> list[str]:
    """Return entity IDs that pass every gate the auto-sync requires:

      • mapped to BC via ``entity_erp_mappings.erp_key='bc'``,
      • entity is_active (when the column exists; defaulted to True),
      • BC credential row present with status verified/syncing/synced,
      • active company selected on the credential.
    """
    cap = max_entities if max_entities and max_entities > 0 else None
    q = (
        db.query(Entity.id, BusinessCentralCredential)
        .join(EntityErpMapping, EntityErpMapping.entity_id == Entity.id)
        .join(
            BusinessCentralCredential,
            BusinessCentralCredential.entity_id == Entity.id,
        )
        .filter(EntityErpMapping.erp_key == ERP_TYPE_BC)
    )
    eligible: list[str] = []
    for entity_id, cred in q.all():
        if cred.status not in ("verified", "syncing", "synced"):
            continue
        if not cred.company_id:
            continue
        # Skip explicitly disabled entities — `is_active` is the canonical
        # column from the Entity model. Defaults to True when missing.
        ent = db.query(Entity).filter(Entity.id == entity_id).first()
        if ent and getattr(ent, "is_active", True) is False:
            continue
        eligible.append(entity_id)
        if cap is not None and len(eligible) >= cap:
            break
    return eligible


def _ineligibility_reason(
    db: Session, entity_id: str,
) -> Optional[SafeError]:
    """Return the most specific reason ``entity_id`` would be rejected by
    :func:`get_eligible_business_central_entities`, or ``None`` if it
    qualifies."""
    mapping = (
        db.query(EntityErpMapping)
        .filter(
            EntityErpMapping.entity_id == entity_id,
            EntityErpMapping.erp_key == ERP_TYPE_BC,
        )
        .first()
    )
    if mapping is None:
        return safe_error(ENTITY_NOT_ELIGIBLE)
    cred = (
        db.query(BusinessCentralCredential)
        .filter(BusinessCentralCredential.entity_id == entity_id)
        .first()
    )
    if cred is None or cred.status not in ("verified", "syncing", "synced"):
        return safe_error(CREDENTIAL_NOT_VERIFIED)
    if not cred.company_id:
        return safe_error(COMPANY_NOT_SELECTED)
    return None


# ── Locking ────────────────────────────────────────────────────────────────
def _running_sync_within_window(
    db: Session, entity_id: str, *, lock_minutes: int,
) -> Optional[ErpSyncRun]:
    """Return a ``running`` sync run for ``entity_id`` whose ``started_at``
    is newer than the lock window. Stale ``running`` rows older than the
    window are ignored so a crashed worker doesn't lock the entity
    forever.
    """
    if lock_minutes <= 0:
        return None
    cutoff = datetime.utcnow() - timedelta(minutes=lock_minutes)
    return (
        db.query(ErpSyncRun)
        .filter(
            ErpSyncRun.entity_id == entity_id,
            ErpSyncRun.erp_type == ERP_TYPE_BC,
            ErpSyncRun.status == "running",
            ErpSyncRun.started_at >= cutoff,
        )
        .order_by(desc(ErpSyncRun.started_at))
        .first()
    )


def acquire_sync_lock(
    db: Session, *, entity_id: str, sync_type: str = SYNC_AUTO_BC_SYNC,
    lock_minutes: Optional[int] = None,
) -> Optional[ErpSyncRun]:
    """Insert a ``running`` ErpSyncRun row only when no other live run
    exists. Returns the new row on success, or ``None`` when the entity
    is already locked. The caller must commit; the lock is released by
    flipping ``status`` via :func:`release_sync_lock`.
    """
    lock_window = lock_minutes if lock_minutes is not None \
        else settings.BC_AUTO_SYNC_LOCK_MINUTES
    if _running_sync_within_window(db, entity_id, lock_minutes=lock_window):
        return None
    run = ErpSyncRun(
        entity_id=entity_id, erp_type=ERP_TYPE_BC, sync_type=sync_type,
        status="running", started_at=datetime.utcnow(),
        created_by=None,
    )
    db.add(run)
    db.flush()
    return run


def release_sync_lock(
    run: ErpSyncRun, *, ok: bool,
    records_processed: Optional[int] = None,
    error: Optional[SafeError] = None,
) -> None:
    """Finalise the orchestrator's aggregate row. Idempotent."""
    if run.finished_at is not None and run.status != "running":
        return
    run.finished_at = datetime.utcnow()
    run.status = "success" if ok else "failed"
    run.records_processed = records_processed
    if error is not None:
        run.safe_error_code = error.code
        run.safe_error_message = error.message


# ── Per-step helpers ───────────────────────────────────────────────────────
def calculate_incremental_date_range(
    db: Session, *, entity_id: str,
    fallback_days: Optional[int] = None,
) -> tuple[date, date]:
    """Return ``(from_date, to_date)`` for an incremental GL entry sync.

    Strategy:
      • ``to_date`` is always today (UTC).
      • ``from_date`` defaults to the last successful ``sync_gl_entries``
        run's ``finished_at`` minus a small overlap. When no successful
        run is on file, fall back to today minus
        ``BC_AUTO_SYNC_INCREMENTAL_DAYS``.
    """
    today = date.today()
    days = fallback_days if fallback_days and fallback_days > 0 \
        else settings.BC_AUTO_SYNC_INCREMENTAL_DAYS
    fallback_from = today - timedelta(days=max(days, 1))

    last = (
        db.query(ErpSyncRun)
        .filter(
            ErpSyncRun.entity_id == entity_id,
            ErpSyncRun.erp_type == ERP_TYPE_BC,
            ErpSyncRun.sync_type == SYNC_GL_ENTRIES,
            ErpSyncRun.status == "success",
        )
        .order_by(desc(ErpSyncRun.finished_at))
        .first()
    )
    if last and last.finished_at:
        # 1-day overlap so a row dated exactly on the previous run boundary
        # never falls through the cracks. Never roll forward beyond today.
        candidate = last.finished_at.date() - timedelta(days=1)
        if candidate < today:
            return candidate, today
    return fallback_from, today


def _record_step(
    db: Session, *, entity_id: str, sync_type: str,
    ok: bool, records_processed: Optional[int],
    error: Optional[SafeError],
) -> ErpSyncRun:
    """Persist a per-layer ``ErpSyncRun`` (gl/dim/txn/flux). Used both on
    success and failure so the diagnostics endpoint sees every step."""
    started = datetime.utcnow()
    run = ErpSyncRun(
        entity_id=entity_id, erp_type=ERP_TYPE_BC, sync_type=sync_type,
        status="success" if ok else "failed",
        started_at=started, finished_at=started,
        records_processed=records_processed if records_processed is not None else 0,
        safe_error_code=error.code if error else None,
        safe_error_message=error.message if error else None,
        created_by=None,
    )
    db.add(run)
    db.flush()
    return run


# Replace helper signatures we depend on. They live in
# `routers/admin/business_central.py` to avoid duplicating the upsert SQL.
# We import them lazily inside the runner to dodge the router-vs-service
# circular import at module load time.
def _import_replace_helpers() -> tuple[
    Callable[..., int], Callable[..., int], Callable[..., int],
]:
    from app.routers.admin.business_central import (
        _replace_accounts, _replace_dimensions, _replace_gl_entries,
    )
    return _replace_accounts, _replace_dimensions, _replace_gl_entries


def _run_layer_gl_accounts(
    db: Session, cred: BusinessCentralCredential, replace: Callable[..., int],
) -> StepResult:
    result = svc_sync_gl_accounts(cred, cred.company_id)
    if not result.ok:
        err = result.error or safe_error(BC_SERVICE_ERROR)
        return StepResult(SYNC_GL_ACCOUNTS, False, 0, err.code, err.message)
    count = replace(db, cred.entity_id, cred.company_id, result.records)
    return StepResult(SYNC_GL_ACCOUNTS, True, count)


def _run_layer_dimensions(
    db: Session, cred: BusinessCentralCredential, replace: Callable[..., int],
) -> StepResult:
    result = svc_sync_dimensions(cred, cred.company_id)
    if not result.ok:
        err = result.error or safe_error(BC_SERVICE_ERROR)
        return StepResult(SYNC_DIMENSIONS, False, 0, err.code, err.message)
    count = replace(db, cred.entity_id, cred.company_id, result.records)
    return StepResult(SYNC_DIMENSIONS, True, count)


def _run_layer_gl_entries(
    db: Session, cred: BusinessCentralCredential,
    replace: Callable[..., int],
) -> StepResult:
    from_d, to_d = calculate_incremental_date_range(
        db, entity_id=cred.entity_id,
    )
    result = svc_sync_general_ledger_entries(
        cred, cred.company_id,
        from_date=from_d.isoformat(), to_date=to_d.isoformat(),
    )
    if not result.ok:
        err = result.error or safe_error(BC_SERVICE_ERROR)
        return StepResult(SYNC_GL_ENTRIES, False, 0, err.code, err.message)
    count = replace(db, cred.entity_id, cred.company_id, result.records)
    return StepResult(SYNC_GL_ENTRIES, True, count)


def _run_layer_generate_flux(
    db: Session, entity: Entity,
) -> StepResult:
    """Optional final step. Triggered only when ontology mappings exist
    for the entity. Returns a `ONTOLOGY_MAPPING_MISSING` warning otherwise
    so the admin diagnostics surface a clear reason."""
    has_mapping = (
        db.query(OntologyMapping)
        .filter(OntologyMapping.entity_id == entity.id)
        .first()
        is not None
    )
    if not has_mapping:
        err = safe_error(ONTOLOGY_MAPPING_MISSING)
        return StepResult(
            SYNC_GENERATE_FLUX_FROM_BC, False, 0, err.code, err.message,
        )
    try:
        # Local imports — keep heavy modules lazy so the scheduler can
        # boot even when MySQL is briefly unreachable.
        from app.services.flux_generation import (
            preview_flux_generation, upsert_bc_generated_flux_rows,
        )
        rows, _diag = preview_flux_generation(db, entity, "both")
        inserted, updated, _skipped = upsert_bc_generated_flux_rows(
            db, entity, rows, overwrite_existing=True,
        )
        return StepResult(
            SYNC_GENERATE_FLUX_FROM_BC, True,
            records_processed=inserted + updated,
        )
    except SQLAlchemyError:
        _log.exception("auto-sync: flux generation DB error for %s", entity.id)
        err = safe_error(FLUX_GENERATION_FAILED)
        return StepResult(SYNC_GENERATE_FLUX_FROM_BC, False, 0,
                          err.code, err.message)
    except Exception:  # pragma: no cover — defensive
        _log.exception("auto-sync: flux generation crashed for %s", entity.id)
        err = safe_error(FLUX_GENERATION_FAILED)
        return StepResult(SYNC_GENERATE_FLUX_FROM_BC, False, 0,
                          err.code, err.message)


# ── Public per-entity orchestrator ─────────────────────────────────────────
def run_business_central_auto_sync_for_entity(
    db: Session,
    *,
    entity_id: str,
    force: bool = False,
    actor_id: Optional[int] = None,
) -> EntitySyncResult:
    """Run the BC auto-sync pipeline for one entity.

    Returns an :class:`EntitySyncResult` with per-step counts and a single
    aggregate status. The function never raises — every failure path
    becomes an entry on the result object so the scheduler / admin route
    can treat the call as a value-returning function.
    """
    started = datetime.utcnow()
    result = EntitySyncResult(entity_id=entity_id, started_at=started)

    # Eligibility gate
    reason = _ineligibility_reason(db, entity_id)
    if reason is not None:
        result.skipped = True
        result.skip_reason = reason.code
        result.safe_error_code = reason.code
        result.safe_error_message = reason.message
        result.finished_at = datetime.utcnow()
        return result

    # Lock
    lock_minutes = 0 if force else settings.BC_AUTO_SYNC_LOCK_MINUTES
    aggregate_run = acquire_sync_lock(
        db, entity_id=entity_id, sync_type=SYNC_AUTO_BC_SYNC,
        lock_minutes=lock_minutes,
    )
    if aggregate_run is None:
        err = safe_error(SYNC_ALREADY_RUNNING)
        result.skipped = True
        result.skip_reason = err.code
        result.safe_error_code = err.code
        result.safe_error_message = err.message
        result.finished_at = datetime.utcnow()
        return result
    aggregate_run.created_by = actor_id
    db.commit()

    # Run layers — each step persists its own ErpSyncRun row.
    cred = (
        db.query(BusinessCentralCredential)
        .filter(BusinessCentralCredential.entity_id == entity_id)
        .first()
    )
    entity = db.query(Entity).filter(Entity.id == entity_id).first()
    if cred is None or entity is None:
        # Should be impossible after the eligibility gate, but guard so we
        # never NPE inside the layer runner.
        err = safe_error(ENTITY_NOT_ELIGIBLE)
        release_sync_lock(aggregate_run, ok=False, error=err)
        db.commit()
        result.ok = False
        result.safe_error_code = err.code
        result.safe_error_message = err.message
        result.finished_at = aggregate_run.finished_at
        return result

    replace_accounts, replace_dimensions, replace_gl_entries = _import_replace_helpers()

    layer_runners: list[Callable[[], StepResult]] = [
        lambda: _run_layer_gl_accounts(db, cred, replace_accounts),
        lambda: _run_layer_dimensions(db, cred, replace_dimensions),
        lambda: _run_layer_gl_entries(db, cred, replace_gl_entries),
    ]

    all_layers_ok = True
    total_records = 0
    for runner in layer_runners:
        step = runner()
        result.steps.append(step)
        _record_step(
            db, entity_id=entity_id, sync_type=step.sync_type,
            ok=step.ok, records_processed=step.records_processed,
            error=safe_error(step.safe_error_code, step.safe_error_message)
                  if step.safe_error_code else None,
        )
        db.commit()
        if not step.ok:
            all_layers_ok = False
        total_records += step.records_processed or 0

    # Optional flux step — only when the three core layers succeeded.
    if all_layers_ok:
        flux_step = _run_layer_generate_flux(db, entity)
        result.steps.append(flux_step)
        _record_step(
            db, entity_id=entity_id, sync_type=flux_step.sync_type,
            ok=flux_step.ok, records_processed=flux_step.records_processed,
            error=safe_error(flux_step.safe_error_code,
                             flux_step.safe_error_message)
                  if flux_step.safe_error_code else None,
        )
        db.commit()

    # Aggregate close-out
    aggregate_error: Optional[SafeError] = None
    if not all_layers_ok:
        # Pick the first non-OK layer's error for the aggregate row.
        first_failure = next((s for s in result.steps if not s.ok), None)
        if first_failure and first_failure.safe_error_code:
            aggregate_error = safe_error(
                first_failure.safe_error_code, first_failure.safe_error_message,
            )
    release_sync_lock(
        aggregate_run, ok=all_layers_ok,
        records_processed=total_records, error=aggregate_error,
    )
    db.commit()

    result.ok = all_layers_ok
    result.finished_at = aggregate_run.finished_at
    if aggregate_error is not None:
        result.safe_error_code = aggregate_error.code
        result.safe_error_message = aggregate_error.message

    notify_auto_sync_result(db, result)
    return result


# ── Notifications ──────────────────────────────────────────────────────────
def notify_auto_sync_result(db: Session, result: EntitySyncResult) -> None:
    """Best-effort notification to admins. Never raises."""
    try:
        from app.services.notification_service import (
            NOTIF_BC_SYNC_FAILED, notify_admins,
        )
        entity = db.query(Entity).filter(Entity.id == result.entity_id).first()
        entity_name = entity.name if entity else result.entity_id
        if not result.ok:
            reason = (result.safe_error_message or "Unknown error")[:200]
            notify_admins(
                db,
                notif_type=NOTIF_BC_SYNC_FAILED,
                title="BC auto-sync failed",
                message=(
                    f"Business Central auto sync failed for {entity_name}: "
                    f"{reason}"
                ),
                actor_id=None,
                entity_id=result.entity_id,
                link_url=f"/admin/integrations?entity={result.entity_id}",
                skip_self=False,
            )
            return
        # Success notification — short summary line.
        gl  = next((s.records_processed for s in result.steps
                    if s.sync_type == SYNC_GL_ACCOUNTS), 0)
        dim = next((s.records_processed for s in result.steps
                    if s.sync_type == SYNC_DIMENSIONS), 0)
        txn = next((s.records_processed for s in result.steps
                    if s.sync_type == SYNC_GL_ENTRIES), 0)
        notify_admins(
            db,
            notif_type=NOTIF_BC_SYNC_FAILED,   # reused channel — no new enum needed
            title="BC sync completed",
            message=(
                f"Business Central sync completed for {entity_name}. "
                f"GL: {gl}, Dimensions: {dim}, Transactions: {txn}."
            ),
            actor_id=None,
            entity_id=result.entity_id,
            link_url=f"/admin/integrations?entity={result.entity_id}",
            skip_self=False,
        )
    except Exception:
        _log.warning("auto-sync notification skipped (non-blocking)",
                     exc_info=False)


# ── Tick orchestrator ──────────────────────────────────────────────────────
@dataclass
class AutoSyncTickResult:
    started_at: datetime
    finished_at: datetime
    enabled: bool
    eligible_entity_ids: list[str]
    results: list[EntitySyncResult]
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None


def run_auto_sync_tick(
    db: Session, *,
    target_entity_id: Optional[str] = None,
    force: bool = False,
    actor_id: Optional[int] = None,
) -> AutoSyncTickResult:
    """Single orchestrator pass.

    When ``target_entity_id`` is supplied we run the pipeline for that
    entity only — used by the manual run-now endpoint.
    Otherwise we fan out across :func:`get_eligible_business_central_entities`.
    """
    started = datetime.utcnow()
    if not settings.BC_AUTO_SYNC_ENABLED and not force:
        err = safe_error(BC_AUTO_SYNC_DISABLED)
        return AutoSyncTickResult(
            started_at=started, finished_at=datetime.utcnow(),
            enabled=False, eligible_entity_ids=[], results=[],
            safe_error_code=err.code, safe_error_message=err.message,
        )

    if target_entity_id:
        eligible = [target_entity_id]
    else:
        eligible = get_eligible_business_central_entities(
            db, max_entities=settings.BC_AUTO_SYNC_MAX_ENTITIES_PER_RUN,
        )

    results: list[EntitySyncResult] = []
    for eid in eligible:
        try:
            results.append(run_business_central_auto_sync_for_entity(
                db, entity_id=eid, force=force, actor_id=actor_id,
            ))
        except SQLAlchemyError:
            _log.exception("auto-sync DB error for %s", eid)
            db.rollback()
            err = safe_error(DATABASE_ERROR)
            results.append(EntitySyncResult(
                entity_id=eid, started_at=datetime.utcnow(),
                finished_at=datetime.utcnow(), ok=False, skipped=False,
                safe_error_code=err.code, safe_error_message=err.message,
            ))
        except Exception:  # pragma: no cover - defensive
            _log.exception("auto-sync crashed for %s", eid)
            db.rollback()
            err = safe_error(BC_SERVICE_ERROR)
            results.append(EntitySyncResult(
                entity_id=eid, started_at=datetime.utcnow(),
                finished_at=datetime.utcnow(), ok=False, skipped=False,
                safe_error_code=err.code, safe_error_message=err.message,
            ))

    return AutoSyncTickResult(
        started_at=started, finished_at=datetime.utcnow(),
        enabled=settings.BC_AUTO_SYNC_ENABLED,
        eligible_entity_ids=eligible, results=results,
    )
