# @file    routers/admin/quickbooks.py
#
# Admin Console — QuickBooks Online integration (OAuth 2.0 only).
# Mounted under /api/admin/integrations/quickbooks. Does not touch BC routes.

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.config import settings
from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.models import (
    ERP_TYPE_QUICKBOOKS,
    Entity,
    EntityErpMapping,
    ErpCustomer,
    ErpGeneralLedgerEntry,
    ErpGlAccount,
    ErpSyncRun,
    ErpVendor,
    QuickBooksCredential,
    SYNC_CUSTOMERS,
    SYNC_GL_ACCOUNTS,
    SYNC_GL_ENTRIES,
    SYNC_TEST_CONNECTION,
    SYNC_VENDORS,
    User,
)
from app.services.erp.quickbooks_oauth import (
    DEFAULT_QB_API_BASE_PRODUCTION,
    DEFAULT_QB_API_BASE_SANDBOX,
    DEFAULT_QB_AUTH_URL,
    DEFAULT_QB_REDIRECT_URI,
    DEFAULT_QB_SCOPE,
    DEFAULT_QB_TOKEN_URL,
    app_credentials_saved,
    build_authorization_url,
    clear_tokens,
    encrypt_credential_field,
    exchange_code_for_tokens,
    generate_oauth_state,
    mask_for_display,
    oauth_error_from_exception,
    oauth_ready,
    store_tokens,
)
from app.services.erp.quickbooks_service import (
    ConnectorResult,
    fetch_company_info,
    fetch_report,
    sync_accounts as svc_sync_accounts,
    sync_customers as svc_sync_customers,
    sync_general_ledger as svc_sync_gl,
    sync_vendors as svc_sync_vendors,
    test_connection as svc_test_connection,
)
from app.services.flux_generation import parse_entity_period
from app.services.erp.safe_errors import (
    DATABASE_ERROR,
    ENTITY_ERP_MAPPING_REQUIRED,
    QUICKBOOKS_GL_NO_TRANSACTIONS,
    QUICKBOOKS_GL_PARSE_EMPTY,
    SafeError,
    safe_error,
)

_log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_role("admin"))])
callback_router = APIRouter()

ERP_KEY_QB = ERP_TYPE_QUICKBOOKS


class QbStatusOut(BaseModel):
    entity_id: str
    erp_type: str = "quickbooks"
    status: str
    connection_status: str = "not_configured"
    token_status: str = "missing"
    realm_id: Optional[str] = None
    company_name: Optional[str] = None
    environment: str
    token_expiry: Optional[datetime] = None
    last_safe_error_code: Optional[str] = None
    last_safe_error_message: Optional[str] = None
    last_tested_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    connected: bool = False
    credentials_saved: bool = False


class QbCredentialUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = Field(default=None, max_length=512)
    environment: Optional[Literal["sandbox", "production"]] = None
    scopes: Optional[str] = Field(default=None, max_length=512)
    auth_url: Optional[str] = Field(default=None, max_length=512)
    token_url: Optional[str] = Field(default=None, max_length=512)
    api_base_sandbox: Optional[str] = Field(default=None, max_length=512)
    api_base_production: Optional[str] = Field(default=None, max_length=512)

    @field_validator(
        "client_id", "client_secret", "redirect_uri", "scopes",
        "auth_url", "token_url", "api_base_sandbox", "api_base_production",
    )
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        return v or None


class QbCredentialOut(BaseModel):
    credential_id: Optional[int] = None
    entity_id: str
    erp_type: str = "quickbooks"
    redirect_uri: Optional[str] = None
    environment: str = "sandbox"
    scopes: Optional[str] = None
    auth_url: Optional[str] = None
    token_url: Optional[str] = None
    api_base_sandbox: Optional[str] = None
    api_base_production: Optional[str] = None
    masked_client_id: Optional[str] = None
    client_secret_last4: Optional[str] = None
    status: str = "not_configured"
    connection_status: str = "not_configured"
    token_status: str = "missing"
    realm_id: Optional[str] = None
    company_name: Optional[str] = None
    token_expiry: Optional[datetime] = None
    last_safe_error_code: Optional[str] = None
    last_safe_error_message: Optional[str] = None
    last_tested_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class QbConnectOut(BaseModel):
    authorization_url: str


class QbSyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str
    start_date: Optional[str] = Field(default=None, description="YYYY-MM-DD for GL sync")
    end_date: Optional[str] = Field(default=None, description="YYYY-MM-DD for GL sync")


class QbConnectorOut(BaseModel):
    ok: bool
    records_processed: int = 0
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None
    company_name: Optional[str] = None
    realm_id: Optional[str] = None
    gl_sync_diagnostics: Optional[dict] = None


