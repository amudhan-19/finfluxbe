# @file    admin/integrations.py
#
# ERP integration: catalog + entity-mapping + per-layer API credentials.
# In v1 the actual ERP HTTP calls are MOCKED — `test` / `sync` flip status
# to `verified` and record a row in api_sync_runs so the rest of the system
# can rely on the gating logic.

from __future__ import annotations
from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.crypto import encrypt, mask_secret
from app.core.deps import require_role, get_current_user
from app.db.session import get_db
from app.models.models import (
    ErpCatalog, EntityErpMapping, ApiCredential, ApiSyncRun, Entity, User,
)
from app.schemas.schemas import (
    ErpCatalogOut, EntityErpMappingOut, EntityErpMappingCreate,
    ApiCredentialOut, ApiCredentialUpsert,
)

router = APIRouter(dependencies=[Depends(require_role("admin"))])


# ── ERP Catalog ──────────────────────────────────────────────
@router.get(
    "/catalog",
    response_model=List[ErpCatalogOut],
    summary="List ERP catalog",
    description="Supported ERP vendor keys for mapping UI.",
)
def list_catalog(db: Session = Depends(get_db)):
    return db.query(ErpCatalog).order_by(ErpCatalog.name).all()


# ── Entity ↔ ERP Mappings ────────────────────────────────────
@router.get(
    "/entity-erp-mappings",
    response_model=List[EntityErpMappingOut],
    summary="List entity↔ERP mappings",
)
def list_mappings(db: Session = Depends(get_db)):
    return db.query(EntityErpMapping).order_by(EntityErpMapping.entity_id).all()


