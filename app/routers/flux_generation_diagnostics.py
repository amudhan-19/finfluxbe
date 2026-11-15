# @file    routers/flux_generation_diagnostics.py
#
# Per-entity Flux generation endpoints used by the supervisor "Period
# Config" panel and by the admin diagnostics view. Two routes:
#
#   GET  /api/entities/{entity_id}/flux-generation-diagnostics
#   POST /api/entities/{entity_id}/flux/save-period-and-generate
#
# Both routes are gated by `require_role("supervisor","director","admin")`
# at the router level and additionally re-check `can_access_entity` so a
# supervisor can only touch their assigned entities — the analyst role is
# explicitly excluded as per the spec.

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import DataError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import can_access_entity, get_current_user, require_role
from app.db.session import get_db
from app.models.models import Entity, User
from app.schemas.schemas import (
    FluxGenerationCaptionDetailOut,
    FluxGenerationDiagnosticsOut,
    PeriodRangeOut,
    SavePeriodAndGeneratePeriod,
    SavePeriodAndGenerateRequest,
    SavePeriodAndGenerateResponse,
    StatementGenerationBreakdown,
)
from app.services.erp.safe_errors import (
    BC_TRANSACTIONS_NOT_SYNCED,
    FLUX_GENERATION_FAILED,
    NO_FLUX_ROWS_GENERATED,
    NO_MATCHING_TRANSACTIONS_FOR_MAPPING,
    ONTOLOGY_MAPPING_MISSING,
    PERIOD_CONFIG_INVALID,
    PERIOD_SAVE_FAILED,
    PL_NO_MAPPED_GL_ACCOUNTS,
    PL_NO_MATCHING_TRANSACTIONS,
    PL_ONTOLOGY_MAPPING_MISSING,
    SELECTED_PERIOD_OUTSIDE_BC_RANGE,
    safe_error,
)
from app.services.flux_generation import (
    StatementDiagnostics,
    build_flux_generation_diagnostics,
    normalize_statement_key,
    preview_flux_generation,
    upsert_bc_generated_flux_rows,
    validate_selected_period_against_bc_data,
)
from app.services.flux_period_config import (
    build_period_range_key,
    parse_flux_period_config,
)


_log = logging.getLogger(__name__)

# Router-level role gate — analysts cannot touch this surface.
router = APIRouter(
    dependencies=[Depends(require_role("supervisor", "director", "admin"))],
)


def _resolve_entity_or_403(
    db: Session, entity_id: str, user: User,
) -> Entity:
    """Common guard: 404 when entity is missing, 403 when the supervisor
    is trying to touch an entity they don't supervise."""
    entity = db.query(Entity).filter(Entity.id == entity_id).first()
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    if user.role != "admin" and not can_access_entity(db, user, entity_id):
        raise HTTPException(
            status_code=403,
            detail={
                "safe_error_code": "ENTITY_ACCESS_DENIED",
                "safe_error_message":
                    "You do not have access to this entity.",
            },
        )
    return entity


