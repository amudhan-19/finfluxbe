# @file    routers/admin/flux_generate_qb.py
#
# Admin endpoint: generate flux_data from QuickBooks GL entries.
# Mirrors the BC generate route but scopes ERP data to erp_type=quickbooks.

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.models import Entity, EntityErpMapping, ERP_TYPE_QUICKBOOKS, User
from app.routers.admin.flux_generate import _build_response, _to_preview_out
from app.schemas.schemas import FluxGenerateRequest, FluxGenerateResponse
from app.services.flux_generation import (
    build_statement_calculation_summaries,
    parse_entity_period,
    parse_flux_period_config,
    preview_flux_generation,
    upsert_qb_generated_flux_rows,
)
from app.services.slm.slm_errors import FLUX_GENERATION_FAILED, make_safe_ai_error

_log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_role("admin"))])


def _require_qb_entity(db: Session, entity_id: str) -> None:
    mapped = (
        db.query(EntityErpMapping)
        .filter(
            EntityErpMapping.entity_id == entity_id,
            EntityErpMapping.erp_key == ERP_TYPE_QUICKBOOKS,
        )
        .first()
    )
    if not mapped:
        raise HTTPException(status_code=409, detail="Entity is not mapped to QuickBooks")


@router.post(
    "/{entity_id}/generate-flux-from-quickbooks",
    response_model=FluxGenerateResponse,
    response_model_by_alias=True,
    summary="Generate Flux data from QuickBooks GL entries",
)
def generate_flux_from_quickbooks(
    entity_id: str,
    body: FluxGenerateRequest,
    request: Request,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FluxGenerateResponse:
    _require_qb_entity(db, entity_id)
    entity = db.query(Entity).filter(Entity.id == entity_id).first()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    try:
        parse_entity_period(entity.period_from)
        parse_entity_period(entity.period_to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        rows, diag = preview_flux_generation(
            db, entity, body.statement, erp_type=ERP_TYPE_QUICKBOOKS,
        )
    except SQLAlchemyError:
        _log.exception("preview_flux_generation (QB) failed for %s", entity_id)
        err = make_safe_ai_error(FLUX_GENERATION_FAILED)
        raise HTTPException(
            status_code=500,
            detail={"safe_error_code": err.code, "safe_error_message": err.message},
        ) from None

    preview_out = [_to_preview_out(r) for r in rows]
    period = parse_flux_period_config(entity)

    if body.mode == "preview":
        return _build_response(
            mode="preview",
            entity_id=entity_id,
            statement=body.statement,
            rows=rows,
            preview_out=preview_out,
            diag=diag,
            period=period,
        )

    try:
        inserted, updated, skipped = upsert_qb_generated_flux_rows(
            db, entity, rows, overwrite_existing=body.overwrite_existing, diag=diag,
        )
    except SQLAlchemyError:
        _log.exception("upsert_qb_generated_flux_rows failed for %s", entity_id)
        db.rollback()
        err = make_safe_ai_error(FLUX_GENERATION_FAILED)
        raise HTTPException(
            status_code=500,
            detail={"safe_error_code": err.code, "safe_error_message": err.message},
        ) from None

    log_change(
        db,
        actor=actor,
        action="flux.generate_from_quickbooks",
        table_name="flux_data",
        row_pk=entity_id,
        after={
            "statement": body.statement,
            "mode": body.mode,
            "rows_inserted": inserted,
            "rows_updated": updated,
            "rows_skipped": skipped,
            "overwrite_existing": body.overwrite_existing,
        },
        request=request,
    )
    db.commit()

    return _build_response(
        mode="commit",
        entity_id=entity_id,
        statement=body.statement,
        rows=rows,
        preview_out=preview_out,
        diag=diag,
        period=period,
        inserted=inserted,
        updated=updated,
        skipped=skipped,
    )