@router.post(
    "/entity-erp-mappings",
    response_model=EntityErpMappingOut,
    status_code=201,
    summary="Connect entity to ERP",
    description="Idempotent return if mapping already exists.",
    responses={404: {"description": "Entity or ERP key not found"}},
)
def connect_entity_to_erp(
    body: EntityErpMappingCreate, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if not db.query(Entity).filter(Entity.id == body.entity_id).first():
        raise HTTPException(status_code=404, detail="Entity not found")
    if not db.query(ErpCatalog).filter(ErpCatalog.key == body.erp_key).first():
        raise HTTPException(status_code=404, detail="ERP not in catalog")
    existing = db.query(EntityErpMapping).filter(
        EntityErpMapping.entity_id == body.entity_id,
        EntityErpMapping.erp_key   == body.erp_key,
    ).first()
    if existing:
        return existing
    m = EntityErpMapping(entity_id=body.entity_id, erp_key=body.erp_key,
                         status="connected", connected_by=actor.id,
                         last_sync_at=datetime.utcnow())
    db.add(m)
    log_change(db, actor=actor, action="erp.connect",
               table_name="entity_erp_mappings", row_pk=f"{body.entity_id}/{body.erp_key}",
               after=body.model_dump(), request=request)
    db.commit()
    db.refresh(m)
    return m


@router.delete(
    "/entity-erp-mappings/{mapping_id}",
    status_code=204,
    summary="Disconnect ERP mapping",
    responses={404: {"description": "Mapping not found"}},
)
def disconnect(
    mapping_id: int, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    m = db.query(EntityErpMapping).filter(EntityErpMapping.id == mapping_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Mapping not found")
    log_change(db, actor=actor, action="erp.disconnect",
               table_name="entity_erp_mappings", row_pk=str(mapping_id),
               before={"entity_id": m.entity_id, "erp_key": m.erp_key}, request=request)
    db.delete(m)
    db.commit()


@router.post(
    "/entity-erp-mappings/{mapping_id}/resync",
    summary="Touch last_sync_at (stub resync)",
    responses={404: {"description": "Mapping not found"}},
)
def resync(
    mapping_id: int, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    m = db.query(EntityErpMapping).filter(EntityErpMapping.id == mapping_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Mapping not found")
    m.last_sync_at = datetime.utcnow()
    log_change(db, actor=actor, action="erp.resync",
               table_name="entity_erp_mappings", row_pk=str(mapping_id), request=request)
    db.commit()
    return {"id": mapping_id, "last_sync_at": m.last_sync_at}


# ── Per-entity API credentials ───────────────────────────────
@router.get(
    "/entities/{entity_id}/api-map",
    response_model=List[ApiCredentialOut],
    summary="List API credentials for entity",
    description="Secrets are never returned; masked metadata only.",
)
def get_api_map(entity_id: str, db: Session = Depends(get_db)):
    return (db.query(ApiCredential)
              .filter(ApiCredential.entity_id == entity_id)
              .order_by(ApiCredential.layer, ApiCredential.id).all())


@router.post(
    "/entities/{entity_id}/api-map",
    response_model=ApiCredentialOut,
    status_code=201,
    summary="Create or update API credential",
    description="Encrypts optional secret with Fernet; GL layer is singleton per entity.",
    responses={404: {"description": "Entity not found"}},
)
def upsert_api_credential(
    entity_id: str, body: ApiCredentialUpsert, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if not db.query(Entity).filter(Entity.id == entity_id).first():
        raise HTTPException(status_code=404, detail="Entity not found")

    # GL is singleton per entity; sub_gl/transaction allow many items by name
    q = db.query(ApiCredential).filter(
        ApiCredential.entity_id == entity_id,
        ApiCredential.layer == body.layer,
    )
    if body.layer != "gl":
        q = q.filter(ApiCredential.item_name == body.item_name)
    cred = q.first()

    if not cred:
        cred = ApiCredential(entity_id=entity_id, layer=body.layer,
                             item_name=body.item_name)
        db.add(cred)
    cred.endpoint = body.endpoint
    cred.auth_type = body.auth_type
    if body.secret:
        cred.encrypted_secret = encrypt(body.secret)
        cred.last4 = mask_secret(body.secret)[-4:]
    cred.status = "unverified"

    log_change(db, actor=actor, action="api_cred.upsert",
               table_name="api_credentials", row_pk=f"{entity_id}/{body.layer}/{body.item_name}",
               after={"endpoint": body.endpoint, "auth_type": body.auth_type,
                      "has_secret": bool(body.secret)},
               request=request)
    db.commit()
    db.refresh(cred)
    return cred


@router.delete(
    "/entities/{entity_id}/api-map/{cred_id}",
    status_code=204,
    summary="Delete API credential",
    responses={404: {"description": "Credential not found"}},
)
def delete_api_credential(
    entity_id: str, cred_id: int, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    cred = db.query(ApiCredential).filter(
        ApiCredential.id == cred_id, ApiCredential.entity_id == entity_id,
    ).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    log_change(db, actor=actor, action="api_cred.delete",
               table_name="api_credentials", row_pk=str(cred_id),
               before={"layer": cred.layer, "item_name": cred.item_name}, request=request)
    db.delete(cred)
    db.commit()


@router.post(
    "/entities/{entity_id}/api-map/{cred_id}/test",
    summary="Test credential (mock success)",
    description="v1 always marks credential **verified**.",
    responses={404: {"description": "Credential not found"}},
)
def test_credential(
    entity_id: str, cred_id: int, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    cred = db.query(ApiCredential).filter(
        ApiCredential.id == cred_id, ApiCredential.entity_id == entity_id,
    ).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    # MOCK: always succeeds in v1
    cred.status = "verified"
    cred.last_tested_at = datetime.utcnow()
    log_change(db, actor=actor, action="api_cred.test",
               table_name="api_credentials", row_pk=str(cred_id),
               after={"ok": True}, request=request)
    db.commit()
    return {"id": cred_id, "status": cred.status, "tested_at": cred.last_tested_at}


@router.post(
    "/entities/{entity_id}/api-map/{cred_id}/sync",
    summary="Sync credential (mock)",
    description="Writes **api_sync_runs** row and marks verified.",
    responses={404: {"description": "Credential not found"}},
)
def sync_credential(
    entity_id: str, cred_id: int, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    cred = db.query(ApiCredential).filter(
        ApiCredential.id == cred_id, ApiCredential.entity_id == entity_id,
    ).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    run = ApiSyncRun(credential_id=cred.id, started_at=datetime.utcnow(),
                     finished_at=datetime.utcnow(), ok=True, rows_synced=42)
    db.add(run)
    cred.last_synced_at = run.finished_at
    if cred.status != "verified":
        cred.status = "verified"
    log_change(db, actor=actor, action="api_cred.sync",
               table_name="api_credentials", row_pk=str(cred_id),
               after={"rows": run.rows_synced}, request=request)
    db.commit()
    return {"id": cred_id, "rows": run.rows_synced, "synced_at": run.finished_at}


@router.get(
    "/entities/{entity_id}/api-complete",
    summary="ERP API completeness check",
    description=(
        "Business Central entities: derived from real synced data — credential "
        "verified, active company set, and GL accounts + general ledger "
        "entries each have a successful sync run with rows > 0.\n\n"
        "Other ERPs: True when GL, at least one Sub-GL, and at least one "
        "Transaction credential row is `verified` (legacy mock connectors)."
    ),
)
def api_complete(entity_id: str, db: Session = Depends(get_db)):
    # Local imports keep the module load order safe — both modules are loaded
    # before this handler ever runs, but the import-from is at request time
    # to avoid any chance of a circular import surfacing at startup.
    from app.routers.admin.business_central import (
        ERP_TYPE_BC,
        calculate_business_central_readiness,
    )
    is_bc = (db.query(EntityErpMapping)
               .filter(EntityErpMapping.entity_id == entity_id,
                       EntityErpMapping.erp_key == ERP_TYPE_BC)
               .first() is not None)
    if is_bc:
        readiness = calculate_business_central_readiness(db, entity_id)
        return {
            "complete":    bool(readiness["api_complete"]),
            "gl":          bool(readiness["gl_ready"]),
            "sub_gl":      bool(readiness["sub_gl_ready"]),
            "transaction": bool(readiness["transaction_ready"]),
            "erp_type":    "business_central",
        }
    from app.models.models import ERP_TYPE_QUICKBOOKS
    from app.routers.admin.quickbooks import calculate_quickbooks_readiness
    is_qb = (db.query(EntityErpMapping)
               .filter(EntityErpMapping.entity_id == entity_id,
                       EntityErpMapping.erp_key == ERP_TYPE_QUICKBOOKS)
               .first() is not None)
    if is_qb:
        readiness = calculate_quickbooks_readiness(db, entity_id)
        return {
            "complete":    bool(readiness["api_complete"]),
            "gl":          bool(readiness["gl_ready"]),
            "sub_gl":      False,
            "transaction": bool(readiness["transaction_ready"]),
            "erp_type":    "quickbooks",
        }
    creds = db.query(ApiCredential).filter(ApiCredential.entity_id == entity_id).all()
    has_gl   = any(c.layer == "gl"          and c.status == "verified" for c in creds)
    has_sgl  = any(c.layer == "sub_gl"      and c.status == "verified" for c in creds)
    has_txn  = any(c.layer == "transaction" and c.status == "verified" for c in creds)
    return {"complete": has_gl and has_sgl and has_txn,
            "gl": has_gl, "sub_gl": has_sgl, "transaction": has_txn}
