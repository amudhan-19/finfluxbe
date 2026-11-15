# @file    macro.py

from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_role, require_entity_access
from app.db.session import get_db
from app.models.models import MacroFactor, User
from app.schemas.schemas import MacroFactorOut, MacroFactorCreate, MacroFactorEdit

router = APIRouter()


@router.get(
    "/{entity_id}/macro-factors",
    response_model=List[MacroFactorOut],
    dependencies=[Depends(require_entity_access())],
    summary="List macro factors",
)
def get_macro_factors(entity_id: str, db: Session = Depends(get_db)):
    return (
        db.query(MacroFactor)
        .filter(MacroFactor.entity_id == entity_id)
        .order_by(MacroFactor.category, MacroFactor.created_at)
        .all()
    )


@router.post(
    "/{entity_id}/macro-factors",
    response_model=MacroFactorOut,
    status_code=201,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Add custom macro factor",
)
def add_macro_factor(
    entity_id: str, body: MacroFactorCreate,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    factor = MacroFactor(
        entity_id  = entity_id,
        category   = body.category,
        icon       = body.icon,
        text       = body.text,
        is_custom  = True,
        created_by = user.id,
    )
    db.add(factor)
    db.commit()
    db.refresh(factor)
    return factor


@router.patch(
    "/{entity_id}/macro-factors/{factor_id}",
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Edit macro factor text",
    responses={404: {"description": "Factor not found"}},
)
def edit_macro_factor(
    entity_id: str, factor_id: int, body: MacroFactorEdit,
    db: Session = Depends(get_db),
):
    f = db.query(MacroFactor).filter(
        MacroFactor.id == factor_id, MacroFactor.entity_id == entity_id,
    ).first()
    if not f:
        raise HTTPException(status_code=404, detail="Factor not found")
    f.text = body.text
    db.commit()
    return {"id": factor_id, "text": body.text}


@router.delete(
    "/{entity_id}/macro-factors/{factor_id}",
    status_code=204,
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
    summary="Delete custom macro factor",
    description="Only rows with `is_custom=true` may be deleted.",
    responses={404: {"description": "Custom factor not found"}},
)
def delete_macro_factor(entity_id: str, factor_id: int, db: Session = Depends(get_db)):
    f = db.query(MacroFactor).filter(
        MacroFactor.id == factor_id,
        MacroFactor.entity_id == entity_id,
        MacroFactor.is_custom == True,
    ).first()
    if not f:
        raise HTTPException(status_code=404, detail="Custom factor not found")
    db.delete(f)
    db.commit()