class QbReadinessOut(BaseModel):
    entity_code: str
    erp_type: str = "quickbooks"
    credential_verified: bool
    company_selected: bool
    gl_ready: bool
    transaction_ready: bool
    api_complete: bool
    customers_ready: bool = False
    vendors_ready: bool = False
    missing: List[str] = []
    gl_account_count: Optional[int] = None
    general_ledger_entry_count: Optional[int] = None
    customers_count: Optional[int] = None
    vendors_count: Optional[int] = None
    gl_last_synced_at: Optional[datetime] = None
    entries_last_synced_at: Optional[datetime] = None
    customers_last_synced_at: Optional[datetime] = None
    vendors_last_synced_at: Optional[datetime] = None


class ErpAccountOut(BaseModel):
    account_no: str
    account_name: Optional[str] = None
    display_name: Optional[str] = None
    account_type: Optional[str] = None
    account_category: Optional[str] = None


class ErpPartyOut(BaseModel):
    code: str
    name: Optional[str] = None
    display_name: Optional[str] = None


def _require_qb_mapping(db: Session, entity_id: str) -> None:
    row = (
        db.query(EntityErpMapping)
        .filter(
            EntityErpMapping.entity_id == entity_id,
            EntityErpMapping.erp_key == ERP_KEY_QB,
        )
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=safe_error(ENTITY_ERP_MAPPING_REQUIRED).message,
        )


def _require_entity(db: Session, entity_id: str) -> Entity:
    ent = db.query(Entity).filter(Entity.id == entity_id).first()
    if not ent:
        raise HTTPException(status_code=404, detail="Entity not found")
    return ent


def _token_status(rec: QuickBooksCredential | None) -> str:
    if not rec or not rec.access_token_encrypted:
        return "missing"
    if rec.token_expiry and rec.token_expiry < datetime.utcnow():
        return "expired"
    return "valid"


def _connection_status(rec: QuickBooksCredential | None) -> str:
    if not rec or not app_credentials_saved(rec):
        return "not_configured"
    if rec.status == "failed":
        return "error"
    if rec.realm_id and rec.access_token_encrypted:
        if rec.token_expiry and rec.token_expiry < datetime.utcnow():
            return "token_expired"
        return "connected"
    if app_credentials_saved(rec):
        return "saved"
    return "not_configured"


