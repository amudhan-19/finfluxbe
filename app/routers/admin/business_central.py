# @file    routers/admin/business_central.py
#
# Admin Console — Business Central credential management.
#
# Mounted under /api/admin/integrations/business-central. Every route is
# admin-only (router-level dependency). Plaintext credentials never leave
# the service layer; responses always use the `BcCredentialOut` shape which
# carries only safe metadata (status, environment, base URL, masked tails,
# selected company, timestamps).

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.models import (
    BusinessCentralCredential,
    Entity,
    EntityErpMapping,
    ErpCompany,
    ErpDimensionValue,
    ErpGlAccount,
    ErpCustomer,
    ErpCustomerLedgerEntry,
    ErpGeneralLedgerEntry,
    ErpVendor,
    ErpVendorLedgerEntry,
    ErpSyncRun,
    SYNC_FETCH_COMPANIES,
    SYNC_GL_ACCOUNTS,
    SYNC_DIMENSIONS,
    SYNC_GL_ENTRIES,
    SYNC_CUSTOMERS,
    SYNC_VENDORS,
    SYNC_CUSTOMER_LEDGER_ENTRIES,
    SYNC_VENDOR_LEDGER_ENTRIES,
    SYNC_TEST_CONNECTION,
    User,
)
from app.services.erp.business_central import (
    DEFAULT_BC_BASE_URL,
    DEFAULT_BC_SCOPE,
    encrypt_credential_field,
    fetch_companies as svc_fetch_companies,
    mask_for_display,
    sync_customer_ledger_entries as svc_sync_customer_ledger,
    sync_customers as svc_sync_customers,
    sync_dimensions as svc_sync_dimensions,
    sync_general_ledger_entries as svc_sync_gl_entries,
    sync_gl_accounts as svc_sync_gl_accounts,
    sync_vendor_ledger_entries as svc_sync_vendor_ledger,
    sync_vendors as svc_sync_vendors,
    test_connection as svc_test_connection,
)
from app.services.erp.safe_errors import (
    BC_CUSTOMER_LEDGER_ENDPOINT_NOT_FOUND,
    BC_VENDOR_LEDGER_ENDPOINT_NOT_FOUND,
    DATABASE_ERROR,
    DB_SCHEMA_MISMATCH,
    ENTITY_ERP_MAPPING_REQUIRED,
    GL_ACCOUNTS_NOT_SYNCED,
    ENDPOINT_NOT_FOUND,
    GL_ENTRIES_NOT_SYNCED,
    INVALID_CONFIG,
    SafeError,
    safe_error,
)

_log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_role("admin"))])


def _notify_bc_failure_safe(
    db: Session, entity_id: str, actor_id: int, error_msg: str | None
) -> None:
    """Notify all admins of a BC sync failure. Never raises."""
    try:
        from app.services.notification_service import NOTIF_BC_SYNC_FAILED, notify_admins
        entity = db.query(Entity).filter(Entity.id == entity_id).first()
        entity_name = entity.name if entity else entity_id
        safe_msg = (error_msg or "Unknown error")[:200]
        notify_admins(
            db,
            notif_type=NOTIF_BC_SYNC_FAILED,
            title="BC Sync Failed",
            message=f"Business Central sync failed for {entity_name}: {safe_msg}",
            actor_id=actor_id,
            entity_id=entity_id,
            link_url=f"/admin/integrations?entity={entity_id}",
            skip_self=False,
        )
    except Exception:
        _log.warning("BC failure notification skipped (non-blocking)", exc_info=False)

