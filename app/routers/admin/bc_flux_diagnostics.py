# @file    routers/admin/bc_flux_diagnostics.py
#
# Admin-only diagnostics for the BC → Ontology → Flux pipeline. Used by the
# admin UI when Flux Review looks empty or stale: it tells the operator
# exactly which step is missing data so they can fix it without digging
# through the database.

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import require_role
from app.db.session import get_db
from app.models.models import (
    Entity,
    ErpDimensionValue,
    ErpGeneralLedgerEntry,
    ErpGlAccount,
    FluxData,
    OntologyMapping,
)
from app.schemas.schemas import BcFluxDiagnosticsOut
from app.services.flux_generation import (
    extract_mapped_dimension_values,
    extract_mapped_gl_account_nos,
)


_log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_role("admin"))])


def _classify_flux_rows(rows: list[FluxData]) -> tuple[int, int, int]:
    """Return ``(seed, bc_generated, manual)`` counts.
    NULL `source_type` is treated as seed (legacy demo data)."""
    seed = bc_generated = manual = 0
    for r in rows:
        st = (r.source_type or "seed").lower()
        if st == "business_central":
            bc_generated += 1
        elif st == "manual":
            manual += 1
        else:
            seed += 1
    return seed, bc_generated, manual


@router.get(
    "/{entity_id}/bc-flux-diagnostics",
    response_model=BcFluxDiagnosticsOut,
    summary="Diagnose the BC → Ontology → Flux pipeline for one entity",
    description=(
        "Returns the per-step counts the admin UI needs to debug an empty "
        "Flux Review: ontology mapping coverage, BC sync counts, and the "
        "current provenance breakdown of `flux_data`. Read-only."
    ),
    responses={404: {"description": "Entity not found"}},
)
def bc_flux_diagnostics(
    entity_id: str, db: Session = Depends(get_db),
) -> BcFluxDiagnosticsOut:
    entity = db.query(Entity).filter(Entity.id == entity_id).first()
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    mappings = db.query(OntologyMapping).filter(
        OntologyMapping.entity_id == entity_id,
    ).all()

    captions_with_gl = 0
    captions_with_dim = 0
    captions_without_gl: list[str] = []
    pl_count = bs_count = 0
    for m in mappings:
        if m.statement == "pl":
            pl_count += 1
        elif m.statement == "bs":
            bs_count += 1
        gl_codes = extract_mapped_gl_account_nos(m.erp_gl_json)
        if gl_codes:
            captions_with_gl += 1
        else:
            captions_without_gl.append(m.tflux_caption)
        if extract_mapped_dimension_values(m.erp_subgl_json):
            captions_with_dim += 1

    erp_gl_count = db.query(ErpGlAccount).filter(
        ErpGlAccount.entity_id == entity_id,
    ).count()
    erp_dim_count = db.query(ErpDimensionValue).filter(
        ErpDimensionValue.entity_id == entity_id,
    ).count()
    erp_entries_count = db.query(ErpGeneralLedgerEntry).filter(
        ErpGeneralLedgerEntry.entity_id == entity_id,
    ).count()

    flux_rows = db.query(FluxData).filter(
        FluxData.entity_id == entity_id,
        FluxData.period_from == entity.period_from,
        FluxData.period_to == entity.period_to,
    ).all()
    seed, bc_generated, manual = _classify_flux_rows(flux_rows)

    last_generated_at = max(
        (r.generated_at for r in flux_rows if r.generated_at is not None),
        default=None,
    )

    return BcFluxDiagnosticsOut(
        entity_id=entity_id,
        period_from=entity.period_from,
        period_to=entity.period_to,
        ontology_mappings_total=len(mappings),
        ontology_mappings_pl=pl_count,
        ontology_mappings_bs=bs_count,
        captions_with_mapped_gl=captions_with_gl,
        captions_without_mapped_gl=captions_without_gl,
        captions_with_mapped_dimensions=captions_with_dim,
        erp_gl_accounts_synced=erp_gl_count,
        erp_dimension_values_synced=erp_dim_count,
        erp_gl_entries_synced=erp_entries_count,
        flux_rows_total=len(flux_rows),
        flux_rows_seed=seed,
        flux_rows_bc_generated=bc_generated,
        flux_rows_manual=manual,
        last_generated_at=last_generated_at,
    )