def _to_credential_out(rec: QuickBooksCredential | None, entity_id: str) -> QbCredentialOut:
    if not rec:
        return QbCredentialOut(
            entity_id=entity_id,
            status="not_configured",
            connection_status="not_configured",
            token_status="missing",
        )
    return QbCredentialOut(
        credential_id=rec.id,
        entity_id=entity_id,
        redirect_uri=rec.redirect_uri,
        environment=rec.environment or "sandbox",
        scopes=rec.scopes,
        auth_url=rec.auth_url,
        token_url=rec.token_url,
        api_base_sandbox=rec.api_base_sandbox,
        api_base_production=rec.api_base_production,
        masked_client_id=(
            f"********{rec.last4_client}" if rec.last4_client else None
        ),
        client_secret_last4=rec.last4_secret,
        status=rec.status,
        connection_status=_connection_status(rec),
        token_status=_token_status(rec),
        realm_id=rec.realm_id,
        company_name=rec.company_name,
        token_expiry=rec.token_expiry,
        last_safe_error_code=rec.last_safe_error_code,
        last_safe_error_message=rec.last_safe_error_message,
        last_tested_at=rec.last_tested_at,
        last_verified_at=rec.last_verified_at,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


def _get_or_create_cred(db: Session, entity_id: str, actor_id: int) -> QuickBooksCredential:
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == entity_id).first()
    if rec:
        return rec
    rec = QuickBooksCredential(
        entity_id=entity_id,
        environment=settings.QUICKBOOKS_ENVIRONMENT or "sandbox",
        redirect_uri=DEFAULT_QB_REDIRECT_URI,
        scopes=DEFAULT_QB_SCOPE,
        auth_url=DEFAULT_QB_AUTH_URL,
        token_url=DEFAULT_QB_TOKEN_URL,
        api_base_sandbox=DEFAULT_QB_API_BASE_SANDBOX,
        api_base_production=DEFAULT_QB_API_BASE_PRODUCTION,
        status="not_configured",
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.add(rec)
    db.flush()
    return rec


def _to_status(rec: QuickBooksCredential | None, entity_id: str) -> QbStatusOut:
    if not rec:
        return QbStatusOut(
            entity_id=entity_id,
            status="not_configured",
            connection_status="not_configured",
            token_status="missing",
            environment=settings.QUICKBOOKS_ENVIRONMENT or "sandbox",
            connected=False,
            credentials_saved=False,
        )
    connected = bool(rec.realm_id and rec.access_token_encrypted)
    return QbStatusOut(
        entity_id=entity_id,
        status=rec.status,
        connection_status=_connection_status(rec),
        token_status=_token_status(rec),
        realm_id=rec.realm_id,
        company_name=rec.company_name,
        environment=rec.environment,
        token_expiry=rec.token_expiry,
        last_safe_error_code=rec.last_safe_error_code,
        last_safe_error_message=rec.last_safe_error_message,
        last_tested_at=rec.last_tested_at,
        last_verified_at=rec.last_verified_at,
        connected=connected,
        credentials_saved=app_credentials_saved(rec),
    )


def _last_successful_run(db: Session, entity_id: str, sync_type: str) -> ErpSyncRun | None:
    return (
        db.query(ErpSyncRun)
        .filter(
            ErpSyncRun.entity_id == entity_id,
            ErpSyncRun.erp_type == ERP_TYPE_QUICKBOOKS,
            ErpSyncRun.sync_type == sync_type,
            ErpSyncRun.status == "success",
        )
        .order_by(ErpSyncRun.started_at.desc())
        .first()
    )


def calculate_quickbooks_readiness(db: Session, entity_id: str) -> dict[str, object]:
    cred = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == entity_id).first()
    cred_verified = bool(cred and cred.status in ("verified", "syncing", "synced"))
    company_selected = bool(cred and cred.realm_id)

    def _sync_ready(stype: str) -> tuple[bool, Optional[int], Optional[datetime]]:
        run = _last_successful_run(db, entity_id, stype)
        if not run:
            return False, None, None
        count = run.records_processed or 0
        synced_at = run.finished_at or run.started_at
        return count > 0, count, synced_at

    gl_ready, gl_count, gl_at = _sync_ready(SYNC_GL_ACCOUNTS)
    txn_ready, txn_count, txn_at = _sync_ready(SYNC_GL_ENTRIES)
    cust_ready, cust_count, cust_at = _sync_ready(SYNC_CUSTOMERS)
    vend_ready, vend_count, vend_at = _sync_ready(SYNC_VENDORS)
    api_complete = bool(cred_verified and company_selected and gl_ready and txn_ready)

    missing: List[str] = []
    if not app_credentials_saved(cred):
        missing.append("save_quickbooks_credentials")
    if not cred_verified:
        missing.append("connect_quickbooks")
    if not company_selected:
        missing.append("realm_id_missing")
    if not gl_ready:
        missing.append("sync_accounts")
    if not txn_ready:
        missing.append("sync_general_ledger")

    return {
        "entity_code": entity_id,
        "erp_type": "quickbooks",
        "credential_verified": cred_verified,
        "company_selected": company_selected,
        "gl_ready": gl_ready,
        "sub_gl_ready": False,
        "transaction_ready": txn_ready,
        "api_complete": api_complete,
        "customers_ready": cust_ready,
        "vendors_ready": vend_ready,
        "missing": missing,
        "gl_account_count": gl_count,
        "general_ledger_entry_count": txn_count,
        "customers_count": cust_count,
        "vendors_count": vend_count,
        "gl_last_synced_at": gl_at,
        "entries_last_synced_at": txn_at,
        "customers_last_synced_at": cust_at,
        "vendors_last_synced_at": vend_at,
    }