# ── Diagnostics ────────────────────────────────────────────────────────
@router.get(
    "/{entity_id}/flux-generation-diagnostics",
    response_model=FluxGenerationDiagnosticsOut,
    summary="Diagnose the Flux generation pipeline for one entity",
    description=(
        "Returns the parsed current/comparison ranges, the BC sync window, "
        "ontology coverage, GL entry counts in each window, and the "
        "current `flux_data` row count. Read-only. Supervisor can call "
        "this for entities they supervise; admins can call it for any "
        "entity."
    ),
    responses={
        404: {"description": "Entity not found"},
        422: {"description": "Period configuration is invalid"},
    },
)
def diagnostics(
    entity_id: str,
    statement: Optional[str] = Query(default=None, pattern="^(pl|bs|both)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FluxGenerationDiagnosticsOut:
    entity = _resolve_entity_or_403(db, entity_id, user)
    try:
        report = build_flux_generation_diagnostics(
            db, entity, statement_filter=statement,
        )
    except ValueError as exc:
        err = safe_error(PERIOD_CONFIG_INVALID, str(exc))
        raise HTTPException(
            status_code=422,
            detail={
                "safe_error_code": err.code,
                "safe_error_message": err.message,
            },
        ) from None
    return FluxGenerationDiagnosticsOut(
        entity_id=report.entity_id,
        period_type=report.period_type,
        period_from_raw=report.period_from_raw,
        period_to_raw=report.period_to_raw,
        current_label=report.current_label,
        comparison_label=report.comparison_label,
        current_start_date=report.current_start_date,
        current_end_date=report.current_end_date,
        comparison_start_date=report.comparison_start_date,
        comparison_end_date=report.comparison_end_date,
        bc_oldest_posting_date=report.bc_oldest_posting_date,
        bc_latest_posting_date=report.bc_latest_posting_date,
        bc_entry_count=report.bc_entry_count,
        is_selected_range_within_bc_data=report.is_selected_range_within_bc_data,
        warnings=report.warnings,
        ontology_mappings_total=report.ontology_mappings_total,
        captions_with_mapped_gl=report.captions_with_mapped_gl,
        captions_without_mapped_gl=report.captions_without_mapped_gl,
        gl_entries_in_current_range=report.gl_entries_in_current_range,
        gl_entries_in_comparison_range=report.gl_entries_in_comparison_range,
        flux_rows_for_selected_period=report.flux_rows_for_selected_period,
        statement_filter=report.statement_filter,
        captions=[
            FluxGenerationCaptionDetailOut(**c) for c in report.captions
        ],
    )


# ── Save & Generate ───────────────────────────────────────────────────
def _classify_post_validation(
    *, period_warnings: list[str], bc_entry_count: int,
    captions_with_gl: int, mappings_total: int,
    matched_transaction_total: int,
    rows_generated: int = 0,
    pl_bucket: Optional[StatementDiagnostics] = None,
    bs_bucket: Optional[StatementDiagnostics] = None,
    statement_requested: str = "both",
) -> Optional[tuple[str, str]]:
    """Decide which ``safe_error_code`` to bubble up after generation.

    The route never raises on these — they're passed through on the
    response so the supervisor UI can render a clear toast. ``None``
    means everything looked healthy. Order is intentional: the first
    blocking signal wins.

    The PL-specific path (``PL_*``) fires when BS produced rows but PL
    did not — that is the demo-blocker we explicitly want to surface so
    the supervisor doesn't have to dig into the diagnostics panel.
    """
    if bc_entry_count == 0:
        return BC_TRANSACTIONS_NOT_SYNCED, (
            "No Business Central transactions are synced for this entity."
        )
    if mappings_total == 0 or captions_with_gl == 0:
        return ONTOLOGY_MAPPING_MISSING, (
            "Ontology mapping is empty or has no GL accounts mapped."
        )
    if any("outside the synced BC range" in w for w in period_warnings):
        return SELECTED_PERIOD_OUTSIDE_BC_RANGE, (
            "Selected period is outside the synced BC data range."
        )

    # PL-only diagnosis: BS regenerated cleanly but PL produced 0 rows.
    pl_attempted = statement_requested in ("pl", "both")
    bs_ok = bs_bucket is not None and (bs_bucket.rows_inserted + bs_bucket.rows_updated) > 0
    if pl_attempted and pl_bucket is not None:
        pl_generated = pl_bucket.rows_inserted + pl_bucket.rows_updated
        if pl_generated == 0 and (bs_ok or statement_requested == "pl"):
            if pl_bucket.captions_total == 0:
                return PL_ONTOLOGY_MAPPING_MISSING, (
                    "Profit & Loss ontology has no mappings yet."
                )
            if pl_bucket.captions_with_mappings == 0:
                return PL_NO_MAPPED_GL_ACCOUNTS, (
                    "Profit & Loss captions are present but none have GL "
                    "accounts assigned."
                )
            if pl_bucket.matched_transaction_total == 0:
                return PL_NO_MATCHING_TRANSACTIONS, (
                    "Mapped P&L GL accounts have no transactions in the "
                    "selected period."
                )

    if matched_transaction_total == 0:
        return NO_MATCHING_TRANSACTIONS_FOR_MAPPING, (
            "Mapped GL accounts have no matching transactions in the "
            "selected period."
        )
    if rows_generated == 0:
        return NO_FLUX_ROWS_GENERATED, (
            "Period saved but no Flux rows were generated from Business "
            "Central for the selected window."
        )
    return None


def _to_breakdown(bucket: StatementDiagnostics) -> StatementGenerationBreakdown:
    """Project the service-layer ``StatementDiagnostics`` onto the
    response schema. Kept tiny so the route reads top-to-bottom."""
    return StatementGenerationBreakdown(
        statement=bucket.statement,
        captions_total=bucket.captions_total,
        captions_with_mappings=bucket.captions_with_mappings,
        captions_without_mappings=list(bucket.captions_without_mappings),
        captions_with_zero_transactions=list(bucket.captions_with_zero_transactions),
        matched_transaction_total=bucket.matched_transaction_total,
        current_transactions_matched=bucket.current_transactions_matched,
        comparison_transactions_matched=bucket.comparison_transactions_matched,
        rows_inserted=bucket.rows_inserted,
        rows_updated=bucket.rows_updated,
        rows_skipped=bucket.rows_skipped,
        rows_generated=bucket.rows_inserted + bucket.rows_updated,
    )


@router.post(
    "/{entity_id}/flux/save-period-and-generate",
    response_model=SavePeriodAndGenerateResponse,
    summary="Save entity period config and regenerate Flux from BC",
    description=(
        "Updates the entity's `period_type`, `period_from`, `period_to`, "
        "then runs Generate-Flux-from-BC for the new window. Supervisors "
        "can only call this for entities they supervise; admins can call "
        "it for any entity."
    ),
    responses={
        404: {"description": "Entity not found"},
        422: {"description": "Period configuration is invalid"},
    },
)
def save_period_and_generate(
    entity_id: str,
    body: SavePeriodAndGenerateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavePeriodAndGenerateResponse:
    entity = _resolve_entity_or_403(db, entity_id, user)

    # 1. Persist the new period config in-line so the diagnostics + the
    #    flux query share a single source of truth.
    before = {
        "period_type": entity.period_type,
        "period_from": entity.period_from,
        "period_to":   entity.period_to,
    }
    entity.period_type = body.period_type
    saved_from = build_period_range_key(body.period_from)
    saved_to = build_period_range_key(body.period_to)
    if not saved_from or not saved_to:
        db.rollback()
        err = safe_error(PERIOD_CONFIG_INVALID)
        raise HTTPException(
            status_code=422,
            detail={
                "safe_error_code": err.code,
                "safe_error_message": err.message,
            },
        ) from None
    entity.period_from = saved_from
    entity.period_to = saved_to
    try:
        db.flush()
    except DataError:
        _log.exception(
            "period save failed for %s (value too long or invalid)",
            entity_id,
        )
        db.rollback()
        err = safe_error(PERIOD_SAVE_FAILED)
        raise HTTPException(
            status_code=422,
            detail={
                "safe_error_code": err.code,
                "safe_error_message": err.message,
            },
        ) from None

    # 2. Validate the period parses cleanly. Anything else is a 422 with
    #    a safe error code — never a stack trace.
    try:
        period = parse_flux_period_config(entity)
    except ValueError as exc:
        db.rollback()
        err = safe_error(PERIOD_CONFIG_INVALID, str(exc))
        raise HTTPException(
            status_code=422,
            detail={
                "safe_error_code": err.code,
                "safe_error_message": err.message,
            },
        ) from None

    validation = validate_selected_period_against_bc_data(
        db, entity_id, period,
    )
    warnings: list[str] = list(validation.warnings)

    # 3. Run the same preview pipeline the admin endpoint uses, then
    #    commit the rows.
    try:
        rows, diag = preview_flux_generation(db, entity, body.statement)
        inserted, updated, skipped = upsert_bc_generated_flux_rows(
            db, entity, rows,
            overwrite_existing=body.overwrite_existing,
            diag=diag,
        )
    except SQLAlchemyError:
        _log.exception("save_period_and_generate failed for %s", entity_id)
        db.rollback()
        err = safe_error(FLUX_GENERATION_FAILED)
        raise HTTPException(
            status_code=500,
            detail={
                "safe_error_code": err.code,
                "safe_error_message": err.message,
            },
        ) from None

    log_change(
        db, actor=user, action="flux.save_period_and_generate",
        table_name="flux_data", row_pk=entity_id,
        before=before,
        after={
            "period_type": body.period_type,
            "period_from": saved_from,
            "period_to":   saved_to,
            "statement":   body.statement,
            "rows_inserted": inserted,
            "rows_updated":  updated,
            "rows_skipped":  skipped,
            "matched_transaction_total": diag.matched_transaction_total,
        },
        request=request,
    )
    db.commit()

    pl_bucket = diag.per_statement.get("pl")
    bs_bucket = diag.per_statement.get("bs")
    requested_statement = normalize_statement_key(body.statement) or "both"
    rows_generated = inserted + updated

    safe_outcome = _classify_post_validation(
        period_warnings=warnings,
        bc_entry_count=validation.bc_range.entry_count,
        captions_with_gl=diag.captions_with_mappings,
        mappings_total=diag.captions_total,
        matched_transaction_total=diag.matched_transaction_total,
        rows_generated=rows_generated,
        pl_bucket=pl_bucket,
        bs_bucket=bs_bucket,
        statement_requested=requested_statement,
    )

    safe_code: Optional[str] = None
    safe_msg:  Optional[str] = None
    if safe_outcome is not None:
        safe_code, safe_msg = safe_outcome

    statements_breakdown: dict[str, StatementGenerationBreakdown] = {
        key: _to_breakdown(bucket)
        for key, bucket in diag.per_statement.items()
    }

    return SavePeriodAndGenerateResponse(
        ok=safe_code is None,
        entity_id=entity_id,
        period_type=body.period_type,
        period_from=entity.period_from,
        period_to=entity.period_to,
        saved_period_from=entity.period_from,
        saved_period_to=entity.period_to,
        current_label=period.current_label,
        comparison_label=period.comparison_label,
        current_range=PeriodRangeOut(
            start_date=period.current_start_date,
            end_date=period.current_end_date,
            label=period.current_label,
        ),
        comparison_range=PeriodRangeOut(
            start_date=period.comparison_start_date,
            end_date=period.comparison_end_date,
            label=period.comparison_label,
        ),
        period=SavePeriodAndGeneratePeriod(
            current_start_date=period.current_start_date,
            current_end_date=period.current_end_date,
            comparison_start_date=period.comparison_start_date,
            comparison_end_date=period.comparison_end_date,
        ),
        rows_inserted=inserted,
        rows_updated=updated,
        rows_skipped=skipped,
        rows_generated=rows_generated,
        captions_total=diag.captions_total,
        captions_with_mappings=diag.captions_with_mappings,
        captions_without_mappings=diag.captions_without_mappings,
        captions_with_zero_transactions=diag.captions_with_zero_transactions,
        matched_transaction_total=diag.matched_transaction_total,
        current_transactions_matched=diag.current_transactions_matched,
        comparison_transactions_matched=diag.comparison_transactions_matched,
        statements=statements_breakdown,
        safe_error_code=safe_code,
        safe_error_message=safe_msg,
        warnings=warnings,
    )
