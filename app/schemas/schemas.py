# @file    schemas.py

from __future__ import annotations
from datetime import date, datetime, time
from typing import List, Optional, Any, Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# ── Shared ───────────────────────────────────────────────────────────
class OrmBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── User ─────────────────────────────────────────────────────────────
class UserOut(OrmBase):
    id: int
    name: str
    initials: str
    color: str
    role: str
    email: str
    department: Optional[str] = None
    status: str = "active"


class UserMe(UserOut):
    """Returned by GET /auth/me — adds visible entity ids."""
    visible_entity_ids: Optional[List[str]] = None


# ── Auth ─────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserMe


class AcceptInviteRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)


# ── Entity ───────────────────────────────────────────────────────────
class EntityOut(OrmBase):
    id: str
    name: str
    region: str
    status: str
    tags: Any
    period_type: str
    period_from: str
    period_to: str
    period_from_label: Optional[str] = None
    period_to_label: Optional[str] = None
    sub_due_date: Optional[date] = None
    rev_due_date: Optional[date] = None
    approval_level: str
    supervisor: UserOut
    director: Optional[UserOut] = None
    currency: Optional[str] = None
    fiscal_year_end: Optional[str] = None
    tax_id: Optional[str] = None
    is_active: bool = True


class EntityListItem(EntityOut):
    """Lighter list view including assignees + macro count for dashboard."""
    assignees: List[UserOut] = []
    macro_factor_count: int = 0
    # Review progress for the queue cards. Additive + optional with safe
    # defaults so existing consumers are unaffected; computed best-effort and
    # never allowed to break the entity list endpoint.
    caption_count: int = 0
    signed_count: int = 0
    review_progress: int = 0


class EntityStatusUpdate(BaseModel):
    status: str
    comment: Optional[str] = None


# ── Flux ─────────────────────────────────────────────────────────────
class FluxRowOut(OrmBase):
    id: int
    caption: str
    current_value: float
    comp_value: float
    pct_variance: Optional[float]
    is_material: bool
    is_bold: bool
    sort_order: int
    # Provenance — surfaced so Flux Review can show "Generated from BC"
    # vs seed/manual rows. NULL on legacy rows is treated as seed.
    source_type: Optional[str] = None
    generated_from: Optional[str] = None
    generated_at: Optional[datetime] = None
    # Finance-normalized variance (credit-normal revenue uses magnitude logic).
    normal_balance: Optional[str] = None
    raw_variance_amount: Optional[float] = None
    finance_current_value: Optional[float] = None
    finance_comparison_value: Optional[float] = None
    finance_variance_amount: Optional[float] = None
    finance_variance_percentage: Optional[float] = None
    finance_direction: Optional[str] = None


# ── AI Commentary ─────────────────────────────────────────────────────
class AiCommentaryOut(OrmBase):
    caption: str
    concise_text: str
    detailed_text: str
    edited_text: Optional[str] = None
    edited_at: Optional[datetime] = None
    ai_score: int
    editor: Optional[UserOut] = None


class AiCommentaryEdit(BaseModel):
    text: str


# ── Flux AI generate ──────────────────────────────────────────────────
class FluxAiGenerateRequest(BaseModel):
    """Body for `POST /api/entities/{entity_id}/flux-ai/generate`."""
    statement: Literal["pl", "bs"]
    caption: str = Field(..., min_length=1, max_length=200)


class FluxAiVarianceDriver(BaseModel):
    driver: str
    evidence: str = ""
    amount: Optional[float] = None
    impact: Literal["low", "medium", "high"] = "low"


class FluxAiRiskFlag(BaseModel):
    risk: str
    severity: Literal["low", "medium", "high"] = "low"
    reason: str = ""