def _replace_accounts(db: Session, entity_id: str, company_id: str, rows: List[dict]) -> int:
    now = datetime.utcnow()
    db.query(ErpGlAccount).filter(
        ErpGlAccount.entity_id == entity_id,
        ErpGlAccount.erp_type == ERP_TYPE_QUICKBOOKS,
        ErpGlAccount.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpGlAccount(
            entity_id=entity_id,
            erp_type=ERP_TYPE_QUICKBOOKS,
            company_id=company_id,
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


def _replace_gl_entries(db: Session, entity_id: str, company_id: str, rows: List[dict]) -> int:
    now = datetime.utcnow()
    db.query(ErpGeneralLedgerEntry).filter(
        ErpGeneralLedgerEntry.entity_id == entity_id,
        ErpGeneralLedgerEntry.erp_type == ERP_TYPE_QUICKBOOKS,
        ErpGeneralLedgerEntry.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpGeneralLedgerEntry(
            entity_id=entity_id,
            erp_type=ERP_TYPE_QUICKBOOKS,
            company_id=company_id,
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
            description=r.get("description"),
            raw_source_json=r.get("raw_source_json"),
            last_synced_at=now,
        ))
    return len(rows)


def _replace_customers(db: Session, entity_id: str, company_id: str, rows: List[dict]) -> int:
    now = datetime.utcnow()
    db.query(ErpCustomer).filter(
        ErpCustomer.entity_id == entity_id,
        ErpCustomer.erp_type == ERP_TYPE_QUICKBOOKS,
        ErpCustomer.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpCustomer(
            entity_id=entity_id,
            erp_type=ERP_TYPE_QUICKBOOKS,
            company_id=company_id,
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
        ErpVendor.erp_type == ERP_TYPE_QUICKBOOKS,
        ErpVendor.company_id == company_id,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(ErpVendor(
            entity_id=entity_id,
            erp_type=ERP_TYPE_QUICKBOOKS,
            company_id=company_id,
            vendor_no=r["vendor_no"],
            display_name=r.get("display_name"),
            external_id=r.get("external_id"),
            raw_source_json=r.get("raw_source_json"),
            last_synced_at=now,
        ))
    return len(rows)


def _start_sync_run(db: Session, entity_id: str, sync_type: str) -> ErpSyncRun:
    run = ErpSyncRun(
        entity_id=entity_id,
        erp_type=ERP_TYPE_QUICKBOOKS,
        sync_type=sync_type,
        status="running",
    )
    db.add(run)
    db.flush()
    return run


def _finish_sync_run(
    db: Session,
    run: ErpSyncRun,
    *,
    ok: bool,
    count: int,
    err: SafeError | None = None,
) -> None:
    run.status = "success" if ok else "failed"
    run.finished_at = datetime.utcnow()
    run.records_processed = count
    run.safe_error_code = err.code if err else None
    run.safe_error_message = err.message if err else None


def _connector_out(
    result: ConnectorResult,
    count: int = 0,
    rec: QuickBooksCredential | None = None,
    *,
    gl_sync_diagnostics: Optional[dict] = None,
    notice: SafeError | None = None,
) -> QbConnectorOut:
    err = notice or result.error
    return QbConnectorOut(
        ok=result.ok,
        records_processed=count,
        safe_error_code=err.code if err else None,
        safe_error_message=err.message if err else None,
        company_name=rec.company_name if rec else None,
        realm_id=rec.realm_id if rec else None,
        gl_sync_diagnostics=gl_sync_diagnostics,
    )


def _parse_entity_period_date(raw: str, *, end_of_month: bool = False) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        start_d, end_d = parse_entity_period(s)
        d = end_d if end_of_month else start_d
        return datetime.combine(d, datetime.min.time())
    except ValueError:
        pass
    for fmt, use_eom in (("%Y-%m-%d", False), ("%Y-%m", True)):
        try:
            if fmt == "%Y-%m":
                dt = datetime.strptime(s[:7], "%Y-%m")
            else:
                dt = datetime.strptime(s[:10], "%Y-%m-%d")
            d = dt.date()
            if end_of_month and (use_eom or len(s) <= 7):
                if d.month == 12:
                    return datetime(d.year, 12, 31)
                next_month = d.replace(day=28) + timedelta(days=4)
                last_day = (next_month - timedelta(days=next_month.day)).day
                return datetime(d.year, d.month, last_day)
            return datetime.combine(d, datetime.min.time())
        except ValueError:
            continue
    return None


def _normalize_gl_date_param(raw: str, *, end: bool) -> str:
    s = (raw or "").strip()
    if not s:
        raise ValueError("date is required")
    try:
        start_d, end_d = parse_entity_period(s)
        return (end_d if end else start_d).isoformat()
    except ValueError:
        pass
    parsed = _parse_entity_period_date(s, end_of_month=end)
    if parsed:
        return parsed.date().isoformat()
    return s[:10]


def _resolve_gl_dates(
    entity: Entity,
    start_date: Optional[str],
    end_date: Optional[str],
) -> tuple[str, str]:
    if start_date and end_date:
        return (
            _normalize_gl_date_param(start_date, end=False),
            _normalize_gl_date_param(end_date, end=True),
        )
    return _default_gl_dates(entity)


def _default_gl_dates(entity: Entity) -> tuple[str, str]:
    today = datetime.utcnow().date()
    start_dt = _parse_entity_period_date(entity.period_from)
    end_dt = _parse_entity_period_date(entity.period_to, end_of_month=True)
    start = start_dt.date() if start_dt else today.replace(month=1, day=1)
    end = end_dt.date() if end_dt else today.replace(month=12, day=31)
    return start.isoformat(), end.isoformat()


@router.get(
    "/credentials/{entity_code}",
    response_model=QbCredentialOut,
    summary="Get QuickBooks credential setup (safe fields only)",
)
def get_quickbooks_credentials(entity_code: str, db: Session = Depends(get_db)):
    _require_qb_mapping(db, entity_code)
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == entity_code).first()
    return _to_credential_out(rec, entity_code)


@router.post(
    "/credentials",
    response_model=QbCredentialOut,
    status_code=status.HTTP_201_CREATED,
    summary="Save / update QuickBooks app credentials",
)
def upsert_quickbooks_credentials(
    body: QbCredentialUpsert,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    _require_entity(db, body.entity_id)
    _require_qb_mapping(db, body.entity_id)
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == body.entity_id).first()
    is_create = rec is None

    if is_create:
        missing = [f for f, val in (
            ("client_id", body.client_id),
            ("client_secret", body.client_secret),
            ("redirect_uri", body.redirect_uri),
            ("environment", body.environment),
            ("auth_url", body.auth_url),
            ("token_url", body.token_url),
            ("api_base_sandbox", body.api_base_sandbox),
            ("api_base_production", body.api_base_production),
        ) if not val]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required field(s) on first save: {', '.join(missing)}",
            )
        env = body.environment or "sandbox"
        if env == "sandbox" and not body.api_base_sandbox:
            raise HTTPException(status_code=400, detail="api_base_sandbox is required for sandbox")
        if env == "production" and not body.api_base_production:
            raise HTTPException(status_code=400, detail="api_base_production is required for production")

    if is_create:
        rec = QuickBooksCredential(
            entity_id=body.entity_id,
            erp_type=ERP_TYPE_QUICKBOOKS,
            client_id_encrypted=encrypt_credential_field(body.client_id or ""),
            client_secret_encrypted=encrypt_credential_field(body.client_secret or ""),
            redirect_uri=body.redirect_uri or DEFAULT_QB_REDIRECT_URI,
            environment=body.environment or "sandbox",
            scopes=body.scopes or DEFAULT_QB_SCOPE,
            auth_url=body.auth_url or DEFAULT_QB_AUTH_URL,
            token_url=body.token_url or DEFAULT_QB_TOKEN_URL,
            api_base_sandbox=body.api_base_sandbox or DEFAULT_QB_API_BASE_SANDBOX,
            api_base_production=body.api_base_production or DEFAULT_QB_API_BASE_PRODUCTION,
            last4_client=mask_for_display(body.client_id or "")[-4:] if body.client_id else None,
            last4_secret=mask_for_display(body.client_secret or "")[-4:] if body.client_secret else None,
            status="saved",
            created_by=actor.id,
            updated_by=actor.id,
        )
        db.add(rec)
    else:
        assert rec is not None
        if body.client_id:
            rec.client_id_encrypted = encrypt_credential_field(body.client_id)
            rec.last4_client = mask_for_display(body.client_id)[-4:]
        if body.client_secret:
            rec.client_secret_encrypted = encrypt_credential_field(body.client_secret)
            rec.last4_secret = mask_for_display(body.client_secret)[-4:]
        if body.redirect_uri:
            rec.redirect_uri = body.redirect_uri
        if body.environment:
            rec.environment = body.environment
        if body.scopes:
            rec.scopes = body.scopes
        if body.auth_url:
            rec.auth_url = body.auth_url
        if body.token_url:
            rec.token_url = body.token_url
        if body.api_base_sandbox:
            rec.api_base_sandbox = body.api_base_sandbox
        if body.api_base_production:
            rec.api_base_production = body.api_base_production
        if not rec.realm_id:
            rec.status = "saved"
        rec.updated_by = actor.id

    log_change(
        db, actor=actor, action="qb.credentials.upsert",
        table_name="quickbooks_credentials", row_pk=body.entity_id,
        request=request,
    )
    db.commit()
    db.refresh(rec)
    return _to_credential_out(rec, body.entity_id)


@router.get("/connect", response_model=QbConnectOut, summary="Start QuickBooks OAuth")
def connect_quickbooks(
    entity_id: str = Query(...),
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    _require_qb_mapping(db, entity_id)
    rec = _get_or_create_cred(db, entity_id, actor.id)
    if not oauth_ready(rec):
        raise HTTPException(
            status_code=409,
            detail="Save QuickBooks credentials before connecting.",
        )
    state = generate_oauth_state()
    rec.oauth_state = state
    rec.oauth_state_expires_at = datetime.utcnow() + timedelta(minutes=15)
    rec.status = "testing"
    rec.updated_by = actor.id
    db.commit()
    url = build_authorization_url(rec=rec, state=state)
    return QbConnectOut(authorization_url=url)


@callback_router.get("/callback", summary="QuickBooks OAuth callback (Intuit redirect)")
def quickbooks_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    realmId: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    frontend = f"{settings.APP_URL.rstrip('/')}/admin/integrations"
    if error or not code or not state or not realmId:
        return RedirectResponse(f"{frontend}?qb=error", status_code=302)
    rec = (
        db.query(QuickBooksCredential)
        .filter(QuickBooksCredential.oauth_state == state)
        .first()
    )
    if not rec or not rec.oauth_state_expires_at or rec.oauth_state_expires_at < datetime.utcnow():
        return RedirectResponse(f"{frontend}?qb=state_invalid", status_code=302)
    try:
        bundle = exchange_code_for_tokens(rec, code)
        store_tokens(rec, bundle)
        rec.realm_id = realmId
        rec.oauth_state = None
        rec.oauth_state_expires_at = None
        rec.status = "verified"
        rec.last_verified_at = datetime.utcnow()
        rec.last_safe_error_code = None
        rec.last_safe_error_message = None
        info = fetch_company_info(rec)
        if info.ok and info.records:
            rec.company_name = info.records[0].get("company_name") or rec.company_name
        db.commit()
        return RedirectResponse(f"{frontend}?entity={rec.entity_id}&qb=connected", status_code=302)
    except Exception as exc:  # noqa: BLE001
        _log.warning("QuickBooks OAuth callback failed: %s", type(exc).__name__)
        err = oauth_error_from_exception(exc)
        rec.status = "failed"
        rec.last_safe_error_code = err.code
        rec.last_safe_error_message = err.message
        db.commit()
        return RedirectResponse(f"{frontend}?entity={rec.entity_id}&qb=failed", status_code=302)


@router.get("/status/{entity_code}", response_model=QbStatusOut, summary="QuickBooks connection status")
def quickbooks_status(entity_code: str, db: Session = Depends(get_db)):
    _require_qb_mapping(db, entity_code)
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == entity_code).first()
    return _to_status(rec, entity_code)


@router.post("/disconnect", summary="Disconnect QuickBooks")
def disconnect_quickbooks(
    body: QbSyncBody,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    _require_qb_mapping(db, body.entity_id)
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == body.entity_id).first()
    realm = rec.realm_id if rec else None
    if rec:
        clear_tokens(rec)
        rec.realm_id = None
        rec.company_name = None
        rec.status = "saved" if app_credentials_saved(rec) else "not_configured"
        rec.oauth_state = None
        rec.updated_by = actor.id
    if realm:
        db.query(ErpGlAccount).filter(
            ErpGlAccount.entity_id == body.entity_id,
            ErpGlAccount.erp_type == ERP_TYPE_QUICKBOOKS,
            ErpGlAccount.company_id == realm,
        ).delete(synchronize_session=False)
        db.query(ErpGeneralLedgerEntry).filter(
            ErpGeneralLedgerEntry.entity_id == body.entity_id,
            ErpGeneralLedgerEntry.erp_type == ERP_TYPE_QUICKBOOKS,
            ErpGeneralLedgerEntry.company_id == realm,
        ).delete(synchronize_session=False)
        db.query(ErpCustomer).filter(
            ErpCustomer.entity_id == body.entity_id,
            ErpCustomer.erp_type == ERP_TYPE_QUICKBOOKS,
            ErpCustomer.company_id == realm,
        ).delete(synchronize_session=False)
        db.query(ErpVendor).filter(
            ErpVendor.entity_id == body.entity_id,
            ErpVendor.erp_type == ERP_TYPE_QUICKBOOKS,
            ErpVendor.company_id == realm,
        ).delete(synchronize_session=False)
    log_change(db, actor=actor, action="qb.disconnect", table_name="quickbooks_credentials",
               row_pk=body.entity_id, request=request)
    db.commit()
    return {"ok": True}


@router.post("/test-connection", response_model=QbConnectorOut, summary="Test QuickBooks connection")
def test_quickbooks_connection(
    body: QbSyncBody,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    _require_qb_mapping(db, body.entity_id)
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == body.entity_id).first()
    if not rec or not rec.realm_id:
        return QbConnectorOut(ok=False, safe_error_code="QUICKBOOKS_REALM_ID_MISSING",
                              safe_error_message=safe_error("QUICKBOOKS_REALM_ID_MISSING").message)
    rec.status = "testing"
    rec.last_tested_at = datetime.utcnow()
    run = _start_sync_run(db, body.entity_id, SYNC_TEST_CONNECTION)
    result = svc_test_connection(rec)
    if result.ok:
        if result.meta:
            rec.company_name = result.meta.get("company_name") or rec.company_name
        rec.status = "verified"
        rec.last_verified_at = datetime.utcnow()
        rec.last_safe_error_code = None
        rec.last_safe_error_message = None
    else:
        rec.status = "failed"
        if result.error:
            rec.last_safe_error_code = result.error.code
            rec.last_safe_error_message = result.error.message
    _finish_sync_run(db, run, ok=result.ok, count=len(result.records), err=result.error)
    db.commit()
    return _connector_out(result, len(result.records), rec)


@router.get("/company", response_model=QbConnectorOut, summary="QuickBooks company info")
def get_quickbooks_company(
    entity_id: str = Query(...),
    db: Session = Depends(get_db),
):
    _require_qb_mapping(db, entity_id)
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == entity_id).first()
    if not rec:
        return QbConnectorOut(ok=False, safe_error_code="QUICKBOOKS_NOT_CONFIGURED")
    result = fetch_company_info(rec)
    return _connector_out(result, len(result.records), rec)


@router.get("/readiness/{entity_code}", response_model=QbReadinessOut, summary="QuickBooks readiness")
def quickbooks_readiness(entity_code: str, db: Session = Depends(get_db)):
    _require_qb_mapping(db, entity_code)
    return QbReadinessOut(**calculate_quickbooks_readiness(db, entity_code))


@router.post("/sync/accounts", response_model=QbConnectorOut, summary="Sync QuickBooks chart of accounts")
def sync_quickbooks_accounts(
    body: QbSyncBody,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    _require_qb_mapping(db, body.entity_id)
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == body.entity_id).first()
    if not rec or not rec.realm_id:
        return QbConnectorOut(ok=False, safe_error_code="QUICKBOOKS_REALM_ID_MISSING")
    rec.status = "syncing"
    run = _start_sync_run(db, body.entity_id, SYNC_GL_ACCOUNTS)
    result = svc_sync_accounts(rec)
    count = 0
    if result.ok:
        try:
            count = _replace_accounts(db, body.entity_id, rec.realm_id, result.records)
            rec.status = "synced"
        except SQLAlchemyError:
            _log.exception("QuickBooks accounts persist failed")
            result = ConnectorResult(ok=False, records=[], error=safe_error(DATABASE_ERROR))
    else:
        rec.status = "failed"
        if result.error:
            rec.last_safe_error_code = result.error.code
            rec.last_safe_error_message = result.error.message
    _finish_sync_run(db, run, ok=result.ok, count=count, err=result.error)
    db.commit()
    return _connector_out(result, count, rec)


@router.post("/sync/general-ledger", response_model=QbConnectorOut, summary="Sync QuickBooks general ledger")
def sync_quickbooks_gl(
    body: QbSyncBody,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    _require_qb_mapping(db, body.entity_id)
    entity = db.query(Entity).filter(Entity.id == body.entity_id).first()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == body.entity_id).first()
    if not rec or not rec.realm_id:
        return QbConnectorOut(ok=False, safe_error_code="QUICKBOOKS_REALM_ID_MISSING")
    start_date, end_date = _resolve_gl_dates(entity, body.start_date, body.end_date)
    rec.status = "syncing"
    run = _start_sync_run(db, body.entity_id, SYNC_GL_ENTRIES)
    result = svc_sync_gl(rec, start_date=start_date, end_date=end_date)
    count = 0
    notice: SafeError | None = None
    diagnostics = dict(result.meta or {})
    if result.ok:
        try:
            count = _replace_gl_entries(db, body.entity_id, rec.realm_id, result.records)
            rec.status = "synced"
            if count == 0:
                data_rows = int(diagnostics.get("data_row_count") or 0)
                top_rows = int(diagnostics.get("top_level_row_count") or 0)
                nested_rows = int(diagnostics.get("nested_row_count") or 0)
                if data_rows > 0 or top_rows > 0 or nested_rows > 0:
                    notice = safe_error(QUICKBOOKS_GL_PARSE_EMPTY)
                else:
                    notice = safe_error(QUICKBOOKS_GL_NO_TRANSACTIONS)
        except SQLAlchemyError:
            _log.exception("QuickBooks GL persist failed")
            result = ConnectorResult(ok=False, records=[], error=safe_error(DATABASE_ERROR))
    else:
        rec.status = "failed"
        if result.error:
            rec.last_safe_error_code = result.error.code
            rec.last_safe_error_message = result.error.message
    _finish_sync_run(db, run, ok=result.ok, count=count, err=result.error or notice)
    db.commit()
    out = _connector_out(result, count, rec, gl_sync_diagnostics=diagnostics or None, notice=notice)
    _log.info(
        "[QB GL SYNC] API response entity=%s dates=%s..%s persisted=%d notice=%s response=%s",
        body.entity_id,
        start_date,
        end_date,
        count,
        notice.code if notice else None,
        out.model_dump_json(),
    )
    return out


@router.post("/sync/customers", response_model=QbConnectorOut, summary="Sync QuickBooks customers")
def sync_quickbooks_customers(body: QbSyncBody, db: Session = Depends(get_db)):
    _require_qb_mapping(db, body.entity_id)
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == body.entity_id).first()
    if not rec or not rec.realm_id:
        return QbConnectorOut(ok=False, safe_error_code="QUICKBOOKS_REALM_ID_MISSING")
    run = _start_sync_run(db, body.entity_id, SYNC_CUSTOMERS)
    result = svc_sync_customers(rec)
    count = _replace_customers(db, body.entity_id, rec.realm_id, result.records) if result.ok else 0
    _finish_sync_run(db, run, ok=result.ok, count=count, err=result.error)
    db.commit()
    return _connector_out(result, count, rec)


