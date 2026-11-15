# @file    flux.py

from typing import List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import get_current_user, require_role, require_entity_access
from app.db.session import get_db
from app.models.models import FluxData, AiCommentary, Entity, User
from app.schemas.schemas import FluxRowOut, AiCommentaryOut, AiCommentaryEdit
from app.services.flux_finance_variance import to_flux_row_out
from app.services.flux_period_config import resolve_flux_period_keys

router = APIRouter()


@router.get(
    "/{entity_id}/flux",
    response_model=List[FluxRowOut],
    dependencies=[Depends(require_entity_access())],
    summary="List flux rows",
    description="P&L (`statement=pl`) or Balance Sheet (`bs`) lines for the entity’s current period window.",
    responses={404: {"description": "Entity not found"}},
)
def get_flux(
    entity_id: str,
    statement: str = Query("pl", pattern="^(pl|bs)$"),
    db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    period_from, period_to = resolve_flux_period_keys(db, e, statement)
    rows = (
        db.query(FluxData)
        .filter(
            FluxData.entity_id   == entity_id,
            FluxData.statement   == statement,
            FluxData.period_from == period_from,
            FluxData.period_to   == period_to,
        )
        .order_by(FluxData.sort_order)
        .all()
    )
    return [to_flux_row_out(r) for r in rows]


@router.get(
    "/{entity_id}/ai-commentary",
    response_model=List[AiCommentaryOut],
    dependencies=[Depends(require_entity_access())],
    summary="List all AI commentary rows",
    description="All caption-level AI commentary blobs for the entity.",
)
def get_all_ai_commentary(entity_id: str, db: Session = Depends(get_db)):
    return db.query(AiCommentary).filter(AiCommentary.entity_id == entity_id).all()


@router.get(
    "/{entity_id}/ai-commentary/{caption}",
    response_model=AiCommentaryOut,
    dependencies=[Depends(require_entity_access())],
    summary="Get AI commentary for one caption",
    responses={404: {"description": "Commentary not found for caption"}},
)
def get_ai_commentary(entity_id: str, caption: str, db: Session = Depends(get_db)):
    ai = db.query(AiCommentary).filter(
        AiCommentary.entity_id == entity_id,
        AiCommentary.caption   == caption,
    ).first()
    if not ai:
        raise HTTPException(status_code=404, detail="Commentary not found")
    return ai


@router.patch(
    "/{entity_id}/ai-commentary/{caption}",
    response_model=AiCommentaryOut,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Edit supervisor AI commentary text",
    description="Updates `edited_text` and audit trail; supervisor, director, or admin only.",
    responses={404: {"description": "Commentary not found"}},
)
def edit_ai_commentary(
    entity_id: str,
    caption: str,
    body: AiCommentaryEdit,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ai = db.query(AiCommentary).filter(
        AiCommentary.entity_id == entity_id,
        AiCommentary.caption   == caption,
    ).first()
    if not ai:
        raise HTTPException(status_code=404, detail="Commentary not found")
    before = {"edited_text": ai.edited_text}
    ai.edited_text = body.text
    ai.edited_by   = user.id
    ai.edited_at   = datetime.utcnow()
    log_change(db, actor=user, action="ai_commentary.edit",
               table_name="ai_commentary", row_pk=f"{entity_id}/{caption}",
               before=before, after={"edited_text": body.text}, request=request)
    db.commit()
    db.refresh(ai)
    return ai
