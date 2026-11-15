# @file    admin/entities.py

from __future__ import annotations
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import require_role, get_current_user
from app.db.session import get_db
from app.models.models import Entity, User, Threshold
from app.schemas.schemas import (
    EntityOut, AdminEntityCreate, AdminEntityUpdate, UserOut,
    SubledgerOptionsOut, SubledgerPartyOptionOut,
)
from app.services.subledger_catalog import build_subledger_options_response

router = APIRouter(dependencies=[Depends(require_role("admin"))])


def _to_entity_out(e: Entity) -> EntityOut:
    return EntityOut(
        id=e.id, name=e.name, region=e.region, status=e.status,
        tags=e.tags or [], period_type=e.period_type,
        period_from=e.period_from, period_to=e.period_to,
        sub_due_date=e.sub_due_date, rev_due_date=e.rev_due_date,
        approval_level=e.approval_level,
        supervisor=UserOut.model_validate(e.supervisor),
        director=UserOut.model_validate(e.director) if e.director else None,
        currency=e.currency, fiscal_year_end=e.fiscal_year_end,
        tax_id=e.tax_id, is_active=e.is_active,
    )


@router.get(
    "",
    response_model=List[EntityOut],
    summary="List all legal entities",
    description="Admin-only; not filtered by analyst visibility.",
)
def list_entities(db: Session = Depends(get_db)):
    return [_to_entity_out(e) for e in db.query(Entity).order_by(Entity.id).all()]


@router.get(
    "/{entity_id}/subledger-options",
    response_model=SubledgerOptionsOut,
    summary="Customer / vendor ledger options for Ontology Mapping",
    responses={404: {"description": "Entity not found"}},
)
def list_subledger_options(entity_id: str, db: Session = Depends(get_db)):
    if not db.query(Entity).filter(Entity.id == entity_id).first():
        raise HTTPException(status_code=404, detail="Entity not found")
    payload = build_subledger_options_response(db, entity_id)
    return SubledgerOptionsOut(
        customers=[SubledgerPartyOptionOut(**c) for c in payload["customers"]],
        vendors=[SubledgerPartyOptionOut(**v) for v in payload["vendors"]],
        message=payload.get("message"),
        warnings=payload.get("warnings") or [],
    )


@router.post(
    "",
    response_model=EntityOut,
    status_code=201,
    summary="Create legal entity",
    description="Seeds default P&L and BS threshold rows.",
    responses={400: {"description": "Invalid supervisor"}, 409: {"description": "Entity id already exists"}},
)
def create_entity(
    body: AdminEntityCreate, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if db.query(Entity).filter(Entity.id == body.id).first():
        raise HTTPException(status_code=409, detail=f"Entity {body.id} already exists")
    sup = db.query(User).filter(User.id == body.supervisor_id).first()
    if not sup or sup.role not in ("supervisor", "director", "admin"):
        raise HTTPException(status_code=400, detail="Supervisor must be a supervisor/director/admin user")

    director = None
    if body.director_id is not None:
        director = db.query(User).filter(User.id == body.director_id).first()
        if not director or director.role not in ("director", "admin"):
            raise HTTPException(status_code=400, detail="Director must be a director/admin user")

    e = Entity(
        id=body.id, name=body.name, region=body.region,
        supervisor_id=body.supervisor_id,
        director_id=body.director_id,
        approval_level=body.approval_level,
        sub_due_date=body.sub_due_date,
        rev_due_date=body.rev_due_date,
        is_active=body.is_active,
        period_type=body.period_type, period_from=body.period_from, period_to=body.period_to,
        currency=body.currency, fiscal_year_end=body.fiscal_year_end,
        tax_id=body.tax_id, tags=body.tags,
    )
    db.add(e)
    # Default thresholds (P&L: 10/100K, BS: 5/75K — per workflow doc)
    db.add(Threshold(entity_id=body.id, statement="pl", mode="both",
                     pct_value=10, abs_value=100000, logic="or", apply_all=True))
    db.add(Threshold(entity_id=body.id, statement="bs", mode="both",
                     pct_value=5,  abs_value=75000,  logic="or", apply_all=True))

    log_change(db, actor=actor, action="entity.create",
               table_name="entities", row_pk=body.id,
               after=body.model_dump(), request=request)
    db.commit()
    db.refresh(e)
    return _to_entity_out(e)


@router.patch(
    "/{entity_id}",
    response_model=EntityOut,
    summary="Update legal entity",
    responses={404: {"description": "Entity not found"}},
)
def update_entity(
    entity_id: str, body: AdminEntityUpdate, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    before = {f: getattr(e, f) for f in
              ("name","region","supervisor_id","director_id","approval_level",
               "currency","fiscal_year_end","tax_id","is_active")}
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(e, field, value)
    log_change(db, actor=actor, action="entity.update",
               table_name="entities", row_pk=entity_id,
               before=before, after=body.model_dump(exclude_none=True), request=request)
    db.commit()
    db.refresh(e)
    return _to_entity_out(e)


@router.delete(
    "/{entity_id}",
    status_code=204,
    summary="Delete legal entity",
    responses={404: {"description": "Entity not found"}},
)
def delete_entity(
    entity_id: str, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    e = db.query(Entity).filter(Entity.id == entity_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    log_change(db, actor=actor, action="entity.delete",
               table_name="entities", row_pk=entity_id,
               before={"name": e.name}, request=request)
    db.delete(e)
    db.commit()