class FluxAiGenerateResponse(BaseModel):
    """Single response shape for both success and SLM-side failures.

    The `ok` flag tells the frontend which case it is in. Auth / access
    failures still return HTTP 401/403/404; runtime AI failures (timeout,
    parse, schema) return HTTP 200 with `ok=false` so a single fetch path
    can render either toast variant cleanly.

    The new schema (May 2026) emits plain string arrays for ``drivers``,
    ``mapped_accounts``, ``transaction_notes``, ``review_notes``, and
    ``recommended_actions``. The legacy structured fields
    (``variance_drivers``, ``risk_flags``) remain for back-compat and
    populate only when the SLM returned the older shape.
    """
    ok: bool
    entity_id: str
    statement: str
    caption: str
    concise_text: Optional[str] = None
    detailed_text: Optional[str] = None
    drivers: List[str] = []
    mapped_accounts: List[str] = []
    customer_vendor_notes: List[str] = []
    transaction_notes: List[str] = []
    party_diagnostics: Optional[dict] = None
    review_notes: List[str] = []
    recommended_actions: List[str] = []
    confidence_score: Optional[float] = None
    # Legacy fields preserved for back-compat.
    variance_drivers: List[FluxAiVarianceDriver] = []
    risk_flags: List[FluxAiRiskFlag] = []
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None