@router.post("/sync/vendors", response_model=QbConnectorOut, summary="Sync QuickBooks vendors")
def sync_quickbooks_vendors(body: QbSyncBody, db: Session = Depends(get_db)):
    _require_qb_mapping(db, body.entity_id)
    rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == body.entity_id).first()
    if not rec or not rec.realm_id:
        return QbConnectorOut(ok=False, safe_error_code="QUICKBOOKS_REALM_ID_MISSING")
    run = _start_sync_run(db, body.entity_id, SYNC_VENDORS)
    result = svc_sync_vendors(rec)
    count = _replace_vendors(db, body.entity_id, rec.realm_id, result.records) if result.ok else 0
    _finish_sync_run(db, run, ok=result.ok, count=count, err=result.error)
    db.commit()
    return _connector_out(result, count, rec)


@router.get("/accounts/{entity_code}", response_model=List[ErpAccountOut], summary="List synced QB accounts")
def list_quickbooks_accounts(entity_code: str, db: Session = Depends(get_db)):
    _require_qb_mapping(db, entity_code)
    rows = (
        db.query(ErpGlAccount)
        .filter(
            ErpGlAccount.entity_id == entity_code,
            ErpGlAccount.erp_type == ERP_TYPE_QUICKBOOKS,
        )
        .order_by(ErpGlAccount.account_no)
        .all()
    )
    return [
        ErpAccountOut(
            account_no=r.account_no,
            account_name=r.account_name,
            display_name=r.display_name or r.account_name,
            account_type=r.account_type,
            account_category=r.account_category,
        )
        for r in rows
    ]