ERP_TYPE_BC = "bc"

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ──────────────────────────────────────────────────────────────────────
# Schemas — request bodies and safe response shapes only.
# ──────────────────────────────────────────────────────────────────────
class BcCredentialUpsert(BaseModel):
    """Body accepted by POST .../credentials.

    On first save (no existing row) tenant_id, client_id, client_secret,
    environment_name, scope and base_url are all required; subsequent
    updates may omit any field to keep the stored value unchanged.
    """
    model_config = ConfigDict(extra="forbid")

    entity_id:        str
    tenant_id:        Optional[str] = None
    client_id:        Optional[str] = None
    client_secret:    Optional[str] = None
    environment_name: Optional[str] = Field(default=None, max_length=100)
    scope:            Optional[str] = Field(default=None, max_length=255)
    base_url:         Optional[str] = Field(default=None, max_length=255)
    company_id:       Optional[str] = Field(default=None, max_length=100)
    company_name:     Optional[str] = Field(default=None, max_length=200)

    @field_validator("tenant_id", "client_id")
    @classmethod
    def _validate_guid(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            return None
        if not _GUID_RE.match(v):
            raise ValueError("must be a GUID like 00000000-0000-0000-0000-000000000000")
        return v

    @field_validator("client_secret")
    @classmethod
    def _validate_secret(cls, v: Optional[str]) -> Optional[str]:
        # Validation NEVER echoes the value (Pydantic includes the field name only).
        if v is None:
            return v
        v = v.strip()
        return v or None

    @field_validator("environment_name", "scope", "base_url", "company_id", "company_name")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        return v or None


class BcTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str


class BcSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id:  str
    company_id: Optional[str] = Field(default=None, max_length=100)


class BcGlEntrySyncRequest(BaseModel):
    """Adds optional from/to date for the GL entries sync."""
    model_config = ConfigDict(extra="forbid")
    entity_id:  str
    company_id: Optional[str] = Field(default=None, max_length=100)
    from_date:  Optional[str] = Field(default=None, max_length=20,
                                      description="ISO date e.g. 2025-01-01")
    to_date:    Optional[str] = Field(default=None, max_length=20,
                                      description="ISO date e.g. 2025-12-31")


class BcCompanyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id:    str
    company_id:   str = Field(min_length=1, max_length=100)
    company_name: str = Field(min_length=1, max_length=200)


class BcCredentialOut(BaseModel):
    """Safe response — never includes plaintext or ciphertext of secrets."""
    credential_id:        int
    entity_id:            str
    erp_type:             str
    environment_name:     str
    scope:                str
    base_url:             str
    company_id:           Optional[str] = None
    company_name:         Optional[str] = None
    masked_client_id:     Optional[str] = None
    masked_client_secret: Optional[str] = None
    status:               str
    last_safe_error_code: Optional[str] = None
    last_safe_error_message: Optional[str] = None
    last_tested_at:       Optional[datetime] = None
    last_verified_at:     Optional[datetime] = None
    created_at:           datetime
    updated_at:           datetime


class BcCompanyOut(BaseModel):
    company_id:   str
    company_name: str


class BcSyncRunOut(BaseModel):
    id:                 int
    sync_type:          str
    status:             str
    started_at:         datetime
    finished_at:        Optional[datetime] = None
    safe_error_code:    Optional[str] = None
    safe_error_message: Optional[str] = None
    records_processed:  Optional[int] = None


class BcConnectorResponse(BaseModel):
    """Returned by test/sync endpoints. Never carries tokens or raw payloads."""
    ok:                  bool
    status:              str
    layer_status:        Optional[str] = None
    safe_error_code:     Optional[str] = None
    safe_error_message:  Optional[str] = None
    records_processed:   Optional[int] = None
    companies:           List[BcCompanyOut] = Field(default_factory=list)
    sync_run:            BcSyncRunOut


class BcReadinessOut(BaseModel):
    entity_code:         str
    erp_type:            str = "business_central"
    credential_verified: bool
    company_selected:    bool
    gl_ready:            bool
    sub_gl_ready:        bool
    transaction_ready:   bool
    api_complete:        bool
    ready_count:         int
    required_ready_count: int = 0
    required_ready_total: int = 2
    customers_ready: bool = False
    vendors_ready: bool = False
    customers_count: Optional[int] = None
    vendors_count: Optional[int] = None
    customers_last_synced_at: Optional[datetime] = None
    vendors_last_synced_at: Optional[datetime] = None
    customer_ledger_ready: bool = False
    vendor_ledger_ready: bool = False
    customer_ledger_count: Optional[int] = None
    vendor_ledger_count: Optional[int] = None
    customer_ledger_last_synced_at: Optional[datetime] = None
    vendor_ledger_last_synced_at: Optional[datetime] = None
    customer_ledger_unavailable: bool = False
    vendor_ledger_unavailable: bool = False
    customer_ledger_optional_message: Optional[str] = None
    vendor_ledger_optional_message: Optional[str] = None
    optional_warnings: List[str] = Field(default_factory=list)
    missing_steps:       List[str]
    gl_account_count:           Optional[int] = None
    dimension_value_count:      Optional[int] = None
    general_ledger_entry_count: Optional[int] = None
    gl_last_synced_at:          Optional[datetime] = None
    dimensions_last_synced_at:  Optional[datetime] = None
    entries_last_synced_at:     Optional[datetime] = None
    active_company_id:   Optional[str] = None
    active_company_name: Optional[str] = None


class ErpAccountOut(BaseModel):
    """Public view of a synced ERP GL account row. Never returns raw_source."""
    company_id:       str
    account_no:       str
    display_name:     Optional[str] = None
    account_name:     Optional[str] = None
    account_category: Optional[str] = None
    sub_category:     Optional[str] = None
    account_type:     Optional[str] = None
    blocked:          Optional[bool] = None
    external_id:      Optional[str] = None
    last_synced_at:   Optional[datetime] = None


class ErpDimensionValueOut(BaseModel):
    company_id:     str
    dimension_code: str
    dimension_name: Optional[str] = None
    value_code:     str
    value_name:     Optional[str] = None
    display_name:   Optional[str] = None
    last_synced_at: Optional[datetime] = None


class ErpSubledgerOptionOut(BaseModel):
    """Selectable sub-ledger detail derived from synced GL entries."""
    value: str
    label: str
    kind: str


class ErpGlEntryOut(BaseModel):
    company_id:        str
    external_entry_no: str
    entry_number:      Optional[str] = None
    posting_date:      Optional[datetime] = None
    document_no:       Optional[str] = None
    account_no:        Optional[str] = None
    description:       Optional[str] = None
    debit_amount:      Optional[float] = None
    credit_amount:     Optional[float] = None
    amount:            Optional[float] = None
    last_synced_at:    Optional[datetime] = None


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _to_safe_out(rec: BusinessCentralCredential) -> BcCredentialOut:
    return BcCredentialOut(
        credential_id=rec.id,
        entity_id=rec.entity_id,
        erp_type=rec.erp_type,
        environment_name=rec.environment_name,
        scope=rec.scope,
        base_url=rec.base_url,
        company_id=rec.company_id,
        company_name=rec.company_name,
        masked_client_id=("*" * 8 + rec.last4_client) if rec.last4_client else None,
        masked_client_secret=("*" * 8 + rec.last4_secret) if rec.last4_secret else None,
        status=rec.status,
        last_safe_error_code=rec.last_safe_error_code,
        last_safe_error_message=rec.last_safe_error_message,
        last_tested_at=rec.last_tested_at,
        last_verified_at=rec.last_verified_at,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


def _require_entity(db: Session, entity_id: str) -> Entity:
    ent = db.query(Entity).filter(Entity.id == entity_id).first()
    if not ent:
        raise HTTPException(status_code=404, detail="Entity not found")
    return ent


def _get_credential(db: Session, entity_id: str) -> Optional[BusinessCentralCredential]:
    return (db.query(BusinessCentralCredential)
              .filter(BusinessCentralCredential.entity_id == entity_id)
              .first())


def _require_credential(db: Session, entity_id: str) -> BusinessCentralCredential:
    rec = _get_credential(db, entity_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Business Central credentials not configured")
    return rec


def _require_bc_mapping(db: Session, entity_id: str) -> EntityErpMapping:
    """Confirms the entity is mapped to Business Central. Returns 409 with a
    safe code so the frontend can render a clear next step (Connect ERP)."""
    mapping = (db.query(EntityErpMapping)
                 .filter(EntityErpMapping.entity_id == entity_id,
                         EntityErpMapping.erp_key == ERP_TYPE_BC)
                 .first())
    if not mapping:
        err = safe_error(ENTITY_ERP_MAPPING_REQUIRED)
        raise HTTPException(
            status_code=409,
            detail={"safe_error_code": err.code, "safe_error_message": err.message},
        )
    return mapping


def _credential_is_invalid_config(rec: BusinessCentralCredential) -> bool:
    """True when the stored row was last marked unreadable/invalid by a
    previous test/sync. The admin must re-enter the secret before saving
    can leave the row in a usable state."""
    return rec.status == "failed" and rec.last_safe_error_code == INVALID_CONFIG


def _reset_bc_data(db: Session, entity_id: str) -> None:
    """Removes BC-derived data for a given entity (companies, GL chart,
    dimensions and GL entries). Used when the credential row is being
    deleted/replaced."""
    db.query(ErpCompany).filter(
        ErpCompany.entity_id == entity_id,
        ErpCompany.erp_type == ERP_TYPE_BC,
    ).delete(synchronize_session=False)
    db.query(ErpGlAccount).filter(
        ErpGlAccount.entity_id == entity_id,
        ErpGlAccount.erp_type == ERP_TYPE_BC,
    ).delete(synchronize_session=False)
    db.query(ErpDimensionValue).filter(
        ErpDimensionValue.entity_id == entity_id,
        ErpDimensionValue.erp_type == ERP_TYPE_BC,
    ).delete(synchronize_session=False)
    db.query(ErpGeneralLedgerEntry).filter(
        ErpGeneralLedgerEntry.entity_id == entity_id,
        ErpGeneralLedgerEntry.erp_type == ERP_TYPE_BC,
    ).delete(synchronize_session=False)
    db.query(ErpCustomer).filter(
        ErpCustomer.entity_id == entity_id,
        ErpCustomer.erp_type == ERP_TYPE_BC,
    ).delete(synchronize_session=False)
    db.query(ErpVendor).filter(
        ErpVendor.entity_id == entity_id,
        ErpVendor.erp_type == ERP_TYPE_BC,
    ).delete(synchronize_session=False)
    db.query(ErpCustomerLedgerEntry).filter(
        ErpCustomerLedgerEntry.entity_id == entity_id,
        ErpCustomerLedgerEntry.erp_type == ERP_TYPE_BC,
    ).delete(synchronize_session=False)
    db.query(ErpVendorLedgerEntry).filter(
        ErpVendorLedgerEntry.entity_id == entity_id,
        ErpVendorLedgerEntry.erp_type == ERP_TYPE_BC,
    ).delete(synchronize_session=False)


def _start_sync_run(db: Session, *, entity_id: str, sync_type: str, actor: User) -> ErpSyncRun:
    run = ErpSyncRun(
        entity_id=entity_id, erp_type=ERP_TYPE_BC, sync_type=sync_type,
        status="running", started_at=datetime.utcnow(), created_by=actor.id,
    )
    db.add(run)
    db.flush()
    return run


def _finish_sync_run(
    run: ErpSyncRun, *, ok: bool,
    error: Optional[SafeError] = None, records_processed: Optional[int] = None,
) -> None:
    run.finished_at = datetime.utcnow()
    run.status = "success" if ok else "failed"
    run.records_processed = records_processed
    if error:
        run.safe_error_code = error.code
        run.safe_error_message = error.message


def _finish_sync_run_optional_unavailable(
    run: ErpSyncRun, error: SafeError,
) -> None:
    """Optional ledger API missing — record as success with 0 rows (non-blocking)."""
    msg = error.message or ""
    if "skipped" not in msg.lower():
        msg = f"{msg} This optional enrichment will be skipped."
    run.finished_at = datetime.utcnow()
    run.status = "success"
    run.records_processed = 0
    run.safe_error_code = error.code
    run.safe_error_message = msg


def _connector_response_optional_unavailable(
    rec: BusinessCentralCredential,
    run: ErpSyncRun,
    error: SafeError,
) -> BcConnectorResponse:
    return BcConnectorResponse(
        ok=True,
        status=rec.status,
        layer_status="optional_unavailable",
        safe_error_code=error.code,
        safe_error_message=run.safe_error_message,
        records_processed=0,
        sync_run=_to_run_out(run),
    )


def _to_run_out(run: ErpSyncRun) -> BcSyncRunOut:
    return BcSyncRunOut(
        id=run.id or 0, sync_type=run.sync_type, status=run.status,
        started_at=run.started_at, finished_at=run.finished_at,
        safe_error_code=run.safe_error_code, safe_error_message=run.safe_error_message,
        records_processed=run.records_processed,
    )


# ── DB-failure mapping ────────────────────────────────────────────────
# Sync routes wrap their persist+commit step so a schema drift or other
# database error becomes a structured safe-error response instead of a
# raw 500 with a stack trace.

# Substrings that strongly suggest the model/table schema is out of sync.
_SCHEMA_DRIFT_MARKERS = (
    "Unknown column",          # MySQL 1054
    "no such column",          # SQLite
    "doesn't exist",           # MySQL 1146
    "Unknown table",           # MySQL 1051
    "does not exist",          # PostgreSQL
)


def _classify_db_failure(exc: SQLAlchemyError) -> SafeError:
    """Map a low-level database exception to a SafeError. The original
    exception text is inspected only for a small whitelist of well-known
    markers and is **never** returned to the caller."""
    raw = str(getattr(exc, "orig", "") or exc)
    for marker in _SCHEMA_DRIFT_MARKERS:
        if marker in raw:
            return safe_error(DB_SCHEMA_MISMATCH)
    return safe_error(DATABASE_ERROR)


def _record_sync_failure(
    db: Session, *, entity_id: str, sync_type: str,
    error: SafeError, actor: User,
) -> ErpSyncRun:
    """Reopen a fresh transaction and persist a single failed sync_run row
    plus the failed credential status. Caller must have already issued a
    rollback. Returns the new run for the response payload. Never raises."""
    db.rollback()
    try:
        rec = _get_credential(db, entity_id)
        if rec is not None:
            rec.status = "failed"
            rec.last_safe_error_code = error.code
            rec.last_safe_error_message = error.message
        run = ErpSyncRun(
            entity_id=entity_id, erp_type=ERP_TYPE_BC, sync_type=sync_type,
            status="failed",
            started_at=datetime.utcnow(), finished_at=datetime.utcnow(),
            safe_error_code=error.code, safe_error_message=error.message,
            records_processed=0, created_by=actor.id,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run
    except SQLAlchemyError:
        db.rollback()
        _log.exception("Could not persist failed sync_run for %s/%s",
                       entity_id, sync_type)
        # Return a synthetic, un-persisted run so the response is still well-formed.
        return ErpSyncRun(
            id=0, entity_id=entity_id, erp_type=ERP_TYPE_BC, sync_type=sync_type,
            status="failed",
            started_at=datetime.utcnow(), finished_at=datetime.utcnow(),
            safe_error_code=error.code, safe_error_message=error.message,
            records_processed=0,
        )


def _safe_db_failure_response(
    db: Session, exc: SQLAlchemyError, *, entity_id: str,
    sync_type: str, actor: User,
) -> BcConnectorResponse:
    """One-stop helper: classify, persist failure, build the response."""
    err = _classify_db_failure(exc)
    failed_run = _record_sync_failure(
        db, entity_id=entity_id, sync_type=sync_type,
        error=err, actor=actor,
    )
    return BcConnectorResponse(
        ok=False, status="failed",
        safe_error_code=err.code, safe_error_message=err.message,
        records_processed=0, sync_run=_to_run_out(failed_run),
    )


def _replace_companies(db: Session, entity_id: str, rows: List[dict]) -> int:
    db.query(ErpCompany).filter(
        ErpCompany.entity_id == entity_id,
        ErpCompany.erp_type == ERP_TYPE_BC,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpCompany(
            entity_id=entity_id, erp_type=ERP_TYPE_BC,
            company_id=r["company_id"], company_name=r["company_name"],
        ))
    return len(rows)


def _replace_accounts(db: Session, entity_id: str, company_id: str, rows: List[dict]) -> int:
    """Atomically rewrites the entity/company GL chart with the latest sync
    payload. The full row payload (including raw source) is stored once;
    callers don't need a second round-trip to read the rich fields."""
    now = datetime.utcnow()
    db.query(ErpGlAccount).filter(
        ErpGlAccount.entity_id == entity_id,
        ErpGlAccount.erp_type == ERP_TYPE_BC,
        ErpGlAccount.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpGlAccount(
            entity_id=entity_id, erp_type=ERP_TYPE_BC, company_id=company_id,
            account_no=r["account_no"],
            account_name=r.get("account_name"),
            display_name=r.get("display_name"),
            account_category=r.get("account_category"),
            sub_category=r.get("sub_category"),
            account_type=r.get("account_type"),
            blocked=r.get("blocked"),
            external_id=r.get("external_id"),
            raw_source_json=r.get("raw_source_json"),
            last_synced_at=now,
        ))
    return len(rows)


def _replace_dimensions(db: Session, entity_id: str, company_id: str, rows: List[dict]) -> int:
    """Atomically rewrites the entity/company dimension chart."""
    now = datetime.utcnow()
    db.query(ErpDimensionValue).filter(
        ErpDimensionValue.entity_id == entity_id,
        ErpDimensionValue.erp_type == ERP_TYPE_BC,
        ErpDimensionValue.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpDimensionValue(
            entity_id=entity_id, erp_type=ERP_TYPE_BC, company_id=company_id,
            dimension_id=r.get("dimension_id"),
            dimension_code=r["dimension_code"],
            dimension_name=r.get("dimension_name"),
            value_id=r.get("value_id"),
            value_code=r["value_code"],
            value_name=r.get("value_name"),
            display_name=r.get("display_name"),
            raw_source_json=r.get("raw_source_json"),
            last_synced_at=now,
        ))
    return len(rows)


def _replace_gl_entries(db: Session, entity_id: str, company_id: str, rows: List[dict]) -> int:
    """Atomically rewrites the entity/company GL entry table for the period."""
    now = datetime.utcnow()
    db.query(ErpGeneralLedgerEntry).filter(
        ErpGeneralLedgerEntry.entity_id == entity_id,
        ErpGeneralLedgerEntry.erp_type == ERP_TYPE_BC,
        ErpGeneralLedgerEntry.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpGeneralLedgerEntry(
            entity_id=entity_id, erp_type=ERP_TYPE_BC, company_id=company_id,
            external_entry_no=r["external_entry_no"],
            external_id=r.get("external_id"),
            entry_number=r.get("entry_number"),
            posting_date=r.get("posting_date"),
            document_no=r.get("document_no"),
            account_id=r.get("account_id"),
            account_no=r.get("account_no"),
            debit_amount=r.get("debit_amount"),
            credit_amount=r.get("credit_amount"),
            amount=r.get("amount"),
            dimension_set_id=r.get("dimension_set_id"),
            description=r.get("description"),
            source_type=r.get("source_type"),
            source_number=r.get("source_number"),
            bal_account_type=r.get("bal_account_type"),
            bal_account_number=r.get("bal_account_number"),
            external_document_number=r.get("external_document_number"),
            raw_source_json=r.get("raw_source_json"),
            last_synced_at=now,
        ))
    return len(rows)


def _replace_customers(db: Session, entity_id: str, company_id: str, rows: List[dict]) -> int:
    now = datetime.utcnow()
    db.query(ErpCustomer).filter(
        ErpCustomer.entity_id == entity_id,
        ErpCustomer.erp_type == ERP_TYPE_BC,
        ErpCustomer.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpCustomer(
            entity_id=entity_id, erp_type=ERP_TYPE_BC, company_id=company_id,
            customer_no=r["customer_no"],
            display_name=r.get("display_name"),
            external_id=r.get("external_id"),
            raw_source_json=r.get("raw_source_json"),
            last_synced_at=now,
        ))
    return len(rows)


def _replace_vendors(db: Session, entity_id: str, company_id: str, rows: List[dict]) -> int:
    now = datetime.utcnow()
    db.query(ErpVendor).filter(
        ErpVendor.entity_id == entity_id,
        ErpVendor.erp_type == ERP_TYPE_BC,
        ErpVendor.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpVendor(
            entity_id=entity_id, erp_type=ERP_TYPE_BC, company_id=company_id,
            vendor_no=r["vendor_no"],
            display_name=r.get("display_name"),
            external_id=r.get("external_id"),
            raw_source_json=r.get("raw_source_json"),
            last_synced_at=now,
        ))
    return len(rows)


def _replace_customer_ledger_entries(
    db: Session, entity_id: str, company_id: str, rows: List[dict],
) -> int:
    now = datetime.utcnow()
    db.query(ErpCustomerLedgerEntry).filter(
        ErpCustomerLedgerEntry.entity_id == entity_id,
        ErpCustomerLedgerEntry.erp_type == ERP_TYPE_BC,
        ErpCustomerLedgerEntry.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpCustomerLedgerEntry(
            entity_id=entity_id, erp_type=ERP_TYPE_BC, company_id=company_id,
            external_entry_no=r["external_entry_no"],
            customer_no=r["customer_no"],
            customer_name=r.get("customer_name"),
            document_no=r.get("document_no"),
            posting_date=r.get("posting_date"),
            amount=r.get("amount"),
            remaining_amount=r.get("remaining_amount"),
            raw_source_json=r.get("raw_source_json"),
            last_synced_at=now,
        ))
    return len(rows)


def _replace_vendor_ledger_entries(
    db: Session, entity_id: str, company_id: str, rows: List[dict],
) -> int:
    now = datetime.utcnow()
    db.query(ErpVendorLedgerEntry).filter(
        ErpVendorLedgerEntry.entity_id == entity_id,
        ErpVendorLedgerEntry.erp_type == ERP_TYPE_BC,
        ErpVendorLedgerEntry.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpVendorLedgerEntry(
            entity_id=entity_id, erp_type=ERP_TYPE_BC, company_id=company_id,
            external_entry_no=r["external_entry_no"],
            vendor_no=r["vendor_no"],
            vendor_name=r.get("vendor_name"),
            document_no=r.get("document_no"),
            posting_date=r.get("posting_date"),
            amount=r.get("amount"),
            remaining_amount=r.get("remaining_amount"),
            raw_source_json=r.get("raw_source_json"),
            last_synced_at=now,
        ))
    return len(rows)


# ──────────────────────────────────────────────────────────────────────
# Readiness — purely DB-derived. No external calls.
# ──────────────────────────────────────────────────────────────────────
_BC_GL_SYNC_TYPE      = SYNC_GL_ACCOUNTS
_BC_DIM_SYNC_TYPE     = SYNC_DIMENSIONS
_BC_TXN_SYNC_TYPE     = SYNC_GL_ENTRIES
_BC_CUSTOMERS_SYNC_TYPE = SYNC_CUSTOMERS
_BC_VENDORS_SYNC_TYPE   = SYNC_VENDORS
_BC_CUST_SYNC_TYPE    = SYNC_CUSTOMER_LEDGER_ENTRIES
_BC_VEND_SYNC_TYPE    = SYNC_VENDOR_LEDGER_ENTRIES

_LEDGER_OPTIONAL_MESSAGE = (
    "Customer/Vendor ledger entries are not exposed in this Business Central API. "
    "Custom API pages are required for ledger-level details."
)

_OPTIONAL_LEDGER_ERRORS = frozenset({
    BC_CUSTOMER_LEDGER_ENDPOINT_NOT_FOUND,
    BC_VENDOR_LEDGER_ENDPOINT_NOT_FOUND,
})


def _last_successful_run(
    db: Session, entity_id: str, sync_type: str,
) -> Optional[ErpSyncRun]:
    """Latest sync_runs row for (entity, type) with status=success."""
    return (db.query(ErpSyncRun)
              .filter(ErpSyncRun.entity_id == entity_id,
                      ErpSyncRun.erp_type == ERP_TYPE_BC,
                      ErpSyncRun.sync_type == sync_type,
                      ErpSyncRun.status == "success")
              .order_by(ErpSyncRun.started_at.desc())
              .first())


def _last_sync_run(
    db: Session, entity_id: str, sync_type: str,
) -> Optional[ErpSyncRun]:
    """Latest sync_runs row for (entity, type), any status."""
    return (db.query(ErpSyncRun)
              .filter(ErpSyncRun.entity_id == entity_id,
                      ErpSyncRun.erp_type == ERP_TYPE_BC,
                      ErpSyncRun.sync_type == sync_type)
              .order_by(ErpSyncRun.started_at.desc())
              .first())


def _optional_ledger_endpoint_unavailable(
    db: Session, entity_id: str, sync_type: str,
) -> tuple[bool, Optional[str]]:
    """True when the latest attempt recorded an optional endpoint-missing code."""
    run = _last_sync_run(db, entity_id, sync_type)
    if run and run.safe_error_code in _OPTIONAL_LEDGER_ERRORS:
        return True, run.safe_error_message
    return False, None


def calculate_business_central_readiness(
    db: Session, entity_id: str,
) -> dict[str, object]:
    """Pure DB inspection — does NOT call Business Central.
    Returns the readiness payload the Admin Console + ontology gate consume."""
    cred = _get_credential(db, entity_id)
    cred_verified = bool(cred and cred.status in ("verified", "syncing", "synced"))
    company_selected = bool(cred and cred.company_id)

    def _sync_ready(stype: str) -> tuple[bool, Optional[int], Optional[datetime]]:
        run = _last_successful_run(db, entity_id, stype)
        if not run:
            return (False, None, None)
        records = run.records_processed or 0
        # Dimension / Sub-GL: BC may return zero values after a healthy sync
        # (or normalizer drops rows). GL and GL entries still require rows > 0.
        if stype == _BC_DIM_SYNC_TYPE:
            return (True, records, run.finished_at or run.started_at)
        return (records > 0, records, run.finished_at or run.started_at)

    gl_ready,  gl_count,  gl_at  = _sync_ready(_BC_GL_SYNC_TYPE)
    sgl_ready, sgl_count, sgl_at = _sync_ready(_BC_DIM_SYNC_TYPE)
    txn_ready, txn_count, txn_at = _sync_ready(_BC_TXN_SYNC_TYPE)
    customers_ready, customers_count, customers_at = _sync_ready(_BC_CUSTOMERS_SYNC_TYPE)
    vendors_ready, vendors_count, vendors_at = _sync_ready(_BC_VENDORS_SYNC_TYPE)
    cust_ready, cust_count, cust_at = _sync_ready(_BC_CUST_SYNC_TYPE)
    vend_ready, vend_count, vend_at = _sync_ready(_BC_VEND_SYNC_TYPE)
    cust_unavail, cust_unavail_msg = _optional_ledger_endpoint_unavailable(
        db, entity_id, _BC_CUST_SYNC_TYPE,
    )
    vend_unavail, vend_unavail_msg = _optional_ledger_endpoint_unavailable(
        db, entity_id, _BC_VEND_SYNC_TYPE,
    )
    if cust_unavail and not cust_unavail_msg:
        cust_unavail_msg = _LEDGER_OPTIONAL_MESSAGE
    if vend_unavail and not vend_unavail_msg:
        vend_unavail_msg = _LEDGER_OPTIONAL_MESSAGE

    required_layers = [gl_ready, txn_ready]
    api_complete = bool(cred_verified and company_selected and all(required_layers))

    missing: List[str] = []
    if not cred_verified:
        missing.append("verify_connection")
    if not company_selected:
        missing.append("select_company")
    if not gl_ready:
        missing.append(GL_ACCOUNTS_NOT_SYNCED)
    if not txn_ready:
        missing.append(GL_ENTRIES_NOT_SYNCED)

    optional_warnings: List[str] = []
    if cust_unavail or vend_unavail:
        optional_warnings.append(_LEDGER_OPTIONAL_MESSAGE)
    elif not customers_ready and not vendors_ready:
        optional_warnings.append(
            "Customer/Vendor master lists are not synced yet. Sync Customers and "
            "Vendors for optional ontology enrichment.",
        )

    return {
        "entity_code":         entity_id,
        "erp_type":            "business_central",
        "credential_verified": cred_verified,
        "company_selected":    company_selected,
        "gl_ready":            gl_ready,
        "sub_gl_ready":        sgl_ready,
        "transaction_ready":   txn_ready,
        "api_complete":        api_complete,
        "ready_count":         sum(1 for x in required_layers if x),
        "required_ready_count": sum(1 for x in required_layers if x),
        "required_ready_total": len(required_layers),
        "customers_ready":       customers_ready,
        "vendors_ready":         vendors_ready,
        "customers_count":       customers_count,
        "vendors_count":         vendors_count,
        "customers_last_synced_at": customers_at,
        "vendors_last_synced_at":   vendors_at,
        "customer_ledger_ready": cust_ready,
        "vendor_ledger_ready":   vend_ready,
        "customer_ledger_count": cust_count,
        "vendor_ledger_count":   vend_count,
        "customer_ledger_last_synced_at": cust_at,
        "vendor_ledger_last_synced_at":   vend_at,
        "customer_ledger_unavailable": cust_unavail,
        "vendor_ledger_unavailable":   vend_unavail,
        "customer_ledger_optional_message": cust_unavail_msg,
        "vendor_ledger_optional_message":   vend_unavail_msg,
        "optional_warnings":   optional_warnings,
        "missing_steps":       missing,
        "gl_account_count":          gl_count,
        "dimension_value_count":     sgl_count,
        "general_ledger_entry_count": txn_count,
        "gl_last_synced_at":          gl_at,
        "dimensions_last_synced_at":  sgl_at,
        "entries_last_synced_at":     txn_at,
        "active_company_id":   cred.company_id   if cred else None,
        "active_company_name": cred.company_name if cred else None,
    }


def _is_optional_ledger_sync_error(error: Optional[SafeError]) -> bool:
    return bool(error and error.code in _OPTIONAL_LEDGER_ERRORS)


def _restore_credential_after_optional_sync(
    db: Session, rec: BusinessCentralCredential, entity_id: str,
) -> None:
    """Optional customer/vendor sync failed — do not poison credential status."""
    readiness = calculate_business_central_readiness(db, entity_id)
    if readiness.get("api_complete"):
        rec.status = "synced"
    elif rec.status == "syncing":
        rec.status = "verified" if readiness.get("credential_verified") else rec.status
    rec.last_safe_error_code = None
    rec.last_safe_error_message = None


def _heal_credential_status_if_required_ready(
    db: Session, entity_id: str,
) -> None:
    """Clear stale FAILED status left by optional ledger endpoint errors."""
    cred = _get_credential(db, entity_id)
    if not cred or cred.status != "failed":
        return
    code = cred.last_safe_error_code or ""
    if code not in _OPTIONAL_LEDGER_ERRORS and code != ENDPOINT_NOT_FOUND:
        return
    readiness = calculate_business_central_readiness(db, entity_id)
    if not readiness.get("api_complete"):
        return
    cred.status = "synced"
    cred.last_safe_error_code = None
    cred.last_safe_error_message = None
    db.commit()


# ──────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────
@router.get(
    "/credentials/{entity_code}",
    response_model=BcCredentialOut,
    summary="Get Business Central credential metadata (safe)",
    responses={404: {"description": "No credential for entity"}},
)
def get_credentials(entity_code: str, db: Session = Depends(get_db)):
    _require_bc_mapping(db, entity_code)
    rec = _require_credential(db, entity_code)
    return _to_safe_out(rec)


@router.delete(
    "/credentials/{entity_code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset / delete Business Central credentials and derived data",
    description=(
        "Deletes the stored credential row plus any companies / GL accounts / "
        "GL entries previously synced for that entity. Use this when the row "
        "is unreadable (INVALID_CONFIG) or you need a clean re-onboarding."
    ),
    responses={404: {"description": "No credential for entity"}},
)
def delete_credentials(
    entity_code: str, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, entity_code)
    rec = _require_credential(db, entity_code)
    cred_id = rec.id
    _reset_bc_data(db, entity_code)
    db.delete(rec)
    log_change(
        db, actor=actor, action="bc.credentials.delete",
        table_name="bc_credentials", row_pk=str(cred_id),
        before={"entity_id": entity_code},
        request=request,
    )
    db.commit()


@router.post(
    "/credentials",
    response_model=BcCredentialOut,
    status_code=status.HTTP_201_CREATED,
    summary="Save / update Business Central credentials",
    description=(
        "Encrypts tenant ID, client ID and client secret with the configured "
        "credential encryption key. Plaintext is never returned. On update, "
        "fields omitted from the body keep their stored values."
    ),
    responses={
        400: {"description": "Validation failed (e.g. missing required field on first save)"},
        404: {"description": "Entity not found"},
    },
)
def upsert_credentials(
    body: BcCredentialUpsert, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_entity(db, body.entity_id)
    _require_bc_mapping(db, body.entity_id)
    rec = _get_credential(db, body.entity_id)
    is_create = rec is None
    # When the existing row is unreadable (INVALID_CONFIG), require the admin
    # to re-enter every secret field — a silent partial update would leave
    # the row half-encrypted under two different keys.
    needs_full_resave = (not is_create) and _credential_is_invalid_config(rec)

    if is_create or needs_full_resave:
        missing = [f for f in (
            "tenant_id", "client_id", "client_secret",
            "environment_name", "scope", "base_url",
        ) if not getattr(body, f)]
        if missing:
            who = "first save" if is_create else "re-entry after INVALID_CONFIG"
            raise HTTPException(
                status_code=400,
                detail=f"Missing required field(s) on {who}: {', '.join(missing)}",
            )

    if is_create:
        rec = BusinessCentralCredential(
            entity_id=body.entity_id, erp_type=ERP_TYPE_BC,
            tenant_id_encrypted=encrypt_credential_field(body.tenant_id or ""),
            client_id_encrypted=encrypt_credential_field(body.client_id or ""),
            client_secret_encrypted=encrypt_credential_field(body.client_secret or ""),
            environment_name=body.environment_name or "",
            scope=body.scope or DEFAULT_BC_SCOPE,
            base_url=(body.base_url or DEFAULT_BC_BASE_URL),
            company_id=body.company_id, company_name=body.company_name,
            last4_client=mask_for_display(body.client_id or "")[-4:] if body.client_id else None,
            last4_secret=mask_for_display(body.client_secret or "")[-4:] if body.client_secret else None,
            status="saved", created_by=actor.id, updated_by=actor.id,
        )
        db.add(rec)
    else:
        if body.tenant_id:
            rec.tenant_id_encrypted = encrypt_credential_field(body.tenant_id)
        if body.client_id:
            rec.client_id_encrypted = encrypt_credential_field(body.client_id)
            rec.last4_client = mask_for_display(body.client_id)[-4:]
        if body.client_secret:
            rec.client_secret_encrypted = encrypt_credential_field(body.client_secret)
            rec.last4_secret = mask_for_display(body.client_secret)[-4:]
        if body.environment_name:
            rec.environment_name = body.environment_name
        if body.scope:
            rec.scope = body.scope
        if body.base_url:
            rec.base_url = body.base_url
        if body.company_id is not None:
            rec.company_id = body.company_id
        if body.company_name is not None:
            rec.company_name = body.company_name
        rec.updated_by = actor.id
        if needs_full_resave:
            # All secrets just rewritten under the current key. Mark row
            # usable again and drop derived data tied to the old config.
            rec.status = "saved"
            rec.last_safe_error_code = None
            rec.last_safe_error_message = None
            _reset_bc_data(db, body.entity_id)
        elif rec.status == "not_configured":
            rec.status = "saved"

    log_change(
        db, actor=actor, action="bc.credentials.upsert",
        table_name="bc_credentials",
        row_pk=str(rec.id) if rec.id else body.entity_id,
        after={
            "entity_id": body.entity_id,
            "environment_name": rec.environment_name,
            "base_url": rec.base_url,
            "company_id": rec.company_id,
            "is_create": is_create,
            "fields_set": [
                k for k in ("tenant_id", "client_id", "client_secret",
                            "environment_name", "scope", "base_url",
                            "company_id", "company_name")
                if getattr(body, k) is not None
            ],
        },
        request=request,
    )
    db.commit()
    db.refresh(rec)
    return _to_safe_out(rec)


@router.post(
    "/test",
    response_model=BcConnectorResponse,
    summary="Test Business Central connection (calls /companies)",
    responses={404: {"description": "Credentials not configured"}},
)
def test_connection(
    body: BcTestRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, body.entity_id)
    rec = _require_credential(db, body.entity_id)
    rec.status = "testing"
    db.flush()

    run = _start_sync_run(db, entity_id=body.entity_id,
                          sync_type=SYNC_TEST_CONNECTION, actor=actor)

    result = svc_test_connection(rec)
    rec.last_tested_at = datetime.utcnow()

    if result.ok:
        records = result.records
        rec.status = "verified"
        rec.last_verified_at = rec.last_tested_at
        rec.last_safe_error_code = None
        rec.last_safe_error_message = None
        _replace_companies(db, body.entity_id, records)
        _finish_sync_run(run, ok=True, records_processed=len(records))
    else:
        rec.status = "failed"
        rec.last_safe_error_code = result.error.code if result.error else "BC_SERVICE_ERROR"
        rec.last_safe_error_message = result.error.message if result.error else None
        _finish_sync_run(run, ok=False, error=result.error, records_processed=0)

    log_change(
        db, actor=actor, action="bc.test_connection",
        table_name="bc_credentials", row_pk=str(rec.id),
        after={"ok": result.ok,
               "safe_error_code": rec.last_safe_error_code},
        request=request,
    )
    db.commit()
    db.refresh(run)
    return BcConnectorResponse(
        ok=result.ok, status=rec.status,
        safe_error_code=rec.last_safe_error_code,
        safe_error_message=rec.last_safe_error_message,
        records_processed=run.records_processed,
        companies=[BcCompanyOut(**r) for r in result.records] if result.ok else [],
        sync_run=_to_run_out(run),
    )


@router.get(
    "/companies/{entity_code}",
    response_model=List[BcCompanyOut],
    summary="List Business Central companies stored from the last test/fetch",
)
def list_companies(entity_code: str, db: Session = Depends(get_db)):
    _require_bc_mapping(db, entity_code)
    rows = (db.query(ErpCompany)
              .filter(ErpCompany.entity_id == entity_code,
                      ErpCompany.erp_type == ERP_TYPE_BC)
              .order_by(ErpCompany.company_name).all())
    return [BcCompanyOut(company_id=r.company_id, company_name=r.company_name) for r in rows]


@router.post(
    "/sync/gl-accounts",
    response_model=BcConnectorResponse,
    summary="Sync GL accounts from Business Central",
    responses={400: {"description": "Validation error"},
               404: {"description": "Credentials not configured"}},
)
def sync_gl_accounts(
    body: BcSyncRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, body.entity_id)
    rec = _require_credential(db, body.entity_id)
    company_id = body.company_id or rec.company_id
    if not company_id:
        raise HTTPException(status_code=400, detail="company_id is required")

    try:
        rec.status = "syncing"
        db.flush()
        run = _start_sync_run(db, entity_id=body.entity_id,
                              sync_type=SYNC_GL_ACCOUNTS, actor=actor)
        result = svc_sync_gl_accounts(rec, company_id)
        if result.ok:
            count = _replace_accounts(db, body.entity_id, company_id, result.records)
            rec.status = "synced"
            rec.last_safe_error_code = None
            rec.last_safe_error_message = None
            _finish_sync_run(run, ok=True, records_processed=count)
        else:
            rec.status = "failed"
            rec.last_safe_error_code = result.error.code if result.error else "BC_SERVICE_ERROR"
            rec.last_safe_error_message = result.error.message if result.error else None
            _finish_sync_run(run, ok=False, error=result.error, records_processed=0)
            _notify_bc_failure_safe(db, body.entity_id, actor.id, rec.last_safe_error_message)

        log_change(
            db, actor=actor, action="bc.sync.gl_accounts",
            table_name="erp_gl_accounts", row_pk=f"{body.entity_id}/{company_id}",
            after={"ok": result.ok, "records": run.records_processed,
                   "safe_error_code": rec.last_safe_error_code},
            request=request,
        )
        db.commit()
        db.refresh(run)
    except SQLAlchemyError as exc:
        return _safe_db_failure_response(
            db, exc, entity_id=body.entity_id,
            sync_type=SYNC_GL_ACCOUNTS, actor=actor,
        )

    return BcConnectorResponse(
        ok=result.ok, status=rec.status,
        safe_error_code=rec.last_safe_error_code,
        safe_error_message=rec.last_safe_error_message,
        records_processed=run.records_processed,
        sync_run=_to_run_out(run),
    )


@router.post(
    "/sync/gl-entries",
    response_model=BcConnectorResponse,
    summary="Sync General Ledger entries from Business Central",
    responses={400: {"description": "Validation error"},
               404: {"description": "Credentials not configured"}},
)
def sync_gl_entries(
    body: BcGlEntrySyncRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, body.entity_id)
    rec = _require_credential(db, body.entity_id)
    company_id = body.company_id or rec.company_id
    if not company_id:
        raise HTTPException(status_code=400, detail="company_id is required")

    try:
        rec.status = "syncing"
        db.flush()
        run = _start_sync_run(db, entity_id=body.entity_id,
                              sync_type=SYNC_GL_ENTRIES, actor=actor)
        result = svc_sync_gl_entries(
            rec, company_id, from_date=body.from_date, to_date=body.to_date,
        )
        if result.ok:
            count = _replace_gl_entries(db, body.entity_id, company_id, result.records)
            rec.status = "synced"
            rec.last_safe_error_code = None
            rec.last_safe_error_message = None
            _finish_sync_run(run, ok=True, records_processed=count)
        else:
            rec.status = "failed"
            rec.last_safe_error_code = result.error.code if result.error else "BC_SERVICE_ERROR"
            rec.last_safe_error_message = result.error.message if result.error else None
            _finish_sync_run(run, ok=False, error=result.error, records_processed=0)
            _notify_bc_failure_safe(db, body.entity_id, actor.id, rec.last_safe_error_message)

        log_change(
            db, actor=actor, action="bc.sync.gl_entries",
            table_name="erp_general_ledger_entries", row_pk=f"{body.entity_id}/{company_id}",
            after={"ok": result.ok, "records": run.records_processed,
                   "safe_error_code": rec.last_safe_error_code},
            request=request,
        )
        db.commit()
        db.refresh(run)
    except SQLAlchemyError as exc:
        return _safe_db_failure_response(
            db, exc, entity_id=body.entity_id,
            sync_type=SYNC_GL_ENTRIES, actor=actor,
        )

    return BcConnectorResponse(
        ok=result.ok, status=rec.status,
        safe_error_code=rec.last_safe_error_code,
        safe_error_message=rec.last_safe_error_message,
        records_processed=run.records_processed,
        sync_run=_to_run_out(run),
    )


@router.post(
    "/sync/customers",
    response_model=BcConnectorResponse,
    summary="Sync Customer master from Business Central",
    responses={400: {"description": "Validation error"},
               404: {"description": "Credentials not configured"}},
)
def sync_customers(
    body: BcSyncRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, body.entity_id)
    rec = _require_credential(db, body.entity_id)
    company_id = body.company_id or rec.company_id
    if not company_id:
        raise HTTPException(status_code=400, detail="company_id is required")

    try:
        rec.status = "syncing"
        db.flush()
        run = _start_sync_run(
            db, entity_id=body.entity_id, sync_type=SYNC_CUSTOMERS, actor=actor,
        )
        result = svc_sync_customers(rec, company_id)
        if result.ok:
            count = _replace_customers(db, body.entity_id, company_id, result.records)
            rec.status = "synced"
            rec.last_safe_error_code = None
            rec.last_safe_error_message = None
            _finish_sync_run(run, ok=True, records_processed=count)
        else:
            rec.status = "failed"
            rec.last_safe_error_code = (
                result.error.code if result.error else "BC_SERVICE_ERROR"
            )
            rec.last_safe_error_message = (
                result.error.message if result.error else None
            )
            _finish_sync_run(run, ok=False, error=result.error, records_processed=0)
        log_change(
            db, actor=actor, action="bc.sync.customers",
            table_name="erp_customers", row_pk=f"{body.entity_id}/{company_id}",
            after={"ok": result.ok, "records": run.records_processed},
            request=request,
        )
        db.commit()
        db.refresh(run)
    except SQLAlchemyError as exc:
        return _safe_db_failure_response(
            db, exc, entity_id=body.entity_id, sync_type=SYNC_CUSTOMERS, actor=actor,
        )

    return BcConnectorResponse(
        ok=result.ok, status=rec.status,
        safe_error_code=rec.last_safe_error_code,
        safe_error_message=rec.last_safe_error_message,
        records_processed=run.records_processed,
        sync_run=_to_run_out(run),
    )


@router.post(
    "/sync/vendors",
    response_model=BcConnectorResponse,
    summary="Sync Vendor master from Business Central",
    responses={400: {"description": "Validation error"},
               404: {"description": "Credentials not configured"}},
)
def sync_vendors(
    body: BcSyncRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, body.entity_id)
    rec = _require_credential(db, body.entity_id)
    company_id = body.company_id or rec.company_id
    if not company_id:
        raise HTTPException(status_code=400, detail="company_id is required")

    try:
        rec.status = "syncing"
        db.flush()
        run = _start_sync_run(
            db, entity_id=body.entity_id, sync_type=SYNC_VENDORS, actor=actor,
        )
        result = svc_sync_vendors(rec, company_id)
        if result.ok:
            count = _replace_vendors(db, body.entity_id, company_id, result.records)
            rec.status = "synced"
            rec.last_safe_error_code = None
            rec.last_safe_error_message = None
            _finish_sync_run(run, ok=True, records_processed=count)
        else:
            rec.status = "failed"
            rec.last_safe_error_code = (
                result.error.code if result.error else "BC_SERVICE_ERROR"
            )
            rec.last_safe_error_message = (
                result.error.message if result.error else None
            )
            _finish_sync_run(run, ok=False, error=result.error, records_processed=0)
        log_change(
            db, actor=actor, action="bc.sync.vendors",
            table_name="erp_vendors", row_pk=f"{body.entity_id}/{company_id}",
            after={"ok": result.ok, "records": run.records_processed},
            request=request,
        )
        db.commit()
        db.refresh(run)
    except SQLAlchemyError as exc:
        return _safe_db_failure_response(
            db, exc, entity_id=body.entity_id, sync_type=SYNC_VENDORS, actor=actor,
        )

    return BcConnectorResponse(
        ok=result.ok, status=rec.status,
        safe_error_code=rec.last_safe_error_code,
        safe_error_message=rec.last_safe_error_message,
        records_processed=run.records_processed,
        sync_run=_to_run_out(run),
    )


@router.post(
    "/sync/customer-ledger-entries",
    response_model=BcConnectorResponse,
    summary="Sync Customer Ledger entries from Business Central (optional custom API)",
    responses={400: {"description": "Validation error"},
               404: {"description": "Credentials not configured"}},
)
def sync_customer_ledger_entries(
    body: BcSyncRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, body.entity_id)
    rec = _require_credential(db, body.entity_id)
    company_id = body.company_id or rec.company_id
    if not company_id:
        raise HTTPException(status_code=400, detail="company_id is required")

    try:
        rec.status = "syncing"
        db.flush()
        run = _start_sync_run(
            db, entity_id=body.entity_id,
            sync_type=SYNC_CUSTOMER_LEDGER_ENTRIES, actor=actor,
        )
        result = svc_sync_customer_ledger(rec, company_id)
        connector_ok = result.ok
        layer_status: Optional[str] = None
        if result.ok:
            count = _replace_customer_ledger_entries(
                db, body.entity_id, company_id, result.records,
            )
            rec.status = "synced"
            rec.last_safe_error_code = None
            rec.last_safe_error_message = None
            _finish_sync_run(run, ok=True, records_processed=count)
        elif _is_optional_ledger_sync_error(result.error):
            _finish_sync_run_optional_unavailable(run, result.error)
            _restore_credential_after_optional_sync(db, rec, body.entity_id)
            connector_ok = True
            layer_status = "optional_unavailable"
        else:
            rec.status = "failed"
            rec.last_safe_error_code = (
                result.error.code if result.error else "BC_SERVICE_ERROR"
            )
            rec.last_safe_error_message = (
                result.error.message if result.error else None
            )
            _finish_sync_run(run, ok=False, error=result.error, records_processed=0)
        log_change(
            db, actor=actor, action="bc.sync.customer_ledger_entries",
            table_name="erp_customer_ledger_entries",
            row_pk=f"{body.entity_id}/{company_id}",
            after={"ok": connector_ok, "records": run.records_processed,
                   "layer_status": layer_status},
            request=request,
        )
        db.commit()
        db.refresh(run)
    except SQLAlchemyError as exc:
        return _safe_db_failure_response(
            db, exc, entity_id=body.entity_id,
            sync_type=SYNC_CUSTOMER_LEDGER_ENTRIES, actor=actor,
        )

    if layer_status == "optional_unavailable" and result.error:
        return _connector_response_optional_unavailable(rec, run, result.error)

    return BcConnectorResponse(
        ok=connector_ok, status=rec.status,
        layer_status=layer_status,
        safe_error_code=rec.last_safe_error_code,
        safe_error_message=rec.last_safe_error_message,
        records_processed=run.records_processed,
        sync_run=_to_run_out(run),
    )


@router.post(
    "/sync/vendor-ledger-entries",
    response_model=BcConnectorResponse,
    summary="Sync Vendor Ledger entries from Business Central",
    responses={400: {"description": "Validation error"},
               404: {"description": "Credentials not configured"}},
)
def sync_vendor_ledger_entries(
    body: BcSyncRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, body.entity_id)
    rec = _require_credential(db, body.entity_id)
    company_id = body.company_id or rec.company_id
    if not company_id:
        raise HTTPException(status_code=400, detail="company_id is required")

    try:
        rec.status = "syncing"
        db.flush()
        run = _start_sync_run(
            db, entity_id=body.entity_id,
            sync_type=SYNC_VENDOR_LEDGER_ENTRIES, actor=actor,
        )
        result = svc_sync_vendor_ledger(rec, company_id)
        connector_ok = result.ok
        layer_status: Optional[str] = None
        if result.ok:
            count = _replace_vendor_ledger_entries(
                db, body.entity_id, company_id, result.records,
            )
            rec.status = "synced"
            rec.last_safe_error_code = None
            rec.last_safe_error_message = None
            _finish_sync_run(run, ok=True, records_processed=count)
        elif _is_optional_ledger_sync_error(result.error):
            _finish_sync_run_optional_unavailable(run, result.error)
            _restore_credential_after_optional_sync(db, rec, body.entity_id)
            connector_ok = True
            layer_status = "optional_unavailable"
        else:
            rec.status = "failed"
            rec.last_safe_error_code = (
                result.error.code if result.error else "BC_SERVICE_ERROR"
            )
            rec.last_safe_error_message = (
                result.error.message if result.error else None
            )
            _finish_sync_run(run, ok=False, error=result.error, records_processed=0)
        log_change(
            db, actor=actor, action="bc.sync.vendor_ledger_entries",
            table_name="erp_vendor_ledger_entries",
            row_pk=f"{body.entity_id}/{company_id}",
            after={"ok": connector_ok, "records": run.records_processed,
                   "layer_status": layer_status},
            request=request,
        )
        db.commit()
        db.refresh(run)
    except SQLAlchemyError as exc:
        return _safe_db_failure_response(
            db, exc, entity_id=body.entity_id,
            sync_type=SYNC_VENDOR_LEDGER_ENTRIES, actor=actor,
        )

    if layer_status == "optional_unavailable" and result.error:
        return _connector_response_optional_unavailable(rec, run, result.error)

    return BcConnectorResponse(
        ok=connector_ok, status=rec.status,
        layer_status=layer_status,
        safe_error_code=rec.last_safe_error_code,
        safe_error_message=rec.last_safe_error_message,
        records_processed=run.records_processed,
        sync_run=_to_run_out(run),
    )


@router.patch(
    "/company",
    response_model=BcCredentialOut,
    summary="Set the active Business Central company for an entity",
    responses={404: {"description": "Credentials not configured"}},
)
def set_company(
    body: BcCompanyPatch, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, body.entity_id)
    rec = _require_credential(db, body.entity_id)
    rec.company_id = body.company_id
    rec.company_name = body.company_name
    rec.updated_by = actor.id
    log_change(
        db, actor=actor, action="bc.company.set",
        table_name="bc_credentials", row_pk=str(rec.id),
        after={"company_id": body.company_id, "company_name": body.company_name},
        request=request,
    )
    db.commit()
    db.refresh(rec)
    return _to_safe_out(rec)


@router.post(
    "/fetch-companies",
    response_model=BcConnectorResponse,
    summary="Re-fetch companies from Business Central without resetting credential status",
    responses={404: {"description": "Credentials not configured"}},
)
def fetch_companies(
    body: BcTestRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, body.entity_id)
    rec = _require_credential(db, body.entity_id)
    run = _start_sync_run(db, entity_id=body.entity_id,
                          sync_type=SYNC_FETCH_COMPANIES, actor=actor)
    result = svc_fetch_companies(rec)
    if result.ok:
        _replace_companies(db, body.entity_id, result.records)
        _finish_sync_run(run, ok=True, records_processed=len(result.records))
    else:
        rec.last_safe_error_code = result.error.code if result.error else "BC_SERVICE_ERROR"
        rec.last_safe_error_message = result.error.message if result.error else None
        _finish_sync_run(run, ok=False, error=result.error, records_processed=0)

    log_change(
        db, actor=actor, action="bc.fetch_companies",
        table_name="erp_companies", row_pk=body.entity_id,
        after={"ok": result.ok, "records": run.records_processed,
               "safe_error_code": rec.last_safe_error_code},
        request=request,
    )
    db.commit()
    db.refresh(run)
    return BcConnectorResponse(
        ok=result.ok, status=rec.status,
        safe_error_code=rec.last_safe_error_code,
        safe_error_message=rec.last_safe_error_message,
        records_processed=run.records_processed,
        companies=[BcCompanyOut(**r) for r in result.records] if result.ok else [],
        sync_run=_to_run_out(run),
    )


# ──────────────────────────────────────────────────────────────────────
# Sub-GL / Dimensions sync
# ──────────────────────────────────────────────────────────────────────
@router.post(
    "/sync/dimensions",
    response_model=BcConnectorResponse,
    summary="Sync Business Central dimension values (Sub-GL layer)",
    responses={400: {"description": "Validation error"},
               404: {"description": "Credentials not configured"}},
)
def sync_dimensions(
    body: BcSyncRequest, request: Request,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    _require_bc_mapping(db, body.entity_id)
    rec = _require_credential(db, body.entity_id)
    company_id = body.company_id or rec.company_id
    if not company_id:
        raise HTTPException(status_code=400, detail="company_id is required")

    try:
        rec.status = "syncing"
        db.flush()
        run = _start_sync_run(db, entity_id=body.entity_id,
                              sync_type=SYNC_DIMENSIONS, actor=actor)
        result = svc_sync_dimensions(rec, company_id)
        if result.ok:
            count = _replace_dimensions(db, body.entity_id, company_id, result.records)
            rec.status = "synced"
            rec.last_safe_error_code = None
            rec.last_safe_error_message = None
            _finish_sync_run(run, ok=True, records_processed=count)
        else:
            rec.status = "failed"
            rec.last_safe_error_code = result.error.code if result.error else "BC_SERVICE_ERROR"
            rec.last_safe_error_message = result.error.message if result.error else None
            _finish_sync_run(run, ok=False, error=result.error, records_processed=0)
            _notify_bc_failure_safe(db, body.entity_id, actor.id, rec.last_safe_error_message)

        log_change(
            db, actor=actor, action="bc.sync.dimensions",
            table_name="erp_dimension_values", row_pk=f"{body.entity_id}/{company_id}",
            after={"ok": result.ok, "records": run.records_processed,
                   "safe_error_code": rec.last_safe_error_code},
            request=request,
        )
        db.commit()
        db.refresh(run)
    except SQLAlchemyError as exc:
        return _safe_db_failure_response(
            db, exc, entity_id=body.entity_id,
            sync_type=SYNC_DIMENSIONS, actor=actor,
        )
    return BcConnectorResponse(
        ok=result.ok, status=rec.status,
        safe_error_code=rec.last_safe_error_code,
        safe_error_message=rec.last_safe_error_message,
        records_processed=run.records_processed,
        sync_run=_to_run_out(run),
    )


# ──────────────────────────────────────────────────────────────────────
# Readiness — what GL/Sub-GL/Transaction layers have been synced for entity
# ──────────────────────────────────────────────────────────────────────
@router.get(
    "/readiness/{entity_code}",
    response_model=BcReadinessOut,
    summary="Business Central API readiness for an entity (used by ontology gate)",
)
def get_readiness(entity_code: str, db: Session = Depends(get_db)):
    _require_bc_mapping(db, entity_code)
    _heal_credential_status_if_required_ready(db, entity_code)
    return BcReadinessOut(**calculate_business_central_readiness(db, entity_code))


# ──────────────────────────────────────────────────────────────────────
# Synced datasets — read-only views the ontology UI consumes.
# ──────────────────────────────────────────────────────────────────────
@router.get(
    "/erp-gl-accounts/{entity_code}",
    response_model=List[ErpAccountOut],
    summary="List synced GL accounts for an entity (Business Central)",
)
def list_erp_gl_accounts(entity_code: str, db: Session = Depends(get_db)):
    _require_bc_mapping(db, entity_code)
    rows = (db.query(ErpGlAccount)
              .filter(ErpGlAccount.entity_id == entity_code,
                      ErpGlAccount.erp_type == ERP_TYPE_BC)
              .order_by(ErpGlAccount.account_no)
              .all())
    return [ErpAccountOut(
        company_id=r.company_id,
        account_no=r.account_no,
        display_name=r.display_name,
        account_name=r.account_name,
        account_category=r.account_category,
        sub_category=r.sub_category,
        account_type=r.account_type,
        blocked=r.blocked,
        external_id=r.external_id,
        last_synced_at=r.last_synced_at,
    ) for r in rows]


@router.get(
    "/erp-subgl-dimensions/{entity_code}",
    response_model=List[ErpDimensionValueOut],
    summary="List synced dimension values for an entity (Business Central)",
)
def list_erp_dimension_values(entity_code: str, db: Session = Depends(get_db)):
    _require_bc_mapping(db, entity_code)
    rows = (db.query(ErpDimensionValue)
              .filter(ErpDimensionValue.entity_id == entity_code,
                      ErpDimensionValue.erp_type == ERP_TYPE_BC)
              .order_by(ErpDimensionValue.dimension_code,
                        ErpDimensionValue.value_code)
              .all())
    return [ErpDimensionValueOut(
        company_id=r.company_id,
        dimension_code=r.dimension_code,
        dimension_name=r.dimension_name,
        value_code=r.value_code,
        value_name=r.value_name,
        display_name=r.display_name or r.value_name or r.value_code,
        last_synced_at=r.last_synced_at,
    ) for r in rows]


@router.get(
    "/erp-subledger-details/{entity_code}",
    response_model=List[ErpSubledgerOptionOut],
    summary="List customer/vendor sub-ledger options (legacy flat list)",
)
def list_erp_subledger_details(entity_code: str, db: Session = Depends(get_db)):
    _require_bc_mapping(db, entity_code)
    from app.services.subledger_catalog import build_subledger_catalog
    return [
        ErpSubledgerOptionOut(value=o.value, label=o.label, kind=o.kind)
        for o in build_subledger_catalog(db, entity_code)
    ]


@router.get(
    "/erp-transactions/{entity_code}",
    response_model=List[ErpGlEntryOut],
    summary="List synced general ledger entries for an entity (Business Central)",
)
def list_erp_transactions(
    entity_code: str,
    limit: int = 500,
    db: Session = Depends(get_db),
):
    """Capped at 500 by default — analyst drill-down uses paged Flux Review,
    not this admin convenience endpoint."""
    _require_bc_mapping(db, entity_code)
    safe_limit = max(1, min(int(limit), 5000))
    rows = (db.query(ErpGeneralLedgerEntry)
              .filter(ErpGeneralLedgerEntry.entity_id == entity_code,
                      ErpGeneralLedgerEntry.erp_type == ERP_TYPE_BC)
              .order_by(ErpGeneralLedgerEntry.posting_date.desc(),
                        ErpGeneralLedgerEntry.id.desc())
              .limit(safe_limit).all())
    return [ErpGlEntryOut(
        company_id=r.company_id,
        external_entry_no=r.external_entry_no,
        entry_number=r.entry_number,
        posting_date=r.posting_date,  # type: ignore[arg-type]
        document_no=r.document_no,
        account_no=r.account_no,
        description=r.description,
        debit_amount=float(r.debit_amount) if r.debit_amount is not None else None,
        credit_amount=float(r.credit_amount) if r.credit_amount is not None else None,
        amount=float(r.amount) if r.amount is not None else None,
        last_synced_at=r.last_synced_at,
    ) for r in rows]
