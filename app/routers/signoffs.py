# @file    signoffs.py

from typing import List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import get_current_user, require_role, require_entity_access
from app.db.session import get_db
from app.models.models import Signoff, CaptionVersion, FluxData, Entity, User
from app.schemas.schemas import SignoffOut, BulkSignoffCreate

router = APIRouter()


def _get_cap_version(db: Session, entity_id: str, caption: str) -> int:
    cv = db.query(CaptionVersion).filter(
        CaptionVersion.entity_id == entity_id,
        CaptionVersion.caption   == caption,
    ).first()
    return cv.version if cv else 0


@router.get(
    "/{entity_id}/signoffs",
    response_model=List[SignoffOut],
    dependencies=[Depends(require_entity_access())],
    summary="List caption sign-offs",
    description="Recomputes **is_stale** vs current caption version and persists updates.",
)
def get_signoffs(entity_id: str, db: Session = Depends(get_db)):
    signoffs = db.query(Signoff).filter(Signoff.entity_id == entity_id).all()
    for s in signoffs:
        current_v = _get_cap_version(db, entity_id, s.caption)
        s.is_stale = s.version != current_v
    db.commit()
    return signoffs


@router.post(
    "/{entity_id}/signoff/{caption}",
    response_model=SignoffOut,
    status_code=201,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Sign off one caption",
    description="Creates or updates sign-off at current caption version.",
)
def sign_caption(
    entity_id: str,
    caption: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_v = _get_cap_version(db, entity_id, caption)
    existing = db.query(Signoff).filter(
        Signoff.entity_id == entity_id,
        Signoff.caption   == caption,
    ).first()
    if existing:
        existing.signed_by = user.id
        existing.signed_at = datetime.utcnow()
        existing.version   = current_v
        existing.is_stale  = False
        out = existing
    else:
        out = Signoff(entity_id=entity_id, caption=caption, signed_by=user.id, version=current_v)
        db.add(out)

    log_change(db, actor=user, action="signoff.sign",
               table_name="signoffs", row_pk=f"{entity_id}/{caption}",
               after={"version": current_v}, request=request)
    db.commit()
    db.refresh(out)
    return out


@router.delete(
    "/{entity_id}/signoff/{caption}",
    status_code=204,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Clear sign-off for a caption",
)
def clear_signoff(
    entity_id: str, caption: str, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    s = db.query(Signoff).filter(
        Signoff.entity_id == entity_id,
        Signoff.caption   == caption,
    ).first()
    if s:
        log_change(db, actor=user, action="signoff.clear",
                   table_name="signoffs", row_pk=f"{entity_id}/{caption}",
                   before={"version": s.version}, request=request)
        db.delete(s)
        db.commit()


@router.post(
    "/{entity_id}/signoff/bulk",
    status_code=201,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Bulk sign-off by statement",
    description="Signs every flux caption row for `pl` or `bs` in the entity’s current period.",
    responses={404: {"description": "Entity not found"}},
)
def bulk_signoff(
    entity_id: str, body: BulkSignoffCreate, request: Request,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    captions = (
        db.query(FluxData.caption)
        .filter(
            FluxData.entity_id   == entity_id,
            FluxData.statement   == body.statement,
            FluxData.period_from == e.period_from,
            FluxData.period_to   == e.period_to,
        ).all()
    )
    signed: list[str] = []
    for (caption,) in captions:
        current_v = _get_cap_version(db, entity_id, caption)
        existing = db.query(Signoff).filter(
            Signoff.entity_id == entity_id,
            Signoff.caption   == caption,
        ).first()
        if existing:
            existing.signed_by = user.id
            existing.signed_at = datetime.utcnow()
            existing.version   = current_v
            existing.is_stale  = False
        else:
            db.add(Signoff(entity_id=entity_id, caption=caption,
                           signed_by=user.id, version=current_v))
        signed.append(caption)
    log_change(db, actor=user, action="signoff.bulk",
               table_name="signoffs", row_pk=entity_id,
               after={"statement": body.statement, "count": len(signed)},
               request=request)
    db.commit()
    return {"signed": signed, "count": len(signed)}
