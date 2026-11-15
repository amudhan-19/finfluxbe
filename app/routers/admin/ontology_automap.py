# @file    routers/admin/ontology_automap.py
#
# Demo auto-map is disabled. Ontology mapping must be authored manually
# in the Admin Ontology UI and saved to ``ontology_mappings.erp_gl_json``.

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.deps import require_role
from app.schemas.schemas import OntologyAutoMapRequest, OntologyAutoMapResponse

router = APIRouter(dependencies=[Depends(require_role("admin"))])

_AUTOMAP_DISABLED_DETAIL = (
    "Demo auto-map is disabled. Select GL accounts manually in Ontology "
    "Mapping, save each caption, then use Generate Flux from BC."
)


@router.post(
    "/{entity_id}/ontology/demo-auto-map",
    response_model=OntologyAutoMapResponse,
    summary="(Disabled) Demo auto-map ontology",
    deprecated=True,
    responses={410: {"description": "Auto-map disabled — use manual mapping"}},
)
def demo_auto_map_ontology(
    entity_id: str,
    body: OntologyAutoMapRequest,
) -> OntologyAutoMapResponse:
    raise HTTPException(status_code=410, detail=_AUTOMAP_DISABLED_DETAIL)