@router.get("/customers/{entity_code}", response_model=List[ErpPartyOut], summary="List synced QB customers")
def list_quickbooks_customers(entity_code: str, db: Session = Depends(get_db)):
    _require_qb_mapping(db, entity_code)
    rows = (
        db.query(ErpCustomer)
        .filter(ErpCustomer.entity_id == entity_code, ErpCustomer.erp_type == ERP_TYPE_QUICKBOOKS)
        .order_by(ErpCustomer.display_name)
        .all()
    )
    return [ErpPartyOut(code=r.customer_no, name=r.display_name, display_name=r.display_name) for r in rows]


@router.get("/vendors/{entity_code}", response_model=List[ErpPartyOut], summary="List synced QB vendors")
def list_quickbooks_vendors(entity_code: str, db: Session = Depends(get_db)):
    _require_qb_mapping(db, entity_code)
    rows = (
        db.query(ErpVendor)
        .filter(ErpVendor.entity_id == entity_code, ErpVendor.erp_type == ERP_TYPE_QUICKBOOKS)
        .order_by(ErpVendor.display_name)
        .all()
    )
    return [ErpPartyOut(code=r.vendor_no, name=r.display_name, display_name=r.display_name) for r in rows]


@router.get("/general-ledger/{entity_code}", summary="List synced QB GL entries (sample)")
def list_quickbooks_gl(entity_code: str, limit: int = Query(200, le=2000), db: Session = Depends(get_db)):
    _require_qb_mapping(db, entity_code)
    rows = (
        db.query(ErpGeneralLedgerEntry)
        .filter(
            ErpGeneralLedgerEntry.entity_id == entity_code,
            ErpGeneralLedgerEntry.erp_type == ERP_TYPE_QUICKBOOKS,
        )
        .order_by(ErpGeneralLedgerEntry.posting_date.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "posting_date": r.posting_date.isoformat() if r.posting_date else None,
            "account_no": r.account_no,
            "document_no": r.document_no,
            "amount": float(r.amount) if r.amount is not None else None,
            "description": r.description,
        }
        for r in rows
    ]


