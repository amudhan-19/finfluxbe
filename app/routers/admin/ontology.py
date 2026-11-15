# @file    admin/ontology.py
#
# Ontology mapping for an entity. Locked until the entity has API-complete
# ERP credentials (gated by /admin/integrations/entities/{id}/api-complete).

from __future__ import annotations
import csv
import io
from typing import List, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import require_role, get_current_user
from app.db.session import get_db
from app.models.models import (
    OntologyMapping, CustomCaption, ApiCredential, Entity, EntityErpMapping, User,
)
from app.schemas.schemas import (
    OntologyMappingOut, OntologyMappingUpsert,
    CustomCaptionOut, CustomCaptionCreate,
    OntologyDisplayColumnUpdate,
)
from app.routers.admin.business_central import (
    ERP_TYPE_BC,
    _heal_credential_status_if_required_ready,
    calculate_business_central_readiness,
)

router = APIRouter(dependencies=[Depends(require_role("admin"))])


def _api_complete_from_creds(creds: List[ApiCredential]) -> bool:
    """Legacy mock-ERP gate: GL + Sub-GL + Transaction credentials all `verified`."""
    has_gl = any(c.layer == "gl" and c.status == "verified" for c in creds)
    has_sgl = any(c.layer == "sub_gl" and c.status == "verified" for c in creds)
    has_txn = any(c.layer == "transaction" and c.status == "verified" for c in creds)
    return has_gl and has_sgl and has_txn


def _is_bc_mapped(db: Session, entity_id: str) -> bool:
    return (db.query(EntityErpMapping)
              .filter(EntityErpMapping.entity_id == entity_id,
                      EntityErpMapping.erp_key == ERP_TYPE_BC)
              .first() is not None)


def _is_qb_mapped(db: Session, entity_id: str) -> bool:
    from app.models.models import ERP_TYPE_QUICKBOOKS
    return (db.query(EntityErpMapping)
              .filter(EntityErpMapping.entity_id == entity_id,
                      EntityErpMapping.erp_key == ERP_TYPE_QUICKBOOKS)
              .first() is not None)


def _api_complete(db: Session, entity_id: str) -> bool:
    """Returns True when the entity is API-ready for ontology editing.

    Business Central: credentials verified, active company selected, and
    GL accounts + general ledger entries synced (see
    `calculate_business_central_readiness`). Customer/vendor ledger is optional.

    Other ERPs: fall back to the legacy `ApiCredential.status == verified`
    gate so the existing mock connectors continue to behave unchanged."""
    if _is_bc_mapped(db, entity_id):
        readiness = calculate_business_central_readiness(db, entity_id)
        return bool(readiness.get("api_complete"))
    if _is_qb_mapped(db, entity_id):
        from app.routers.admin.quickbooks import calculate_quickbooks_readiness
        readiness = calculate_quickbooks_readiness(db, entity_id)
        return bool(readiness.get("api_complete"))
    creds = db.query(ApiCredential).filter(ApiCredential.entity_id == entity_id).all()
    return _api_complete_from_creds(creds)


@router.get(
    "/entities-ready",
    response_model=List[str],
    summary="Entity IDs ready for ontology mapping (BC: GL + GL entries synced)",
)
def list_entities_ready(db: Session = Depends(get_db)):
    """Union of:
    - BC-mapped entities whose Business Central readiness reports api_complete
    - Non-BC entities whose legacy ApiCredential rows are all verified."""
    bc_entities = {row.entity_id for row in db.query(EntityErpMapping)
                                                .filter(EntityErpMapping.erp_key == ERP_TYPE_BC).all()}
    from app.models.models import ERP_TYPE_QUICKBOOKS
    qb_entities = {row.entity_id for row in db.query(EntityErpMapping)
                                                .filter(EntityErpMapping.erp_key == ERP_TYPE_QUICKBOOKS).all()}
    ready: set[str] = set()

    for eid in bc_entities:
        _heal_credential_status_if_required_ready(db, eid)
        if calculate_business_central_readiness(db, eid).get("api_complete"):
            ready.add(eid)

    from app.routers.admin.quickbooks import calculate_quickbooks_readiness
    for eid in qb_entities:
        if calculate_quickbooks_readiness(db, eid).get("api_complete"):
            ready.add(eid)

    creds = db.query(ApiCredential).all()
    by_e: dict[str, List[ApiCredential]] = {}
    for c in creds:
        by_e.setdefault(c.entity_id, []).append(c)
    for eid, lst in by_e.items():
        if eid in bc_entities or eid in qb_entities:
            continue
        if _api_complete_from_creds(lst):
            ready.add(eid)
    return sorted(ready)


