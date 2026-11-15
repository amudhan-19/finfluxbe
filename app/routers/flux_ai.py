# @file    routers/flux_ai.py
#
# Flux AI commentary endpoint.
#
# Mounts under `/api/entities` (same prefix as the existing flux router) so
# the URL stays predictable for the frontend:
#
#     POST /api/entities/{entity_id}/flux-ai/generate
#
# RBAC: any authenticated user with entity access may regenerate. Director
# role is excluded because directors never edit commentary in the existing
# UI; analysts and supervisors are the actual users of this action. The
# coarse role gate is enforced in `require_role`; per-entity access is
# enforced inside the service via `can_access_entity`.

from __future__ import annotations
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.models import User
from app.schemas.schemas import FluxAiGenerateRequest, FluxAiGenerateResponse
from app.services.flux_ai.flux_ai_service import generate_flux_ai_commentary
from app.services.slm.slm_errors import (
    ENTITY_ACCESS_DENIED,
    ENTITY_NOT_FOUND,
    FLUX_DATA_NOT_FOUND,
    SLM_CONFIG_MISSING,
)

_log = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/{entity_id}/flux-ai/generate",
    response_model=FluxAiGenerateResponse,
    dependencies=[Depends(require_role("analyst", "supervisor", "admin"))],
    summary="Generate Flux AI commentary",
    description=(
        "Generates concise + detailed AI commentary for one (statement, "
        "caption) on the entity's active period. Saves the result into "
        "`ai_commentary` (preserving any reviewer-edited `edited_text`)."
        "\n\n"
        "Returns HTTP 200 with `ok=true` on success. Auth / access / "
        "missing-data errors use the standard 401/403/404 HTTP codes. "
        "Runtime AI failures (timeout, parse, schema, transport) return "
        "HTTP 200 with `ok=false` and a stable `safe_error_code` so the "
        "frontend can render a single toast variant."
    ),
    responses={
        401: {"description": "Not authenticated"},
        403: {"description": "No access to entity / role not permitted"},
        404: {"description": "Entity or flux row not found"},
        503: {"description": "AI service is not configured"},
    },
)
async def generate_flux_ai(
    entity_id: str,
    body: FluxAiGenerateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
) -> FluxAiGenerateResponse:
    result = await generate_flux_ai_commentary(
        db,
        current_user=user,
        entity_id=entity_id,
        statement=body.statement,
        caption=body.caption,
        request=request,
    )

    if result.ok:
        db.commit()
        return FluxAiGenerateResponse(
            ok=True,
            entity_id=result.entity_id,
            statement=result.statement,
            caption=result.caption,
            concise_text=result.concise_text,
            detailed_text=result.detailed_text,
            drivers=result.drivers,
            mapped_accounts=result.mapped_accounts,
            customer_vendor_notes=result.customer_vendor_notes,
            transaction_notes=result.transaction_notes,
            party_diagnostics=result.party_diagnostics or None,
            review_notes=result.review_notes,
            recommended_actions=result.recommended_actions,
            confidence_score=result.confidence_score,
            variance_drivers=result.variance_drivers,
            risk_flags=result.risk_flags,
        )

    # Translate domain-level failures to HTTP-level errors where appropriate.
    err = result.error
    if err is None:
        # Defensive default — service contract guarantees an error on !ok.
        raise HTTPException(status_code=500, detail="Unexpected AI failure")

    if err.code == ENTITY_NOT_FOUND:
        raise HTTPException(status_code=404, detail=err.message)
    if err.code == ENTITY_ACCESS_DENIED:
        raise HTTPException(status_code=403, detail=err.message)
    if err.code == FLUX_DATA_NOT_FOUND:
        raise HTTPException(status_code=404, detail={
            "safe_error_code": err.code,
            "safe_error_message": err.message,
        })
    if err.code == SLM_CONFIG_MISSING:
        raise HTTPException(status_code=503, detail={
            "safe_error_code": err.code,
            "safe_error_message": err.message,
        })

    # Transient SLM failures (timeout / http / parse / schema) come back as
    # HTTP 200 + ok=false so the frontend can show a retry toast without
    # tripping its global 5xx handler.
    return FluxAiGenerateResponse(
        ok=False,
        entity_id=entity_id,
        statement=body.statement,
        caption=body.caption,
        safe_error_code=err.code,
        safe_error_message=err.message,
    )
