# @file    profile.py

from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import get_current_user, require_role, require_entity_access
from app.db.session import get_db
from app.models.models import CompanyProfile, Threshold, Entity, DelegationLog, User
from app.schemas.schemas import (
    CompanyProfileOut, CompanyProfileUpdate,
    ThresholdOut, ThresholdUpdate,
    DelegateCreate, PeriodUpdate,
)
from app.services.flux_period_config import build_period_range_key

router = APIRouter()


# ── Company Profile ──────────────────────────────────────────
@router.get(
    "/{entity_id}/profile",
    response_model=CompanyProfileOut,
    dependencies=[Depends(require_entity_access())],
    summary="Get company profile",
    description="Auto-creates an empty profile row if missing.",
)
def get_profile(entity_id: str, db: Session = Depends(get_db)):
    p = db.query(CompanyProfile).filter(CompanyProfile.entity_id == entity_id).first()
    if not p:
        # Auto-create empty so the supervisor form has something to edit
        p = CompanyProfile(entity_id=entity_id)
        db.add(p)
        db.commit()
        db.refresh(p)
    return p


@router.put(
    "/{entity_id}/profile",
    response_model=CompanyProfileOut,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Update company profile",
)
def update_profile(
    entity_id: str, body: CompanyProfileUpdate, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    p = db.query(CompanyProfile).filter(CompanyProfile.entity_id == entity_id).first()
    if not p:
        p = CompanyProfile(entity_id=entity_id)
        db.add(p)
    before = {f: getattr(p, f) for f in ("industry","geography","revenue_range","entity_type","brief")}
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(p, field, value)
    log_change(db, actor=user, action="profile.update",
               table_name="company_profiles", row_pk=entity_id,
               before=before, after=body.model_dump(exclude_none=True),
               request=request)
    db.commit()
    db.refresh(p)
    return p


# ── Thresholds ───────────────────────────────────────────────
@router.get(
    "/{entity_id}/thresholds",
    response_model=List[ThresholdOut],
    dependencies=[Depends(require_entity_access())],
    summary="List materiality thresholds",
)
def get_thresholds(entity_id: str, db: Session = Depends(get_db)):
    return db.query(Threshold).filter(Threshold.entity_id == entity_id).all()


@router.put(
    "/{entity_id}/thresholds",
    response_model=ThresholdOut,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Upsert threshold row",
    description="One row per entity + statement (`pl` or `bs`).",
)
def update_thresholds(
    entity_id: str, body: ThresholdUpdate, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    t = db.query(Threshold).filter(
        Threshold.entity_id == entity_id,
        Threshold.statement == body.statement,
    ).first()
    if not t:
        t = Threshold(entity_id=entity_id, statement=body.statement)
        db.add(t)
    before = {f: getattr(t, f, None) for f in ("mode","pct_value","abs_value","logic","apply_all")}
    t.mode      = body.mode
    t.pct_value = body.pct_value
    t.abs_value = body.abs_value
    t.logic     = body.logic
    t.apply_all = body.apply_all
    log_change(db, actor=user, action="threshold.update",
               table_name="thresholds", row_pk=f"{entity_id}/{body.statement}",
               before=before, after=body.model_dump(),
               request=request)
    db.commit()
    db.refresh(t)
    return t


# ── Periods ───────────────────────────────────────────────────
@router.put(
    "/{entity_id}/periods",
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Update entity reporting period",
    responses={404: {"description": "Entity not found"}},
)
def update_periods(
    entity_id: str, body: PeriodUpdate, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    before = {"period_type": e.period_type, "period_from": e.period_from, "period_to": e.period_to}
    e.period_type = body.period_type
    e.period_from = build_period_range_key(body.period_from)
    e.period_to   = build_period_range_key(body.period_to)
    if not e.period_from or not e.period_to:
        raise HTTPException(
            status_code=422,
            detail={
                "safe_error_code": "PERIOD_CONFIG_INVALID",
                "safe_error_message": (
                    "Period configuration is invalid. Select at least one "
                    "month for current and comparison."
                ),
            },
        )
    after = {
        "period_type": e.period_type,
        "period_from": e.period_from,
        "period_to": e.period_to,
    }
    try:
        log_change(db, actor=user, action="period.update",
                   table_name="entities", row_pk=entity_id,
                   before=before, after=after,
                   request=request)
        db.commit()
    except DataError:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail={
                "safe_error_code": "PERIOD_SAVE_FAILED",
                "safe_error_message": (
                    "Period configuration could not be saved. Use a compact "
                    "month range and try again."
                ),
            },
        ) from None
    return {"entity_id": entity_id, **after}


# ── Delegate ──────────────────────────────────────────────────
@router.post(
    "/{entity_id}/delegate",
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Delegate entity supervisor",
    description="Appends **delegation_logs** row and sets entity `supervisor_id` to target user.",
)
def delegate(
    entity_id: str, body: DelegateCreate, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    log = DelegationLog(
        entity_id    = entity_id,
        from_user_id = user.id,
        to_user_id   = body.to_user_id,
        note         = body.note,
    )
    db.add(log)
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if e:
        e.supervisor_id = body.to_user_id
    log_change(db, actor=user, action="entity.delegate",
               table_name="entities", row_pk=entity_id,
               after={"to_user_id": body.to_user_id, "note": body.note}, request=request)
    db.commit()
    return {"delegated": True, "to_user_id": body.to_user_id}