@router.get(
    "/{entity_id}",
    response_model=List[OntologyMappingOut],
    summary="List ontology mappings",
    description="Requires `statement` query: **pl** or **bs**.",
)
def get_mapping(entity_id: str, statement: Literal["pl", "bs"] = Query(...), db: Session = Depends(get_db)):
    return (db.query(OntologyMapping)
              .filter(OntologyMapping.entity_id == entity_id,
                      OntologyMapping.statement == statement)
              .order_by(OntologyMapping.sort_order, OntologyMapping.id).all())


@router.put(
    "/{entity_id}",
    response_model=OntologyMappingOut,
    summary="Upsert ontology mapping row",
    description="**409** until entity passes `/integrations/.../api-complete` (GL + Sub-GL + Transaction verified).",
    responses={404: {"description": "Entity not found"}, 409: {"description": "Ontology locked"}},
)
def upsert_mapping(
    entity_id: str, body: OntologyMappingUpsert, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if not db.query(Entity).filter(Entity.id == entity_id).first():
        raise HTTPException(status_code=404, detail="Entity not found")
    if not _api_complete(db, entity_id):
        raise HTTPException(status_code=409,
                            detail="Ontology locked: entity has not completed GL / Sub-GL / Transaction verification")

    row = (db.query(OntologyMapping)
             .filter(OntologyMapping.entity_id == entity_id,
                     OntologyMapping.statement == body.statement,
                     OntologyMapping.tflux_caption == body.tflux_caption)
             .first())
    if not row:
        row = OntologyMapping(entity_id=entity_id, statement=body.statement,
                              tflux_caption=body.tflux_caption)
        db.add(row)
    before = {f: getattr(row, f, None) for f in
              ("erp_gl_json", "erp_subgl_json", "erp_subledger_json",
               "custom_caption", "is_hidden", "display_column")}
    row.erp_gl_json        = body.erp_gl_json
    if body.erp_subledger_json is not None:
        row.erp_subledger_json = body.erp_subledger_json
        if body.erp_subledger_json:
            row.erp_subgl_json = []
    elif body.erp_subgl_json is not None:
        row.erp_subgl_json = body.erp_subgl_json
    row.custom_caption = body.custom_caption
    row.is_hidden      = body.is_hidden
    row.display_column = body.display_column
    row.updated_by     = actor.id
    row.version        = (row.version or 1) + 1

    log_change(db, actor=actor, action="ontology.upsert",
               table_name="ontology_mappings",
               row_pk=f"{entity_id}/{body.statement}/{body.tflux_caption}",
               before=before, after=body.model_dump(), request=request)
    db.commit()
    db.refresh(row)
    return row


@router.put(
    "/{entity_id}/display-column",
    summary="Set display column mode for all ontology mappings of an entity",
    responses={404: {"description": "Entity not found"}, 409: {"description": "Ontology locked"}},
)
def set_display_column(
    entity_id: str, body: OntologyDisplayColumnUpdate, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if not db.query(Entity).filter(Entity.id == entity_id).first():
        raise HTTPException(status_code=404, detail="Entity not found")
    if not _api_complete(db, entity_id):
        raise HTTPException(
            status_code=409,
            detail="Ontology locked: entity has not completed GL / Sub-GL / Transaction verification",
        )
    rows = db.query(OntologyMapping).filter(OntologyMapping.entity_id == entity_id).all()
    for row in rows:
        row.display_column = body.display_column
        row.updated_by = actor.id
        row.version = (row.version or 1) + 1
    log_change(db, actor=actor, action="ontology.display_column",
               table_name="ontology_mappings", row_pk=entity_id,
               after={"display_column": body.display_column, "rows": len(rows)}, request=request)
    db.commit()
    return {"ok": True, "entity_id": entity_id, "display_column": body.display_column, "updated": len(rows)}


@router.patch(
    "/{entity_id}/visibility",
    summary="Set caption visibility",
    description="Query params: `statement`, `tflux_caption`, `is_hidden`.",
)
def toggle_visibility(
    entity_id: str, statement: Literal["pl","bs"], tflux_caption: str,
    is_hidden: bool, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if not db.query(Entity).filter(Entity.id == entity_id).first():
        raise HTTPException(status_code=404, detail="Entity not found")
    if not _api_complete(db, entity_id):
        raise HTTPException(
            status_code=409,
            detail="Ontology locked: entity has not completed GL / Sub-GL / Transaction verification",
        )
    row = (db.query(OntologyMapping)
             .filter(OntologyMapping.entity_id == entity_id,
                     OntologyMapping.statement == statement,
                     OntologyMapping.tflux_caption == tflux_caption)
             .first())
    if not row:
        row = OntologyMapping(entity_id=entity_id, statement=statement,
                              tflux_caption=tflux_caption)
        db.add(row)
    row.is_hidden = is_hidden
    row.updated_by = actor.id
    log_change(db, actor=actor, action="ontology.visibility",
               table_name="ontology_mappings",
               row_pk=f"{entity_id}/{statement}/{tflux_caption}",
               after={"is_hidden": is_hidden}, request=request)
    db.commit()
    return {"ok": True, "is_hidden": is_hidden}


@router.post(
    "/{entity_id}/resync",
    summary="Ontology resync (audit stub)",
    description="Writes audit row; no external ERP call in v1. Does not auto-map GL accounts.",
)
def resync(entity_id: str, request: Request, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not db.query(Entity).filter(Entity.id == entity_id).first():
        raise HTTPException(status_code=404, detail="Entity not found")
    if not _api_complete(db, entity_id):
        raise HTTPException(status_code=409, detail="Ontology locked")
    log_change(db, actor=actor, action="ontology.resync",
               table_name="ontology_mappings", row_pk=entity_id, request=request)
    db.commit()
    return {"ok": True, "entity_id": entity_id}


@router.post(
    "/{entity_id}/reset-mappings",
    summary="Clear manual GL/dimension mappings for entity statement",
    description=(
        "Clears ``erp_gl_json`` and ``erp_subgl_json`` on ontology mappings "
        "and custom captions for the selected statement. Does not delete "
        "synced BC data, credentials, or entity records."
    ),
    responses={404: {"description": "Entity not found"}, 409: {"description": "Ontology locked"}},
)
def reset_mappings(
    entity_id: str,
    request: Request,
    statement: Literal["pl", "bs"] = Query(...),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not db.query(Entity).filter(Entity.id == entity_id).first():
        raise HTTPException(status_code=404, detail="Entity not found")
    if not _api_complete(db, entity_id):
        raise HTTPException(status_code=409, detail="Ontology locked")

    mapping_rows = (
        db.query(OntologyMapping)
        .filter(OntologyMapping.entity_id == entity_id, OntologyMapping.statement == statement)
        .all()
    )
    for row in mapping_rows:
        row.erp_gl_json = []
        row.erp_subgl_json = []
        row.erp_subledger_json = []
        row.updated_by = actor.id
        row.version = (row.version or 1) + 1

    custom_rows = (
        db.query(CustomCaption)
        .filter(CustomCaption.entity_id == entity_id, CustomCaption.statement == statement)
        .all()
    )
    for cap in custom_rows:
        cap.erp_gl_json = []
        cap.erp_subgl_json = []
        cap.erp_subledger_json = []

    log_change(
        db, actor=actor, action="ontology.reset_mappings",
        table_name="ontology_mappings", row_pk=f"{entity_id}/{statement}",
        after={
            "statement": statement,
            "mappings_cleared": len(mapping_rows),
            "custom_captions_cleared": len(custom_rows),
        },
        request=request,
    )
    db.commit()
    return {
        "ok": True,
        "entity_id": entity_id,
        "statement": statement,
        "mappings_cleared": len(mapping_rows),
        "custom_captions_cleared": len(custom_rows),
    }


@router.get(
    "/{entity_id}/export.csv",
    summary="Export ontology CSV",
    description="Downloads `ontology_{entity_id}.csv`.",
)
def export_csv(entity_id: str, db: Session = Depends(get_db)):
    if not db.query(Entity).filter(Entity.id == entity_id).first():
        raise HTTPException(status_code=404, detail="Entity not found")
    if not _api_complete(db, entity_id):
        raise HTTPException(status_code=409, detail="Ontology locked")
    rows = (db.query(OntologyMapping)
              .filter(OntologyMapping.entity_id == entity_id)
              .order_by(OntologyMapping.statement, OntologyMapping.sort_order).all())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["statement","tflux_caption","custom_caption","is_hidden",
                "display_column","erp_gl","erp_subgl"])
    for r in rows:
        w.writerow([r.statement, r.tflux_caption, r.custom_caption or "",
                    "yes" if r.is_hidden else "no", r.display_column,
                    "|".join(r.erp_gl_json or []), "|".join(r.erp_subgl_json or [])])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=ontology_{entity_id}.csv"})


# ── Custom captions ──────────────────────────────────────────
@router.get(
    "/{entity_id}/custom-captions",
    response_model=List[CustomCaptionOut],
    summary="List custom captions",
)
def list_custom(entity_id: str, db: Session = Depends(get_db)):
    return (db.query(CustomCaption)
              .filter(CustomCaption.entity_id == entity_id)
              .order_by(CustomCaption.statement, CustomCaption.sort_order).all())


@router.post(
    "/{entity_id}/custom-captions",
    response_model=CustomCaptionOut,
    status_code=201,
    summary="Add custom caption",
    responses={409: {"description": "Ontology locked (API incomplete)"}},
)
def add_custom(
    entity_id: str, body: CustomCaptionCreate, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if not _api_complete(db, entity_id):
        raise HTTPException(status_code=409, detail="Ontology locked")
    last = (db.query(CustomCaption)
              .filter(CustomCaption.entity_id == entity_id,
                      CustomCaption.statement == body.statement)
              .order_by(CustomCaption.sort_order.desc()).first())
    cap = CustomCaption(
        entity_id=entity_id, statement=body.statement,
        name=body.name, is_subtotal=body.is_subtotal,
        erp_gl_json=body.erp_gl_json,
        erp_subgl_json=body.erp_subgl_json or [],
        erp_subledger_json=body.erp_subledger_json if body.erp_subledger_json is not None else [],
        sort_order=(last.sort_order + 10) if last else 0,
    )
    db.add(cap)
    log_change(db, actor=actor, action="custom_caption.create",
               table_name="custom_captions", row_pk=f"{entity_id}/{body.name}",
               after=body.model_dump(), request=request)
    db.commit()
    db.refresh(cap)
    return cap


@router.delete(
    "/{entity_id}/custom-captions/{cap_id}",
    status_code=204,
    summary="Delete custom caption",
    responses={404: {"description": "Caption not found"}},
)
def delete_custom(
    entity_id: str, cap_id: int, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    cap = db.query(CustomCaption).filter(
        CustomCaption.id == cap_id, CustomCaption.entity_id == entity_id,
    ).first()
    if not cap:
        raise HTTPException(status_code=404, detail="Custom caption not found")
    if not _api_complete(db, entity_id):
        raise HTTPException(status_code=409, detail="Ontology locked")
    log_change(db, actor=actor, action="custom_caption.delete",
               table_name="custom_captions", row_pk=str(cap_id),
               before={"name": cap.name}, request=request)
    db.delete(cap)
    db.commit()
