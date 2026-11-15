# @file    routers/admin/business_central_auto_sync.py
#
# Admin-only endpoints for the Business Central auto-sync.
#
#   POST /api/admin/integrations/business-central/auto-sync/run-now
#   GET  /api/admin/integrations/business-central/auto-sync/status
#
# These do NOT replace the manual sync buttons — those keep working via
# the existing routers. This module only exposes the orchestrator surface.

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.models import User
from app.services.erp.auto_sync_scheduler import bc_auto_sync_scheduler
from app.services.erp.auto_sync_service import (
    EntitySyncResult,
    StepResult,
    get_eligible_business_central_entities,
)


_log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_role("admin"))])


# ── Schemas ────────────────────────────────────────────────────────────
class AutoSyncRunNowRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    entity_id: Optional[str] = Field(default=None, alias="entityId")
    force: bool = False


class AutoSyncStepOut(BaseModel):
    sync_type: str
    ok: bool
    records_processed: int = 0
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None


class AutoSyncEntityResultOut(BaseModel):
    entity_id: str
    ok: bool
    skipped: bool
    skip_reason: Optional[str] = None
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    steps: List[AutoSyncStepOut] = []


class AutoSyncRunNowResponse(BaseModel):
    enabled: bool
    started_at: datetime
    finished_at: datetime
    eligible_entity_ids: List[str]
    results: List[AutoSyncEntityResultOut]
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None


class AutoSyncStatusResponse(BaseModel):
    enabled: bool
    cron: str
    timezone: str
    scheduler_running: bool
    next_run_at: Optional[datetime] = None
    last_started_at: Optional[datetime] = None
    last_finished_at: Optional[datetime] = None
    last_status: Optional[str] = None
    eligible_entities: int
    eligible_entity_ids: List[str] = []
    running_entities: List[str] = []
    last_results_summary: List[AutoSyncEntityResultOut] = []


# ── Mappers ────────────────────────────────────────────────────────────
def _step_to_out(step: StepResult) -> AutoSyncStepOut:
    return AutoSyncStepOut(
        sync_type=step.sync_type,
        ok=step.ok,
        records_processed=step.records_processed or 0,
        safe_error_code=step.safe_error_code,
        safe_error_message=step.safe_error_message,
    )


def _result_to_out(result: EntitySyncResult) -> AutoSyncEntityResultOut:
    return AutoSyncEntityResultOut(
        entity_id=result.entity_id,
        ok=result.ok,
        skipped=result.skipped,
        skip_reason=result.skip_reason,
        safe_error_code=result.safe_error_code,
        safe_error_message=result.safe_error_message,
        started_at=result.started_at,
        finished_at=result.finished_at,
        steps=[_step_to_out(s) for s in result.steps],
    )


# ── Endpoints ──────────────────────────────────────────────────────────
@router.post(
    "/auto-sync/run-now",
    response_model=AutoSyncRunNowResponse,
    summary="Manually trigger Business Central auto-sync",
    description=(
        "Runs the BC auto-sync orchestrator for one entity (when "
        "`entityId` is supplied) or every eligible entity. `force=true` "
        "ignores the duplicate-prevention lock window. Admin only."
    ),
    responses={
        404: {"description": "Target entity is not eligible for auto-sync"},
    },
)
def run_now(
    body: AutoSyncRunNowRequest,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AutoSyncRunNowResponse:
    # Validate the per-entity case eagerly so the API returns 404 rather
    # than 200 with a `skipped=true` payload — keeps API ergonomics close
    # to the manual sync routes.
    if body.entity_id:
        eligible = get_eligible_business_central_entities(db)
        if body.entity_id not in eligible and not body.force:
            raise HTTPException(
                status_code=404,
                detail={
                    "safe_error_code": "ENTITY_NOT_ELIGIBLE",
                    "safe_error_message":
                        "Entity is not eligible for Business Central auto-sync.",
                },
            )

    tick = bc_auto_sync_scheduler.run_now(
        target_entity_id=body.entity_id, force=body.force,
        actor_id=actor.id,
    )
    return AutoSyncRunNowResponse(
        enabled=tick.enabled,
        started_at=tick.started_at,
        finished_at=tick.finished_at,
        eligible_entity_ids=tick.eligible_entity_ids,
        results=[_result_to_out(r) for r in tick.results],
        safe_error_code=tick.safe_error_code,
        safe_error_message=tick.safe_error_message,
    )


@router.get(
    "/auto-sync/status",
    response_model=AutoSyncStatusResponse,
    summary="Business Central auto-sync status",
    description=(
        "Returns scheduler enabled/cron/timezone, last-tick summary, "
        "next scheduled run time, and the currently eligible entity IDs. "
        "Admin only. Does NOT trigger a sync."
    ),
)
def status(db: Session = Depends(get_db)) -> AutoSyncStatusResponse:
    snap = bc_auto_sync_scheduler.status()
    eligible = get_eligible_business_central_entities(db)
    last_results = snap.get("last_results_summary") or []
    running = [
        r["entity_id"] for r in last_results
        if isinstance(r, dict) and r.get("ok") is False and r.get("skipped") is False
    ]
    last_results_out: List[AutoSyncEntityResultOut] = []
    for r in last_results:
        if not isinstance(r, dict):
            continue
        # Only the small subset the admin status panel renders — `started_at`
        # and `steps` are not present on the snapshot, so we reuse the
        # current tick's times when available.
        last_results_out.append(AutoSyncEntityResultOut(
            entity_id=str(r.get("entity_id") or ""),
            ok=bool(r.get("ok") or False),
            skipped=bool(r.get("skipped") or False),
            skip_reason=r.get("skip_reason"),
            safe_error_code=r.get("safe_error_code"),
            safe_error_message=r.get("safe_error_message"),
            started_at=snap.get("last_started_at") or datetime.utcnow(),
            finished_at=snap.get("last_finished_at"),
            steps=[],
        ))
    return AutoSyncStatusResponse(
        enabled=bool(snap.get("enabled")),
        cron=str(snap.get("cron") or ""),
        timezone=str(snap.get("timezone") or "UTC"),
        scheduler_running=bool(snap.get("scheduler_running")),
        next_run_at=snap.get("next_run_at"),
        last_started_at=snap.get("last_started_at"),
        last_finished_at=snap.get("last_finished_at"),
        last_status=snap.get("last_status"),
        eligible_entities=len(eligible),
        eligible_entity_ids=eligible,
        running_entities=running,
        last_results_summary=last_results_out,
    )
