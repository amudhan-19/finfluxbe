# @file    routers/admin/flux_generate.py
#
# Admin endpoint: generate flux_data from Business Central GL entries.
#
# The contract is intentionally identical for preview and commit so the
# frontend can render the preview table first, then resubmit with
# ``mode='commit'`` after the admin reviews it.

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.models import Entity, User
from app.schemas.schemas import (
    FluxGenerateRequest,
    FluxGenerateResponse,
    FluxPreviewRowOut,
    FluxSkippedCaptionOut,
)
from app.services.flux_generation import (
    build_statement_calculation_summaries,
    parse_entity_period,
    parse_flux_period_config,
    preview_flux_generation,
    upsert_bc_generated_flux_rows,
)
from app.schemas.schemas import FluxStatementCalculationOut
from app.services.slm.slm_errors import (
    FLUX_GENERATION_FAILED,
    make_safe_ai_error,
)


_log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_role("admin"))])


def _to_preview_out(row) -> FluxPreviewRowOut:
    """Mapping helper extracted so the route stays free of nested
    constructors (SonarQube readability rule)."""
    return FluxPreviewRowOut(
        statement=row.statement,
        caption=row.caption,
        current_value=float(row.current_value),
        comp_value=float(row.comp_value),
        pct_variance=float(row.pct_variance) if row.pct_variance is not None else None,
        is_material=row.is_material,
        had_existing_row=row.had_existing_row,
        matched_account_count=row.matched_account_count,
        matched_transaction_count=row.matched_transaction_count,
        skipped_reason=row.skipped_reason,
        selected_gl_accounts=row.selected_gl_accounts,
        selected_subledger=row.selected_subledger,
        selected_dimensions=row.selected_subledger,
        warnings=row.warnings,
    )


def _build_response(
    *,
    mode: str,
    entity_id: str,
    statement: str,
    rows,
    preview_out: list[FluxPreviewRowOut],
    diag,
    period,
    inserted: int = 0,
    updated: int = 0,
    skipped: int = 0,
) -> FluxGenerateResponse:
    skipped_captions = [
        FluxSkippedCaptionOut(caption=r.caption, reason=r.skipped_reason or "")
        for r in rows
        if r.skipped_reason
    ]
    rows_generated = inserted + updated if mode == "commit" else sum(
        1 for r in rows if not r.skipped_reason
    )
    return FluxGenerateResponse(
        mode=mode,
        entity_id=entity_id,
        statement=statement,
        rows_previewed=len(rows),
        rows_inserted=inserted,
        rows_updated=updated,
        rows_skipped=skipped,
        preview_rows=preview_out if mode == "preview" else [],
        captions_total=diag.captions_total,
        captions_with_mappings=diag.captions_with_mappings,
        captions_without_mappings=diag.captions_without_mappings,
        captions_with_zero_transactions=diag.captions_with_zero_transactions,
        matched_transaction_total=diag.matched_transaction_total,
        rows_generated=rows_generated,
        matched_transactions=diag.matched_transaction_total,
        unmapped_captions=diag.captions_without_mappings,
        captions_with_no_transactions=diag.captions_with_zero_transactions,
        skipped_captions=skipped_captions,
        statement_calculations=[
            FluxStatementCalculationOut(**s)
            for s in build_statement_calculation_summaries(period, diag, rows)
        ],
    )


@router.post(
    "/{entity_id}/generate-flux-from-bc",
    response_model=FluxGenerateResponse,
    response_model_by_alias=True,
    summary="Generate Flux data from Business Central GL entries",
    description=(
        "Aggregates synced `erp_general_ledger_entries` via manually saved "
        "ontology mappings (`erp_gl_json` / optional `erp_subledger_json`) to produce "
        "`flux_data` rows for the entity's current/comparison period pair.\n\n"
        "**preview** mode calculates rows without writing anything and "
        "returns warnings (unmapped captions, captions with no matching "
        "transactions).\n\n"
        "**commit** mode upserts flux_data and writes an audit log entry. "
        "Rows are stamped with `source_type='business_central'` so Flux "
        "Review can distinguish them from seed/demo data.\n\n"
        "When `overwrite_existing=false` (default), captions that already "
        "have a flux_data row for the same period are preserved; comments "
        "and sign-offs are never deleted."
    ),
    responses={
        404: {"description": "Entity not found"},
        422: {"description": "Entity period label could not be parsed"},
        500: {"description": "Unexpected DB error during generation"},
    },
)
def generate_flux_from_bc(
    entity_id: str,
    body: FluxGenerateRequest,
    request: Request,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FluxGenerateResponse:
    entity = db.query(Entity).filter(Entity.id == entity_id).first()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    try:
        parse_entity_period(entity.period_from)
        parse_entity_period(entity.period_to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        rows, diag = preview_flux_generation(db, entity, body.statement)
    except SQLAlchemyError:
        _log.exception("preview_flux_generation failed for %s", entity_id)
        err = make_safe_ai_error(FLUX_GENERATION_FAILED)
        raise HTTPException(
            status_code=500,
            detail={
                "safe_error_code": err.code,
                "safe_error_message": err.message,
            },
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
        inserted, updated, skipped = upsert_bc_generated_flux_rows(
            db, entity, rows, overwrite_existing=body.overwrite_existing,
        )
    except SQLAlchemyError:
        _log.exception("upsert_bc_generated_flux_rows failed for %s", entity_id)
        db.rollback()
        err = make_safe_ai_error(FLUX_GENERATION_FAILED)
        raise HTTPException(
            status_code=500,
            detail={
                "safe_error_code": err.code,
                "safe_error_message": err.message,
            },
        ) from None

    log_change(
        db,
        actor=actor,
        action="flux.generate_from_bc",
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