def _report_route(report_name: str, summary: str):
    @router.get(f"/reports/{report_name}", summary=summary)
    def _handler(
        entity_id: str = Query(...),
        start_date: Optional[str] = Query(None),
        end_date: Optional[str] = Query(None),
        db: Session = Depends(get_db),
    ):
        _require_qb_mapping(db, entity_id)
        rec = db.query(QuickBooksCredential).filter(QuickBooksCredential.entity_id == entity_id).first()
        if not rec or not rec.realm_id:
            raise HTTPException(status_code=409, detail="QuickBooks not connected")
        qb_report = {
            "trial-balance": "TrialBalance",
            "profit-loss": "ProfitAndLoss",
            "balance-sheet": "BalanceSheet",
            "cash-flow": "CashFlow",
        }[report_name]
        result = fetch_report(rec, qb_report, start_date=start_date, end_date=end_date)
        if not result.ok:
            raise HTTPException(status_code=502, detail=result.error.message if result.error else "Report failed")
        return result.meta or {}

    return _handler


_report_route("trial-balance", "QuickBooks Trial Balance report")
_report_route("profit-loss", "QuickBooks Profit and Loss report")
_report_route("balance-sheet", "QuickBooks Balance Sheet report")
_report_route("cash-flow", "QuickBooks Cash Flow report")
