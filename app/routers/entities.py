# @file    entities.py

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import (
    get_current_user, require_role, require_entity_access,
    visible_entity_ids, can_access_entity,
)
from app.db.session import get_db
from app.models.models import (
    Entity, User, EntityAssignment, MacroFactor, UserEntityAccess,
)
from app.schemas.schemas import EntityOut, EntityListItem, EntityStatusUpdate, UserOut
from app.services.flux_period_config import parse_flux_period_config

router = APIRouter()


def _period_labels(e: Entity) -> tuple[Optional[str], Optional[str]]:
    try:
        period = parse_flux_period_config(e)
    except ValueError:
        return None, None
    return period.current_label, period.comparison_label


def _review_progress(db: Session, e: Entity) -> tuple[int, int, int]:
    """Best-effort review completion for the queue cards.

    Returns ``(caption_count, signed_count, progress_pct)``. This is purely
    additive UI metadata — any failure degrades to ``(0, 0, 0)`` so the entity
    list endpoint (used across the whole app) can never break because of it.
    """
    try:
        from app.models.models import FluxData, Signoff

        caption_count = (
            db.query(FluxData.caption)
            .filter(
                FluxData.entity_id == e.id,
                FluxData.period_from == e.period_from,
                FluxData.period_to == e.period_to,
            )
            .distinct()
            .count()
        )
        if caption_count <= 0:
            return 0, 0, 0
        signed_count = db.query(Signoff).filter(Signoff.entity_id == e.id).count()
        signed_count = max(0, min(signed_count, caption_count))
        progress = round(signed_count / caption_count * 100)
        return caption_count, signed_count, max(0, min(100, progress))
    except Exception:
        return 0, 0, 0


def _to_list_item(db: Session, e: Entity) -> EntityListItem:
    assignee_ids = [a.assignee_id for a in db.query(EntityAssignment.assignee_id)
                    .filter(EntityAssignment.entity_id == e.id, EntityAssignment.is_active == True).distinct().all()] or []
    # Fall back to UserEntityAccess if no caption-level assignments
    if not assignee_ids:
        assignee_ids = [r.user_id for r in db.query(UserEntityAccess).filter(UserEntityAccess.entity_id == e.id).all()]
    assignees = db.query(User).filter(User.id.in_(assignee_ids)).all() if assignee_ids else []
    macro_count = db.query(MacroFactor).filter(MacroFactor.entity_id == e.id).count()
    from_lbl, to_lbl = _period_labels(e)
    caption_count, signed_count, progress = _review_progress(db, e)
    return EntityListItem(
        id=e.id, name=e.name, region=e.region, status=e.status, tags=e.tags or [],
        period_type=e.period_type, period_from=e.period_from, period_to=e.period_to,
        period_from_label=from_lbl, period_to_label=to_lbl,
        sub_due_date=e.sub_due_date, rev_due_date=e.rev_due_date,
        approval_level=e.approval_level,
        supervisor=UserOut.model_validate(e.supervisor),
        director=UserOut.model_validate(e.director) if e.director else None,
        currency=e.currency, fiscal_year_end=e.fiscal_year_end, tax_id=e.tax_id,
        is_active=e.is_active,
        assignees=[UserOut.model_validate(u) for u in assignees],
        macro_factor_count=macro_count,
        caption_count=caption_count,
        signed_count=signed_count,
        review_progress=progress,
    )


@router.get(
    "",
    response_model=List[EntityListItem],
    summary="List legal entities",
    description="Entities visible to the caller (analysts: assigned / `user_entity_access` only). Filter with optional `region`, `tag`, `status` query params.",
    responses={401: {"description": "Not authenticated"}},
)
def list_entities(
    region: Optional[str] = Query(None),
    tag: Optional[str]    = Query(None),
    status: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(Entity).filter(Entity.is_active == True)

    visible = visible_entity_ids(db, user)
    if visible is not None:
        q = q.filter(Entity.id.in_(visible))
    if region:
        q = q.filter(Entity.region == region)
    if status:
        q = q.filter(Entity.status == status)

    entities = q.all()
    if tag:
        entities = [e for e in entities if tag in (e.tags or [])]
    return [_to_list_item(db, e) for e in entities]


@router.get(
    "/{entity_id}",
    response_model=EntityListItem,
    summary="Get one entity",
    description="Full entity card including supervisor, director, assignees, macro factor count.",
    responses={403: {"description": "No access to entity"}, 404: {"description": "Entity not found"}},
)
def get_entity(
    entity_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    if not can_access_entity(db, user, entity_id):
        raise HTTPException(status_code=403, detail="No access")
    return _to_list_item(db, e)


@router.patch(
    "/{entity_id}/status",
    response_model=EntityListItem,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Update entity workflow status",
    description="Sets `status` to one of: assigned, wip, under_review, rejected, approved, pending_director. Writes **audit_logs**.",
    responses={400: {"description": "Invalid status value"}, 404: {"description": "Entity not found"}},
)
def update_entity_status(
    entity_id: str,
    body: EntityStatusUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    allowed = {"assigned", "wip", "under_review", "rejected", "approved", "pending_director"}
    if body.status not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid status. Allowed: {allowed}")
    before = {"status": e.status}
    e.status = body.status
    log_change(db, actor=user, action=f"entity.status.{body.status}",
               table_name="entities", row_pk=entity_id,
               before=before, after={"status": e.status, "comment": body.comment},
               request=request)
    db.commit()
    db.refresh(e)
    return _to_list_item(db, e)