# ── Flux generate-from-BC ────────────────────────────────────────────
class FluxGenerateRequest(BaseModel):
    """Body for `POST /api/admin/entities/{id}/generate-flux-from-bc`."""
    statement: Literal["pl", "bs", "both"] = "both"
    mode: Literal["preview", "commit"] = "preview"
    # Renamed from `overwrite_demo_data` for clarity. Aliased so older
    # frontend payloads keep working without churn.
    overwrite_existing: bool = Field(
        default=False, alias="overwriteExisting",
    )
    source: Literal["business_central"] = "business_central"

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class FluxPreviewRowOut(BaseModel):
    statement: str
    caption: str
    current_value: float
    comp_value: float
    pct_variance: Optional[float] = None
    is_material: bool
    had_existing_row: bool
    matched_account_count: int = 0
    matched_transaction_count: int = 0
    skipped_reason: Optional[str] = None
    selected_gl_accounts: List[str] = Field(default_factory=list, alias="selectedGlAccounts")
    selected_subledger: List[str] = Field(default_factory=list, alias="selectedSubledger")
    selected_dimensions: List[str] = Field(default_factory=list, alias="selectedDimensions")
    warnings: List[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class FluxSkippedCaptionOut(BaseModel):
    caption: str
    reason: str


class FluxGenerateResponse(BaseModel):
    mode: Literal["preview", "commit"]
    entity_id: str
    statement: str
    rows_previewed: int
    rows_inserted: int
    rows_updated: int
    rows_skipped: int
    preview_rows: List[FluxPreviewRowOut] = []
    # Diagnostics for the admin UI — surfaced in both preview and commit
    # so a single shape covers both flows.
    captions_total: int = 0
    captions_with_mappings: int = 0
    captions_without_mappings: List[str] = []
    captions_with_zero_transactions: List[str] = []
    matched_transaction_total: int = 0
    rows_generated: int = Field(default=0, alias="rowsGenerated")
    matched_transactions: int = Field(default=0, alias="matchedTransactions")
    unmapped_captions: List[str] = Field(default_factory=list, alias="unmappedCaptions")
    captions_with_no_transactions: List[str] = Field(
        default_factory=list, alias="captionsWithNoTransactions",
    )
    skipped_captions: List[FluxSkippedCaptionOut] = Field(
        default_factory=list, alias="skippedCaptions",
    )
    statement_calculations: List["FluxStatementCalculationOut"] = Field(
        default_factory=list, alias="statementCalculations",
    )

    model_config = ConfigDict(populate_by_name=True)


class FluxStatementCalculationOut(BaseModel):
    statement: str
    calculation_mode: str = Field(alias="calculationMode")
    calculation_label: str = Field(alias="calculationLabel")
    current_start_date: date = Field(alias="currentStartDate")
    current_end_date: date = Field(alias="currentEndDate")
    comparison_start_date: date = Field(alias="comparisonStartDate")
    comparison_end_date: date = Field(alias="comparisonEndDate")
    mapped_gl_accounts_count: int = Field(default=0, alias="mappedGlAccountsCount")
    selected_subledger_count: int = Field(default=0, alias="selectedSubledgerCount")
    matched_transactions: int = Field(default=0, alias="matchedTransactions")
    rows_generated: int = Field(default=0, alias="rowsGenerated")
    warnings: List[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class BcFluxDiagnosticsOut(BaseModel):
    """Response of `GET /api/admin/entities/{id}/bc-flux-diagnostics`.

    Exposes the signals the admin UI needs to debug an empty Flux Review:
    how many captions have an ontology mapping, how many GL entries have
    been synced, whether the current `flux_data` rows are seed or BC-
    generated, and so on.
    """
    entity_id: str
    period_from: str
    period_to: str

    ontology_mappings_total: int
    ontology_mappings_pl: int
    ontology_mappings_bs: int
    captions_with_mapped_gl: int
    captions_without_mapped_gl: List[str] = []
    captions_with_mapped_dimensions: int

    erp_gl_accounts_synced: int
    erp_dimension_values_synced: int
    erp_gl_entries_synced: int

    flux_rows_total: int
    flux_rows_seed: int
    flux_rows_bc_generated: int
    flux_rows_manual: int
    last_generated_at: Optional[datetime] = None


# ── Demo ontology auto-map ───────────────────────────────────────────
class OntologyAutoMapRequest(BaseModel):
    """Body for the admin demo auto-map endpoint."""
    statement: Literal["pl", "bs", "both"] = "both"
    overwrite: bool = False
    dry_run: bool = True


class OntologyAutoMapEntry(BaseModel):
    statement: str
    tflux_caption: str
    matched_gl_accounts: List[str] = []
    matched_dimension_values: List[str] = []
    is_new_mapping: bool
    skipped_reason: Optional[str] = None
    mapped_accounts_count: int = 0
    matched_transaction_count: int = 0
    skipped_subtotal_accounts: int = 0
    skipped_non_posting_accounts: int = 0
    skipped_suspicious_category_accounts: int = 0


class OntologyAutoMapResponse(BaseModel):
    entity_id: str
    statement: str
    dry_run: bool
    overwrite: bool
    captions_total: int
    captions_mapped: int
    captions_skipped: int
    entries: List[OntologyAutoMapEntry] = []
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None


# ── Comments ─────────────────────────────────────────────────────────
class ReplyOut(OrmBase):
    id: int
    text: str
    created_at: datetime
    user: UserOut


class CommentOut(OrmBase):
    id: int
    caption: str
    text: str
    is_resolved: bool
    resolved_at: Optional[datetime] = None
    created_at: datetime
    user: UserOut
    resolver: Optional[UserOut] = None
    replies: List[ReplyOut] = []


CommentOut.model_rebuild()


class CommentCreate(BaseModel):
    text: str
    parent_id: Optional[int] = None


# ── Sign-Offs ────────────────────────────────────────────────────────
class SignoffOut(OrmBase):
    caption: str
    signed_at: datetime
    version: int
    is_stale: bool
    signer: UserOut


class BulkSignoffCreate(BaseModel):
    statement: Literal["pl", "bs"]


# ── Macro Factors ────────────────────────────────────────────────────
class MacroFactorOut(OrmBase):
    id: int
    category: str
    icon: str
    text: str
    is_custom: bool
    created_at: datetime
    creator: UserOut


class MacroFactorCreate(BaseModel):
    category: str
    icon: str = "📌"
    text: str


class MacroFactorEdit(BaseModel):
    text: str


# ── Tasks ────────────────────────────────────────────────────────────
class TaskReminderOut(OrmBase):
    days_before: int
    remind_time: time
    email: str
    is_sent: bool


class TaskRecurrenceOut(OrmBase):
    frequency: str
    day_of_month: Optional[int] = None


class TaskOut(OrmBase):
    id: int
    entity_id: str
    title: str
    description: Optional[str] = None
    due_date: date
    status: str
    priority: str
    phase: str
    is_recurring: bool
    created_at: datetime
    updated_at: datetime
    assignee: UserOut
    reminder: Optional[TaskReminderOut] = None
    recurrence: Optional[TaskRecurrenceOut] = None


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    assignee_id: int
    due_date: date
    status: str = "todo"
    priority: str = "medium"
    phase: str = "preparation"
    reminder: Optional[dict] = None
    recurrence: Optional[dict] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    assignee_id: Optional[int] = None
    due_date: Optional[date] = None
    priority: Optional[str] = None
    phase: Optional[str] = None


# ── Personal Reminders ───────────────────────────────────────────────
class PersonalReminderOut(OrmBase):
    id: int
    text: str
    due_date: date
    remind_time: time
    is_done: bool
    created_at: datetime


class PersonalReminderCreate(BaseModel):
    text: str
    due_date: date
    remind_time: time = time(9, 0)


# ── Chat ─────────────────────────────────────────────────────────────
class ChatReplyOut(OrmBase):
    id: int
    text: str
    created_at: datetime
    user: UserOut


class ChatMessageOut(OrmBase):
    id: int
    text: str
    is_flagged: bool
    created_at: datetime
    user: UserOut
    replies: List[ChatReplyOut] = []


class ChatMessageCreate(BaseModel):
    text: str


class ChatReplyCreate(BaseModel):
    text: str


# ── Profile & Thresholds & Periods ───────────────────────────────────
class CompanyProfileOut(OrmBase):
    industry: str
    geography: str
    revenue_range: str
    entity_type: str
    brief: Optional[str] = None


class CompanyProfileUpdate(BaseModel):
    industry: Optional[str] = None
    geography: Optional[str] = None
    revenue_range: Optional[str] = None
    entity_type: Optional[str] = None
    brief: Optional[str] = None


class ThresholdOut(OrmBase):
    statement: str
    mode: str
    pct_value: float
    abs_value: float
    logic: str
    apply_all: bool


class ThresholdUpdate(BaseModel):
    statement: Literal["pl", "bs"]
    mode: Literal["pct","abs","both"] = "both"
    pct_value: float = 10.0
    abs_value: float = 100000.0
    logic: Literal["and","or"] = "or"
    apply_all: bool = True


class PeriodUpdate(BaseModel):
    period_type: Literal["YoY","MoM","QoQ","Custom"]
    period_from: str
    period_to: str


# ── Save & Generate (period save + BC flux regeneration) ─────────────
class SavePeriodAndGenerateRequest(BaseModel):
    """Body for `POST /api/entities/{id}/flux/save-period-and-generate`."""
    period_type: Literal["YoY","MoM","QoQ","Custom"]
    period_from: str
    period_to:   str
    statement: Literal["pl","bs","both"] = "both"
    overwrite_existing: bool = Field(
        default=True, alias="overwriteExisting",
    )

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class SavePeriodAndGeneratePeriod(BaseModel):
    """Resolved date window echoed back to the supervisor UI so the toast
    + diagnostics panel can show *exactly* what the backend computed."""
    current_start_date:    date
    current_end_date:      date
    comparison_start_date: date
    comparison_end_date:   date


class PeriodRangeOut(BaseModel):
    """Human-friendly period window for diagnostics responses."""
    start_date: date
    end_date: date
    label: str


class StatementGenerationBreakdown(BaseModel):
    """Per-statement (`pl` / `bs`) totals split out of the combined
    response so the supervisor UI can render breakdowns like
    `"PL: 0 / 8 mapped"` even when BS regenerated cleanly. The most
    common debug aid for the demo: confirms whether PL produced any
    rows at all without scanning the row list."""
    statement: str
    captions_total: int = 0
    captions_with_mappings: int = 0
    captions_without_mappings: List[str] = []
    captions_with_zero_transactions: List[str] = []
    matched_transaction_total: int = 0
    current_transactions_matched: int = 0
    comparison_transactions_matched: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_skipped: int = 0
    rows_generated: int = 0


class SavePeriodAndGenerateResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ok: bool
    entity_id: str
    period_type: str
    period_from: str
    period_to: str
    saved_period_from: str = Field(alias="savedPeriodFrom")
    saved_period_to: str = Field(alias="savedPeriodTo")
    current_label: str
    comparison_label: str
    current_range: PeriodRangeOut = Field(alias="currentRange")
    comparison_range: PeriodRangeOut = Field(alias="comparisonRange")
    period: SavePeriodAndGeneratePeriod
    rows_inserted: int
    rows_updated: int
    rows_skipped: int
    rows_generated: int                    # = rows_inserted + rows_updated
    captions_total: int
    captions_with_mappings: int
    captions_without_mappings: List[str] = []
    captions_with_zero_transactions: List[str] = []
    matched_transaction_total: int
    current_transactions_matched: int
    comparison_transactions_matched: int
    statements: dict[str, StatementGenerationBreakdown] = {}
    safe_error_code: Optional[str] = None
    safe_error_message: Optional[str] = None
    warnings: List[str] = []


# ── Flux generation diagnostics (per-entity, supervisor & admin) ─────
class FluxGenerationCaptionDetailOut(BaseModel):
    """Caption-level diagnostic. Returned for every ontology mapping in
    the active filter so the supervisor can pinpoint which P&L caption
    failed to match (e.g. ``Consulting Revenue`` mapped to ``6710`` but
    no transactions in the selected period)."""
    statement: str
    caption: str
    calculation_mode: str = Field(default="net_change", alias="calculationMode")
    mapped_gl_accounts: List[str] = []
    selected_subledger: List[str] = Field(default_factory=list, alias="selectedSubledger")
    matched_current_transactions: int = 0
    matched_comparison_transactions: int = 0
    current_value: float = 0.0
    comparison_value: float = 0.0

    model_config = ConfigDict(populate_by_name=True)


class FluxGenerationDiagnosticsOut(BaseModel):
    entity_id: str
    period_type: str
    period_from_raw: str
    period_to_raw: str
    current_label: str
    comparison_label: str
    current_start_date: date
    current_end_date: date
    comparison_start_date: date
    comparison_end_date: date

    bc_oldest_posting_date: Optional[date] = None
    bc_latest_posting_date: Optional[date] = None
    bc_entry_count: int
    is_selected_range_within_bc_data: bool
    warnings: List[str] = []

    ontology_mappings_total: int
    captions_with_mapped_gl: int
    captions_without_mapped_gl: List[str] = []
    gl_entries_in_current_range: int
    gl_entries_in_comparison_range: int
    flux_rows_for_selected_period: int
    statement_filter: Optional[str] = None
    captions: List[FluxGenerationCaptionDetailOut] = []


# ── Delegate / Workflow ──────────────────────────────────────────────
class DelegateCreate(BaseModel):
    to_user_id: int
    note: Optional[str] = None


class WorkflowAction(BaseModel):
    note: Optional[str] = None


# ── Export ───────────────────────────────────────────────────────────
class ExportRequest(BaseModel):
    mode: Literal["concise","detailed"] = "concise"
    statement: Literal["pl","bs","both"] = "both"


# ── Admin: Users ─────────────────────────────────────────────────────
class AdminUserOut(UserOut):
    supervisor: Optional[UserOut] = None
    entity_codes: List[str] = []
    last_login_at: Optional[datetime] = None
    invited_at: Optional[datetime] = None


class AdminUserInviteOut(AdminUserOut):
    """Invite response: the created user plus a non-destructive email
    delivery outcome. The user is always created/invited even when the
    invitation email could not be delivered (partial success)."""
    user_created: bool = True
    invite_created: bool = True
    email_sent: bool = False
    email_status: Literal["accepted", "failed"] = "failed"
    safe_error_code: Optional[str] = None
    message: Optional[str] = None


class ResendInviteOut(BaseModel):
    ok: bool = True
    email_sent: bool = False
    email_status: Literal["accepted", "failed"] = "failed"
    safe_error_code: Optional[str] = None
    message: Optional[str] = None


class InviteUserRequest(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    role: Literal["analyst","supervisor","director","admin"]
    department: Optional[str] = None
    supervisor_user_id: Optional[int] = None
    entity_ids: List[str] = []


class UpdateUserRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[Literal["analyst","supervisor","director","admin"]] = None
    status: Optional[Literal["active","invited","disabled"]] = None
    department: Optional[str] = None
    supervisor_user_id: Optional[int] = None
    entity_ids: Optional[List[str]] = None


class BulkRoleRequest(BaseModel):
    user_ids: List[int]
    role: Literal["analyst","supervisor","director","admin"]


# ── Admin: RBAC ──────────────────────────────────────────────────────
class RoleOut(OrmBase):
    id: int
    key: str
    label: str
    description: Optional[str] = None


class PermissionOut(OrmBase):
    id: int
    key: str
    group: str
    label: str


class ModuleAccessUpdate(BaseModel):
    role_key: str
    modules: List[str]


class PermissionMatrixUpdate(BaseModel):
    role_key: str
    permission_keys: List[str]


# ── Admin: Entities ──────────────────────────────────────────────────
class AdminEntityCreate(BaseModel):
    id: str = Field(min_length=2, max_length=20)
    name: str
    region: Literal["North America","Europe","APAC"]
    supervisor_id: int
    period_type: Literal["YoY","MoM","QoQ","Custom"] = "YoY"
    period_from: str
    period_to: str
    currency: Optional[str] = None
    fiscal_year_end: Optional[str] = None
    tax_id: Optional[str] = None
    tags: List[str] = []


class AdminEntityUpdate(BaseModel):
    name: Optional[str] = None
    region: Optional[Literal["North America","Europe","APAC"]] = None
    supervisor_id: Optional[int] = None
    director_id: Optional[int] = None
    approval_level: Optional[Literal["supervisor","director"]] = None
    period_type: Optional[Literal["YoY","MoM","QoQ","Custom"]] = None
    period_from: Optional[str] = None
    period_to: Optional[str] = None
    currency: Optional[str] = None
    fiscal_year_end: Optional[str] = None
    tax_id: Optional[str] = None
    is_active: Optional[bool] = None
    tags: Optional[List[str]] = None


# ── Admin: Branding ──────────────────────────────────────────────────
class BrandingOut(OrmBase):
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    updated_at: Optional[datetime] = None


# ── Admin: Audit ─────────────────────────────────────────────────────
class AuditLogOut(OrmBase):
    id: int
    actor_user_id: int
    action: str
    table_name: str
    row_pk: Optional[str] = None
    before_json: Optional[Any] = None
    after_json: Optional[Any] = None
    ip_address: Optional[str] = None
    created_at: datetime
    actor: Optional[UserOut] = None


# ── Admin: ERP / Integrations ────────────────────────────────────────
class ErpCatalogOut(OrmBase):
    id: int
    key: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    auth_type: Optional[str] = None


class EntityErpMappingOut(OrmBase):
    id: int
    entity_id: str
    erp_key: str
    status: str
    last_sync_at: Optional[datetime] = None


class EntityErpMappingCreate(BaseModel):
    entity_id: str
    erp_key: str


class ApiCredentialOut(OrmBase):
    id: int
    entity_id: str
    layer: str
    item_name: Optional[str] = None
    endpoint: Optional[str] = None
    auth_type: Optional[str] = None
    last4: Optional[str] = None
    status: str
    last_tested_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None


class ApiCredentialUpsert(BaseModel):
    layer: Literal["gl","sub_gl","transaction"]
    item_name: Optional[str] = None
    endpoint: str
    auth_type: str = "api_key"
    secret: Optional[str] = None  # plain — server encrypts


# ── Admin: Ontology ──────────────────────────────────────────────────
class SubledgerPartyOptionOut(BaseModel):
    type: str
    code: str
    name: str = ""
    label: str

    model_config = ConfigDict(populate_by_name=True)


class SubledgerOptionsOut(BaseModel):
    customers: List[SubledgerPartyOptionOut] = Field(default_factory=list)
    vendors: List[SubledgerPartyOptionOut] = Field(default_factory=list)
    message: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class OntologyMappingOut(OrmBase):
    id: int
    entity_id: str
    statement: str
    tflux_caption: str
    erp_gl_json: List[str] = []
    erp_subgl_json: List[str] = []
    erp_subledger_json: List[Any] = Field(default_factory=list)
    custom_caption: Optional[str] = None
    is_hidden: bool = False
    display_column: str = "tflux"
    sort_order: int = 0
    version: int = 1

    @field_validator("erp_gl_json", "erp_subgl_json", "erp_subledger_json", mode="before")
    @classmethod
    def _coerce_null_json_lists(cls, v: Any) -> list:
        return [] if v is None else v


class OntologyMappingUpsert(BaseModel):
    statement: Literal["pl","bs"]
    tflux_caption: str
    erp_gl_json: List[str] = []
    erp_subgl_json: Optional[List[str]] = None
    erp_subledger_json: Optional[List[Any]] = None
    custom_caption: Optional[str] = None
    is_hidden: bool = False
    display_column: Literal["tflux","erp","custom"] = "tflux"


class OntologyDisplayColumnUpdate(BaseModel):
    display_column: Literal["tflux", "erp", "custom"]


class CustomCaptionCreate(BaseModel):
    statement: Literal["pl","bs"]
    name: str
    is_subtotal: bool = False
    erp_gl_json: List[str] = []
    erp_subgl_json: List[str] = []
    erp_subledger_json: Optional[List[Any]] = None


class CustomCaptionOut(OrmBase):
    id: int
    statement: str
    name: str
    is_subtotal: bool
    erp_gl_json: List[str] = []
    erp_subgl_json: List[str] = []
    erp_subledger_json: List[Any] = Field(default_factory=list)
    sort_order: int = 0

    @field_validator("erp_gl_json", "erp_subgl_json", "erp_subledger_json", mode="before")
    @classmethod
    def _coerce_null_json_lists(cls, v: Any) -> list:
        return [] if v is None else v


# ── Admin: Support ─────────────────────────────────────────────────────
class SupportChannelOut(BaseModel):
    id: str
    label: str
    value: str
    description: str
    color: str
    bg: str


class AdminSupportTicketCreate(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1, max_length=8000)
    priority: Literal["low", "medium", "high", "critical"] = "medium"

    @field_validator("subject", "description")
    @classmethod
    def strip_nonempty(cls, v: str) -> str:
        t = v.strip()
        if not t:
            raise ValueError("must not be empty")
        return t


class AdminSupportTicketOut(BaseModel):
    id: int
    reference: str
    message: str


class AdminAiChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str) -> str:
        t = v.strip()
        if not t:
            raise ValueError("must not be empty")
        return t


class AdminAiChatResponse(BaseModel):
    reply: str


# ── Notifications ─────────────────────────────────────────────────────
class NotificationOut(OrmBase):
    id: int
    type: str
    title: str
    message: str
    link_url: Optional[str] = None
    entity_id: Optional[str] = None
    is_read: bool
    read_at: Optional[datetime] = None
    created_at: datetime
    actor: Optional[UserOut] = None


class UnreadCountOut(BaseModel):
    count: int
