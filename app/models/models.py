# @file    models.py

from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, SmallInteger, Text, Boolean,
    Enum, Date, DateTime, Time, DECIMAL, JSON,
    ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship
from app.db.session import Base


# ── Users ─────────────────────────────────────────────────────────────
# Identity is captured by `first_name` + `last_name` (both required).
# `name` is exposed as a Python @property so existing serializers keep
# working without storing the denormalized full name in the database.
# `is_active` is derived from `status` (any value other than 'disabled').
class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    email           = Column(String(150), nullable=False, unique=True, index=True)
    # Hashed credential at rest; DB column is `password` (see schema.sql).
    password = Column(String(255), nullable=True)
    first_name      = Column(String(60),  nullable=False)
    last_name       = Column(String(60),  nullable=False)
    initials        = Column(String(5),   nullable=False)
    color           = Column(String(10),  nullable=False, default="#64748b")
    role            = Column(Enum("analyst", "supervisor", "director", "admin"),
                             nullable=False, default="analyst")
    department      = Column(String(100), nullable=True)
    supervisor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status          = Column(Enum("active", "invited", "disabled"),
                             nullable=False, default="invited")
    invited_at      = Column(DateTime, nullable=True)
    last_login_at   = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, nullable=False, default=datetime.utcnow)

    supervisor      = relationship("User", remote_side=[id], foreign_keys=[supervisor_user_id])

    __table_args__ = (
        UniqueConstraint("initials", name="uq_users_initials"),
        Index("ix_users_status", "status"),
    )

    @property
    def name(self) -> str:
        """Display name derived from first_name + last_name."""
        return f"{self.first_name or ''} {self.last_name or ''}".strip()

    @property
    def is_active(self) -> bool:
        """True for any user whose status is not 'disabled'."""
        return self.status != "disabled"


# ── Refresh Tokens ────────────────────────────────────────────────────
class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    jti         = Column(String(64), nullable=False, unique=True, index=True)
    expires_at  = Column(DateTime, nullable=False)
    revoked_at  = Column(DateTime, nullable=True)
    ip_address  = Column(String(45), nullable=True)
    user_agent  = Column(String(255), nullable=True)
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)


# ── Roles / Permissions / RBAC matrix ─────────────────────────────────
class Role(Base):
    __tablename__ = "roles"

    id    = Column(Integer, primary_key=True, autoincrement=True)
    key   = Column(String(20), nullable=False, unique=True)   # analyst|supervisor|director|admin
    label = Column(String(50), nullable=False)
    description = Column(String(255), nullable=True)


class Permission(Base):
    __tablename__ = "permissions"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    key         = Column(String(80), nullable=False, unique=True)
    group       = Column(String(40), nullable=False)
    label       = Column(String(120), nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id"),)

    id            = Column(Integer, primary_key=True, autoincrement=True)
    role_id       = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    permission_id = Column(Integer, ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False)


class ModuleAccess(Base):
    __tablename__ = "module_access"
    __table_args__ = (UniqueConstraint("role_id", "module_key"),)

    id         = Column(Integer, primary_key=True, autoincrement=True)
    role_id    = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    module_key = Column(String(40), nullable=False)


# ── Entities (extended with legal-entity fields) ──────────────────────
class Entity(Base):
    __tablename__ = "entities"

    id             = Column(String(20),  primary_key=True)  # also serves as `code`
    name           = Column(String(150), nullable=False)
    region         = Column(Enum("North America", "Europe", "APAC"), nullable=False)
    supervisor_id  = Column(Integer, ForeignKey("users.id"), nullable=False)
    status         = Column(Enum("assigned","wip","under_review","rejected","approved","pending_director"),
                            nullable=False, default="assigned")
    tags           = Column(JSON, nullable=False, default=list)
    period_type    = Column(Enum("YoY","MoM","QoQ","Custom"), nullable=False, default="YoY")
    period_from    = Column(String(20), nullable=False)
    period_to      = Column(String(20), nullable=False)
    sub_due_date   = Column(Date, nullable=True)
    rev_due_date   = Column(Date, nullable=True)
    approval_level = Column(Enum("supervisor","director"), nullable=False, default="supervisor")
    director_id    = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Legal-entity fields (merged from doc §17)
    currency         = Column(String(10),  nullable=True)
    fiscal_year_end  = Column(String(10),  nullable=True)  # 'MM-DD' or 'Dec 31'
    tax_id           = Column(String(50),  nullable=True)
    is_active        = Column(Boolean, nullable=False, default=True)

    updated_at     = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    supervisor       = relationship("User", foreign_keys=[supervisor_id])
    director         = relationship("User", foreign_keys=[director_id])
    assignments      = relationship("EntityAssignment", back_populates="entity", cascade="all, delete")
    profile          = relationship("CompanyProfile",   back_populates="entity", uselist=False, cascade="all, delete")
    thresholds       = relationship("Threshold",        back_populates="entity", cascade="all, delete")
    flux_data        = relationship("FluxData",         back_populates="entity", cascade="all, delete")
    ai_commentary    = relationship("AiCommentary",     back_populates="entity", cascade="all, delete")
    caption_versions = relationship("CaptionVersion",   back_populates="entity", cascade="all, delete")
    comments         = relationship("Comment",          back_populates="entity", cascade="all, delete")
    signoffs         = relationship("Signoff",          back_populates="entity", cascade="all, delete")
    macro_factors    = relationship("MacroFactor",      back_populates="entity", cascade="all, delete")
    tasks            = relationship("Task",             back_populates="entity", cascade="all, delete")
    chat_messages    = relationship("ChatMessage",      back_populates="entity", cascade="all, delete")


# ── User <-> Entity access (analyst scoping) ──────────────────────────
class UserEntityAccess(Base):
    __tablename__ = "user_entity_access"
    __table_args__ = (UniqueConstraint("user_id", "entity_id", name="uq_user_entity"),)

    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_id  = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True)
    role_scope = Column(Enum("read","write","supervise"), nullable=False, default="write")
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    granted_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# ── Entity Assignments (caption-level) ────────────────────────────────
class EntityAssignment(Base):
    __tablename__ = "entity_assignments"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    entity_id   = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    caption     = Column(String(100), nullable=False)
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    start_date  = Column(Date, nullable=True)
    end_date    = Column(Date, nullable=True)
    is_active   = Column(Boolean, nullable=False, default=True)

    entity   = relationship("Entity", back_populates="assignments")
    assignee = relationship("User", foreign_keys=[assignee_id])


# ── Company Profile ───────────────────────────────────────────────────
class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    entity_id     = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, unique=True)
    industry      = Column(String(150), nullable=False, default="")
    geography     = Column(String(100), nullable=False, default="")
    revenue_range = Column(String(50),  nullable=False, default="")
    entity_type   = Column(String(50),  nullable=False, default="")
    brief         = Column(Text, nullable=True)

    entity = relationship("Entity", back_populates="profile")


# ── Thresholds ────────────────────────────────────────────────────────
class Threshold(Base):
    __tablename__ = "thresholds"
    __table_args__ = (UniqueConstraint("entity_id", "statement"),)

    id        = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    statement = Column(Enum("pl", "bs"), nullable=False)
    mode      = Column(Enum("pct", "abs", "both"), nullable=False, default="both")
    pct_value = Column(DECIMAL(5, 2),  nullable=False, default=10.00)
    abs_value = Column(DECIMAL(15, 2), nullable=False, default=100000.00)
    logic     = Column(Enum("and", "or"), nullable=False, default="or")
    apply_all = Column(Boolean, nullable=False, default=True)

    entity = relationship("Entity", back_populates="thresholds")


# ── Flux Data ─────────────────────────────────────────────────────────
class FluxData(Base):
    __tablename__ = "flux_data"
    __table_args__ = (UniqueConstraint("entity_id", "statement", "period_from", "period_to", "caption"),)

    id            = Column(Integer, primary_key=True, autoincrement=True)
    entity_id     = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    statement     = Column(Enum("pl", "bs"), nullable=False)
    period_from   = Column(String(20), nullable=False)
    period_to     = Column(String(20), nullable=False)
    caption       = Column(String(100), nullable=False)
    current_value = Column(DECIMAL(15, 2), nullable=False, default=0)
    comp_value    = Column(DECIMAL(15, 2), nullable=False, default=0)
    pct_variance  = Column(DECIMAL(8, 2),  nullable=True)
    is_material   = Column(Boolean, nullable=False, default=False)
    is_bold       = Column(Boolean, nullable=False, default=False)
    sort_order    = Column(SmallInteger, nullable=False, default=0)

    # Provenance: ``seed`` (demo data), ``business_central`` (generated from
    # synced GL entries via ontology mapping), ``manual`` (admin override).
    # NULL = legacy rows, treated as seed by `detect_seed_flux_rows`.
    source_type     = Column(String(40), nullable=True)
    generated_from  = Column(String(40), nullable=True)   # e.g. "ontology+bc"
    generated_at    = Column(DateTime,    nullable=True)

    entity = relationship("Entity", back_populates="flux_data")


# ── AI Commentary ─────────────────────────────────────────────────────
class AiCommentary(Base):
    __tablename__ = "ai_commentary"
    __table_args__ = (UniqueConstraint("entity_id", "caption"),)

    id            = Column(Integer, primary_key=True, autoincrement=True)
    entity_id     = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    caption       = Column(String(100), nullable=False)
    concise_text  = Column(Text, nullable=False)
    detailed_text = Column(Text, nullable=False)
    edited_text   = Column(Text, nullable=True)
    edited_by     = Column(Integer, ForeignKey("users.id"), nullable=True)
    edited_at     = Column(DateTime, nullable=True)
    ai_score      = Column(Integer, nullable=False, default=80)
    updated_at    = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    entity    = relationship("Entity", back_populates="ai_commentary")
    editor    = relationship("User",   foreign_keys=[edited_by])


# ── Caption Versions ──────────────────────────────────────────────────
class CaptionVersion(Base):
    __tablename__ = "caption_versions"
    __table_args__ = (UniqueConstraint("entity_id", "caption"),)

    id         = Column(Integer, primary_key=True, autoincrement=True)
    entity_id  = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    caption    = Column(String(100), nullable=False)
    version    = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    entity = relationship("Entity", back_populates="caption_versions")


# ── Comments ──────────────────────────────────────────────────────────
class Comment(Base):
    __tablename__ = "comments"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    entity_id   = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    caption     = Column(String(100), nullable=False)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    text        = Column(Text, nullable=False)
    parent_id   = Column(Integer, ForeignKey("comments.id"), nullable=True)
    is_resolved = Column(Boolean, nullable=False, default=False)
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)

    entity   = relationship("Entity", back_populates="comments")
    user     = relationship("User",   foreign_keys=[user_id])
    resolver = relationship("User",   foreign_keys=[resolved_by])
    replies  = relationship("Comment", back_populates="parent", foreign_keys=[parent_id])
    parent   = relationship("Comment", back_populates="replies", remote_side="Comment.id")


# ── Sign-Offs ─────────────────────────────────────────────────────────
class Signoff(Base):
    __tablename__ = "signoffs"
    __table_args__ = (UniqueConstraint("entity_id", "caption"),)

    id        = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    caption   = Column(String(100), nullable=False)
    signed_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    signed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    version   = Column(Integer, nullable=False, default=0)
    is_stale  = Column(Boolean, nullable=False, default=False)

    entity = relationship("Entity", back_populates="signoffs")
    signer = relationship("User",   foreign_keys=[signed_by])


# ── Macro Factors ─────────────────────────────────────────────────────
class MacroFactor(Base):
    __tablename__ = "macro_factors"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    entity_id  = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    category   = Column(String(50), nullable=False)
    icon       = Column(String(10), nullable=False, default="📌")
    text       = Column(Text, nullable=False)
    is_custom  = Column(Boolean, nullable=False, default=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    entity  = relationship("Entity", back_populates="macro_factors")
    creator = relationship("User",   foreign_keys=[created_by])


# ── Tasks ─────────────────────────────────────────────────────────────
class Task(Base):
    __tablename__ = "tasks"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    entity_id    = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    title        = Column(String(200), nullable=False)
    description  = Column(Text, nullable=True)
    assignee_id  = Column(Integer, ForeignKey("users.id"), nullable=False)
    due_date     = Column(Date, nullable=False)
    status       = Column(Enum("todo","in_progress","pending","done"), nullable=False, default="todo")
    priority     = Column(Enum("low","medium","high","critical"),      nullable=False, default="medium")
    phase        = Column(Enum("preparation","review","submission","approval"), nullable=False, default="preparation")
    is_recurring = Column(Boolean, nullable=False, default=False)
    created_at   = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at   = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    entity     = relationship("Entity", back_populates="tasks")
    assignee   = relationship("User",   foreign_keys=[assignee_id])
    reminder   = relationship("TaskReminder",   back_populates="task", uselist=False, cascade="all, delete")
    recurrence = relationship("TaskRecurrence", back_populates="task", uselist=False, cascade="all, delete")


class TaskReminder(Base):
    __tablename__ = "task_reminders"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    task_id     = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    days_before = Column(Integer, nullable=False, default=1)
    remind_time = Column(Time,    nullable=False)
    email       = Column(String(150), nullable=False)
    is_sent     = Column(Boolean,  nullable=False, default=False)
    sent_at     = Column(DateTime, nullable=True)

    task = relationship("Task", back_populates="reminder")


class TaskRecurrence(Base):
    __tablename__ = "task_recurrence"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    task_id      = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True)
    frequency    = Column(Enum("weekly","monthly","quarterly"), nullable=False, default="monthly")
    day_of_month = Column(Integer, nullable=True)

    task = relationship("Task", back_populates="recurrence")


# ── Personal Reminders ────────────────────────────────────────────────
class PersonalReminder(Base):
    __tablename__ = "personal_reminders"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    text        = Column(Text, nullable=False)
    due_date    = Column(Date, nullable=False)
    remind_time = Column(Time, nullable=False)
    is_done     = Column(Boolean, nullable=False, default=False)
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


# ── Chat Messages ─────────────────────────────────────────────────────
class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    entity_id    = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    user_id      = Column(Integer,   ForeignKey("users.id"), nullable=False)
    text         = Column(Text, nullable=False)
    is_flagged   = Column(Boolean, nullable=False, default=False)
    forwarded_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at   = Column(DateTime, nullable=False, default=datetime.utcnow)

    entity   = relationship("Entity", back_populates="chat_messages")
    user     = relationship("User",   foreign_keys=[user_id])
    fwd_user = relationship("User",   foreign_keys=[forwarded_to])
    replies  = relationship("ChatReply", back_populates="message", cascade="all, delete")


class ChatReply(Base):
    __tablename__ = "chat_replies"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    text       = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    message = relationship("ChatMessage", back_populates="replies")
    user    = relationship("User", foreign_keys=[user_id])


# ── Delegation Log ────────────────────────────────────────────────────
class DelegationLog(Base):
    __tablename__ = "delegation_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    entity_id    = Column(String(20), ForeignKey("entities.id"), nullable=False)
    from_user_id = Column(Integer,   ForeignKey("users.id"), nullable=False)
    to_user_id   = Column(Integer,   ForeignKey("users.id"), nullable=False)
    note         = Column(Text, nullable=True)
    created_at   = Column(DateTime, nullable=False, default=datetime.utcnow)

    from_user = relationship("User", foreign_keys=[from_user_id])
    to_user   = relationship("User", foreign_keys=[to_user_id])


# ── Export Log ────────────────────────────────────────────────────────
class ExportLog(Base):
    __tablename__ = "export_log"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    entity_id   = Column(String(20), ForeignKey("entities.id"), nullable=False)
    exported_by = Column(Integer,    ForeignKey("users.id"), nullable=False)
    mode        = Column(Enum("concise", "detailed"), nullable=False, default="concise")
    statement   = Column(Enum("pl", "bs", "both"),    nullable=False, default="both")
    file_path   = Column(String(500), nullable=True)
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)

    exporter = relationship("User", foreign_keys=[exported_by])


# ── Audit Log ─────────────────────────────────────────────────────────
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    action        = Column(String(60), nullable=False, index=True)
    table_name    = Column(String(60), nullable=False, index=True)
    row_pk        = Column(String(80), nullable=True)
    before_json   = Column(JSON, nullable=True)
    after_json    = Column(JSON, nullable=True)
    ip_address    = Column(String(45), nullable=True)
    created_at    = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    actor = relationship("User", foreign_keys=[actor_user_id])


# ── Branding ──────────────────────────────────────────────────────────
class BrandingSettings(Base):
    __tablename__ = "branding_settings"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    logo_url      = Column(String(500), nullable=True)
    primary_color = Column(String(10),  nullable=True)
    updated_at    = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by    = Column(Integer, ForeignKey("users.id"), nullable=True)


# ── ERP Integration ───────────────────────────────────────────────────
class ErpCatalog(Base):
    __tablename__ = "erp_catalog"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    key         = Column(String(40), nullable=False, unique=True)
    name        = Column(String(100), nullable=False)
    description = Column(String(255), nullable=True)
    icon        = Column(String(10), nullable=True)
    color       = Column(String(20), nullable=True)
    auth_type   = Column(String(40), nullable=True)


class EntityErpMapping(Base):
    __tablename__ = "entity_erp_mappings"
    __table_args__ = (UniqueConstraint("entity_id", "erp_key", name="uq_entity_erp"),)

    id            = Column(Integer, primary_key=True, autoincrement=True)
    entity_id     = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    erp_key       = Column(String(40), nullable=False)
    status        = Column(Enum("disconnected","connecting","connected","error"),
                           nullable=False, default="disconnected")
    last_sync_at  = Column(DateTime, nullable=True)
    connected_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at    = Column(DateTime, nullable=False, default=datetime.utcnow)


class ApiCredential(Base):
    __tablename__ = "api_credentials"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    entity_id         = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True)
    layer             = Column(Enum("gl","sub_gl","transaction"), nullable=False)
    item_name         = Column(String(100), nullable=True)   # null for layer=gl, filled for sub_gl/transaction
    endpoint          = Column(String(500), nullable=True)
    auth_type         = Column(String(40), nullable=True)
    encrypted_secret  = Column(Text, nullable=True)
    last4             = Column(String(8),  nullable=True)
    status            = Column(Enum("missing","unverified","verified","error"), nullable=False, default="missing")
    last_tested_at    = Column(DateTime, nullable=True)
    last_synced_at    = Column(DateTime, nullable=True)


class ApiSyncRun(Base):
    __tablename__ = "api_sync_runs"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    credential_id = Column(Integer, ForeignKey("api_credentials.id", ondelete="CASCADE"), nullable=False)
    started_at    = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at   = Column(DateTime, nullable=True)
    ok            = Column(Boolean,  nullable=False, default=False)
    safe_error    = Column(String(500), nullable=True)
    rows_synced   = Column(Integer, nullable=True)


# ── Ontology ──────────────────────────────────────────────────────────
class OntologyMapping(Base):
    __tablename__ = "ontology_mappings"
    __table_args__ = (UniqueConstraint("entity_id", "statement", "tflux_caption"),)

    id              = Column(Integer, primary_key=True, autoincrement=True)
    entity_id       = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    statement       = Column(Enum("pl","bs"), nullable=False)
    tflux_caption   = Column(String(100), nullable=False)
    erp_gl_json        = Column(JSON, nullable=False, default=list)
    erp_subgl_json     = Column(JSON, nullable=False, default=list)
    erp_subledger_json = Column(JSON, nullable=False, default=list)
    custom_caption     = Column(String(150), nullable=True)
    is_hidden       = Column(Boolean, nullable=False, default=False)
    display_column  = Column(Enum("tflux","erp","custom"), nullable=False, default="tflux")
    sort_order      = Column(SmallInteger, nullable=False, default=0)
    version         = Column(Integer, nullable=False, default=1)
    updated_by      = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at      = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CustomCaption(Base):
    __tablename__ = "custom_captions"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    entity_id       = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    statement       = Column(Enum("pl","bs"), nullable=False)
    name            = Column(String(150), nullable=False)
    is_subtotal     = Column(Boolean, nullable=False, default=False)
    erp_gl_json        = Column(JSON, nullable=False, default=list)
    erp_subgl_json     = Column(JSON, nullable=False, default=list)
    erp_subledger_json = Column(JSON, nullable=False, default=list)
    sort_order         = Column(SmallInteger, nullable=False, default=0)


# ── Business Central / normalized ERP ─────────────────────────────────
# All Business Central (and any future real-connector) credential and sync
# state lives in these dedicated tables so the legacy generic ApiCredential
# / ApiSyncRun flow used by SAP/Oracle/NetSuite/Dynamics365/Workday remains
# untouched and continues to work as before.

# Status lifecycle exposed to the Admin Console for BC credentials.
BC_CREDENTIAL_STATUSES = (
    "not_configured",
    "saved",
    "testing",
    "verified",
    "failed",
    "syncing",
    "synced",
)

# Centralized sync-type identifiers. Keep these as Python string constants
# (not a SQL ENUM) so adding a new sync type only ever needs a code change —
# the DB column is a plain VARCHAR(64) and accepts any new value without
# requiring a destructive ALTER on production. The `ERP_SYNC_TYPES` tuple
# remains the documented allow-list checked at the application layer.
SYNC_TEST_CONNECTION = "test_connection"
SYNC_FETCH_COMPANIES = "fetch_companies"
SYNC_GL_ACCOUNTS     = "sync_gl_accounts"
SYNC_DIMENSIONS      = "sync_dimensions"
SYNC_GL_ENTRIES              = "sync_gl_entries"
SYNC_CUSTOMERS               = "sync_customers"
SYNC_VENDORS                 = "sync_vendors"
SYNC_CUSTOMER_LEDGER_ENTRIES = "sync_customer_ledger_entries"
SYNC_VENDOR_LEDGER_ENTRIES   = "sync_vendor_ledger_entries"
# Alias preserved for callers that prefer the spelled-out spec name.
# The on-disk value remains `sync_gl_entries` to keep historical rows valid.
SYNC_GENERAL_LEDGER_ENTRIES = SYNC_GL_ENTRIES
# Aggregate row written by the BC auto-sync orchestrator — one per entity
# per scheduled tick — so the admin diagnostics endpoint can summarise the
# whole pipeline run with a single query.
SYNC_AUTO_BC_SYNC          = "auto_bc_sync"
# Marker row written when the orchestrator triggers Generate-Flux-from-BC
# after the three core layers succeed.
SYNC_GENERATE_FLUX_FROM_BC = "generate_flux_from_bc"

ERP_SYNC_TYPES = (
    SYNC_TEST_CONNECTION,
    SYNC_FETCH_COMPANIES,
    SYNC_GL_ACCOUNTS,
    SYNC_DIMENSIONS,
    SYNC_GL_ENTRIES,
    SYNC_CUSTOMERS,
    SYNC_VENDORS,
    SYNC_CUSTOMER_LEDGER_ENTRIES,
    SYNC_VENDOR_LEDGER_ENTRIES,
    SYNC_AUTO_BC_SYNC,
    SYNC_GENERATE_FLUX_FROM_BC,
)

ERP_SYNC_STATUSES = ("running", "success", "failed")
ERP_SYNC_TYPE_COLUMN_LENGTH = 64


class BusinessCentralCredential(Base):
    """Per-entity Business Central app-registration credentials.

    Sensitive fields are stored as Fernet ciphertext only; plaintext never
    leaves the service layer. `last4_secret` / `last4_client` exist purely so
    the Admin UI can render a masked summary without touching ciphertext.
    """
    __tablename__ = "bc_credentials"
    __table_args__ = (UniqueConstraint("entity_id", name="uq_bc_credentials_entity"),)

    id                       = Column(Integer, primary_key=True, autoincrement=True)
    entity_id                = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                                      nullable=False, index=True)
    erp_type                 = Column(String(40), nullable=False, default="bc")
    auth_type                = Column(String(40), nullable=False, default="oauth2_client_credentials")

    tenant_id_encrypted      = Column(Text, nullable=False)
    client_id_encrypted      = Column(Text, nullable=False)
    client_secret_encrypted  = Column(Text, nullable=False)

    environment_name         = Column(String(100), nullable=False)
    scope                    = Column(String(255), nullable=False)
    base_url                 = Column(String(255), nullable=False)

    company_id               = Column(String(100), nullable=True)
    company_name             = Column(String(150), nullable=True)

    last4_client             = Column(String(8), nullable=True)
    last4_secret             = Column(String(8), nullable=True)

    status                   = Column(Enum(*BC_CREDENTIAL_STATUSES, name="bc_credential_status"),
                                      nullable=False, default="not_configured")
    last_safe_error_code     = Column(String(80), nullable=True)
    last_safe_error_message  = Column(String(500), nullable=True)

    last_tested_at           = Column(DateTime, nullable=True)
    last_verified_at         = Column(DateTime, nullable=True)

    created_by               = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by               = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at               = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at               = Column(DateTime, nullable=False, default=datetime.utcnow,
                                      onupdate=datetime.utcnow)


class ErpSyncRun(Base):
    """Normalized per-entity sync history (test_connection / fetch_companies /
    sync_gl_accounts / sync_gl_entries). Independent of the legacy
    api_sync_runs table which keeps mock-flow rows for the other ERPs."""
    __tablename__ = "erp_sync_runs"
    __table_args__ = (
        Index("ix_erp_sync_runs_entity_started", "entity_id", "started_at"),
    )

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    entity_id           = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    erp_type            = Column(String(40), nullable=False, default="bc")
    # VARCHAR (not ENUM) so adding new sync types is a code-only change.
    # Application code restricts values to ERP_SYNC_TYPES; the DB merely stores
    # the resulting text, keeping migrations simple and forward-compatible.
    sync_type           = Column(String(ERP_SYNC_TYPE_COLUMN_LENGTH), nullable=False)
    status              = Column(Enum(*ERP_SYNC_STATUSES, name="erp_sync_status"),
                                 nullable=False, default="running")
    started_at          = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at         = Column(DateTime, nullable=True)
    safe_error_code     = Column(String(80), nullable=True)
    safe_error_message  = Column(String(500), nullable=True)
    records_processed   = Column(Integer, nullable=True)
    created_by          = Column(Integer, ForeignKey("users.id"), nullable=True)


class ErpCompany(Base):
    """Companies fetched from Business Central (or any ERP) for an entity."""
    __tablename__ = "erp_companies"
    __table_args__ = (
        UniqueConstraint("entity_id", "erp_type", "company_id",
                         name="uq_erp_companies_entity_erp_company"),
    )

    id           = Column(Integer, primary_key=True, autoincrement=True)
    entity_id    = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    erp_type     = Column(String(40), nullable=False, default="bc")
    company_id   = Column(String(100), nullable=False)
    company_name = Column(String(200), nullable=False)
    updated_at   = Column(DateTime, nullable=False, default=datetime.utcnow,
                          onupdate=datetime.utcnow)


class ErpGlAccount(Base):
    """Normalized GL chart pulled from the ERP for a given (entity, company).

    The legacy `account_no` / `account_name` / `account_category` columns are
    kept as the canonical fields. The extra optional columns below carry the
    fuller normalized shape requested by the ontology / BC integration spec
    (external_id, display_name, sub_category, account_type, blocked,
    raw_source_json, last_synced_at). All new columns are nullable so adding
    them is a non-destructive ALTER on existing databases.
    """
    __tablename__ = "erp_gl_accounts"
    __table_args__ = (
        UniqueConstraint("entity_id", "erp_type", "company_id", "account_no",
                         name="uq_erp_gl_accounts_unique"),
    )

    id               = Column(Integer, primary_key=True, autoincrement=True)
    entity_id        = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    erp_type         = Column(String(40), nullable=False, default="bc")
    company_id       = Column(String(100), nullable=False)
    account_no       = Column(String(80),  nullable=False)
    account_name     = Column(String(255), nullable=True)
    account_category = Column(String(80),  nullable=True)

    external_id      = Column(String(100), nullable=True)
    display_name     = Column(String(255), nullable=True)
    sub_category     = Column(String(80),  nullable=True)
    account_type     = Column(String(80),  nullable=True)
    blocked          = Column(Boolean,     nullable=True)
    raw_source_json  = Column(JSON,        nullable=True)
    last_synced_at   = Column(DateTime,    nullable=True)

    updated_at       = Column(DateTime, nullable=False, default=datetime.utcnow,
                              onupdate=datetime.utcnow)


class ErpDimensionValue(Base):
    """Normalized Sub-GL data for an ERP — for Business Central this is the
    flattened dimension/dimension-value table (Department, Region, etc.).

    Each row represents one dimension VALUE; `dimension_*` columns describe the
    parent dimension and `value_*` columns describe the value itself. The shape
    deliberately mirrors `ErpGlAccount` so the ontology UI can treat both as
    selectable tags."""
    __tablename__ = "erp_dimension_values"
    __table_args__ = (
        UniqueConstraint("entity_id", "erp_type", "company_id",
                         "dimension_code", "value_code",
                         name="uq_erp_dim_values_unique"),
        Index("ix_erp_dim_values_entity_company", "entity_id", "company_id"),
    )

    id              = Column(Integer, primary_key=True, autoincrement=True)
    entity_id       = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    erp_type        = Column(String(40), nullable=False, default="bc")
    company_id      = Column(String(100), nullable=False)

    dimension_id    = Column(String(100), nullable=True)
    dimension_code  = Column(String(80),  nullable=False)
    dimension_name  = Column(String(255), nullable=True)

    value_id        = Column(String(100), nullable=True)
    value_code      = Column(String(80),  nullable=False)
    value_name      = Column(String(255), nullable=True)

    display_name    = Column(String(255), nullable=True)
    raw_source_json = Column(JSON,        nullable=True)
    last_synced_at  = Column(DateTime,    nullable=True)

    updated_at      = Column(DateTime, nullable=False, default=datetime.utcnow,
                             onupdate=datetime.utcnow)


class ErpGeneralLedgerEntry(Base):
    """Normalized GL entries pulled from the ERP.

    The new optional columns (external_id, entry_number, account_id,
    debit_amount, credit_amount, dimension_set_id, raw_source_json,
    last_synced_at) capture the wider shape needed for analyst drill-down
    while the original columns remain the canonical legacy fields."""
    __tablename__ = "erp_general_ledger_entries"
    __table_args__ = (
        UniqueConstraint("entity_id", "erp_type", "company_id", "external_entry_no",
                         name="uq_erp_gl_entries_unique"),
        Index("ix_erp_gl_entries_post_date", "entity_id", "company_id", "posting_date"),
    )

    id                = Column(Integer, primary_key=True, autoincrement=True)
    entity_id         = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                               nullable=False, index=True)
    erp_type          = Column(String(40), nullable=False, default="bc")
    company_id        = Column(String(100), nullable=False)
    external_entry_no = Column(String(80),  nullable=False)
    posting_date      = Column(Date,        nullable=True)
    document_no       = Column(String(100), nullable=True)
    account_no        = Column(String(80),  nullable=True)
    amount            = Column(DECIMAL(20, 2), nullable=True)
    description       = Column(String(255), nullable=True)

    external_id       = Column(String(100), nullable=True)
    entry_number      = Column(String(80),  nullable=True)
    account_id        = Column(String(100), nullable=True)
    debit_amount      = Column(DECIMAL(20, 2), nullable=True)
    credit_amount     = Column(DECIMAL(20, 2), nullable=True)
    dimension_set_id  = Column(String(100), nullable=True)
    source_type       = Column(String(80),  nullable=True)
    source_number     = Column(String(80),  nullable=True)
    bal_account_type  = Column(String(120), nullable=True)
    bal_account_number = Column(String(80),  nullable=True)
    external_document_number = Column(String(100), nullable=True)
    raw_source_json   = Column(JSON,        nullable=True)
    last_synced_at    = Column(DateTime,    nullable=True)

    updated_at        = Column(DateTime, nullable=False, default=datetime.utcnow,
                               onupdate=datetime.utcnow)


class ErpCustomer(Base):
    """Customer master synced from Business Central /customers."""
    __tablename__ = "erp_customers"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "erp_type", "company_id", "customer_no",
            name="uq_erp_customers_unique",
        ),
        Index("ix_erp_customers_entity_company", "entity_id", "company_id"),
    )

    id              = Column(Integer, primary_key=True, autoincrement=True)
    entity_id       = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    erp_type        = Column(String(40), nullable=False, default="bc")
    company_id      = Column(String(100), nullable=False)
    customer_no     = Column(String(80), nullable=False)
    display_name    = Column(String(255), nullable=True)
    external_id     = Column(String(100), nullable=True)
    raw_source_json = Column(JSON, nullable=True)
    last_synced_at  = Column(DateTime, nullable=True)
    updated_at      = Column(DateTime, nullable=False, default=datetime.utcnow,
                             onupdate=datetime.utcnow)


class ErpVendor(Base):
    """Vendor master synced from Business Central /vendors."""
    __tablename__ = "erp_vendors"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "erp_type", "company_id", "vendor_no",
            name="uq_erp_vendors_unique",
        ),
        Index("ix_erp_vendors_entity_company", "entity_id", "company_id"),
    )

    id              = Column(Integer, primary_key=True, autoincrement=True)
    entity_id       = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    erp_type        = Column(String(40), nullable=False, default="bc")
    company_id      = Column(String(100), nullable=False)
    vendor_no       = Column(String(80), nullable=False)
    display_name    = Column(String(255), nullable=True)
    external_id     = Column(String(100), nullable=True)
    raw_source_json = Column(JSON, nullable=True)
    last_synced_at  = Column(DateTime, nullable=True)
    updated_at      = Column(DateTime, nullable=False, default=datetime.utcnow,
                             onupdate=datetime.utcnow)


class ErpCustomerLedgerEntry(Base):
    """Customer ledger entries synced from Business Central."""
    __tablename__ = "erp_customer_ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "erp_type", "company_id", "external_entry_no",
            name="uq_erp_customer_ledger_unique",
        ),
        Index("ix_erp_customer_ledger_post", "entity_id", "company_id", "posting_date"),
    )

    id               = Column(Integer, primary_key=True, autoincrement=True)
    entity_id        = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    erp_type         = Column(String(40), nullable=False, default="bc")
    company_id       = Column(String(100), nullable=False)
    customer_no      = Column(String(80), nullable=False)
    customer_name    = Column(String(255), nullable=True)
    document_no      = Column(String(100), nullable=True)
    posting_date     = Column(Date, nullable=True)
    amount           = Column(DECIMAL(20, 2), nullable=True)
    remaining_amount = Column(DECIMAL(20, 2), nullable=True)
    external_entry_no = Column(String(80), nullable=False)
    raw_source_json  = Column(JSON, nullable=True)
    last_synced_at   = Column(DateTime, nullable=True)
    updated_at       = Column(DateTime, nullable=False, default=datetime.utcnow,
                              onupdate=datetime.utcnow)


class ErpVendorLedgerEntry(Base):
    """Vendor ledger entries synced from Business Central."""
    __tablename__ = "erp_vendor_ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "erp_type", "company_id", "external_entry_no",
            name="uq_erp_vendor_ledger_unique",
        ),
        Index("ix_erp_vendor_ledger_post", "entity_id", "company_id", "posting_date"),
    )

    id               = Column(Integer, primary_key=True, autoincrement=True)
    entity_id        = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    erp_type         = Column(String(40), nullable=False, default="bc")
    company_id       = Column(String(100), nullable=False)
    vendor_no        = Column(String(80), nullable=False)
    vendor_name      = Column(String(255), nullable=True)
    document_no      = Column(String(100), nullable=True)
    posting_date     = Column(Date, nullable=True)
    amount           = Column(DECIMAL(20, 2), nullable=True)
    remaining_amount = Column(DECIMAL(20, 2), nullable=True)
    external_entry_no = Column(String(80), nullable=False)
    raw_source_json  = Column(JSON, nullable=True)
    last_synced_at   = Column(DateTime, nullable=True)
    updated_at       = Column(DateTime, nullable=False, default=datetime.utcnow,
                              onupdate=datetime.utcnow)


# QuickBooks Online credential lifecycle (OAuth 2.0 authorization code).
QB_CREDENTIAL_STATUSES = BC_CREDENTIAL_STATUSES

ERP_TYPE_QUICKBOOKS = "quickbooks"


class QuickBooksCredential(Base):
    """Per-entity QuickBooks Online app registration and OAuth tokens.

    Intuit ``client_id`` / ``client_secret`` and OAuth tokens are stored as
    Fernet ciphertext. Plaintext never leaves the service layer. ``last4_secret``
    supports masked UI without decrypting.
    """
    __tablename__ = "quickbooks_credentials"
    __table_args__ = (UniqueConstraint("entity_id", name="uq_qb_credentials_entity"),)

    id                       = Column(Integer, primary_key=True, autoincrement=True)
    entity_id                = Column(String(20), ForeignKey("entities.id", ondelete="CASCADE"),
                                      nullable=False, index=True)
    erp_type                 = Column(String(40), nullable=False, default=ERP_TYPE_QUICKBOOKS)
    auth_type                = Column(String(40), nullable=False, default="oauth2_authorization_code")

    client_id_encrypted      = Column(Text, nullable=True)
    client_secret_encrypted  = Column(Text, nullable=True)
    redirect_uri             = Column(String(512), nullable=True)
    scopes                   = Column(String(512), nullable=True)
    auth_url                 = Column(String(512), nullable=True)
    token_url                = Column(String(512), nullable=True)
    api_base_sandbox         = Column(String(512), nullable=True)
    api_base_production      = Column(String(512), nullable=True)
    last4_client             = Column(String(8), nullable=True)
    last4_secret             = Column(String(8), nullable=True)

    access_token_encrypted   = Column(Text, nullable=True)
    refresh_token_encrypted  = Column(Text, nullable=True)
    token_expiry             = Column(DateTime, nullable=True)

    realm_id                 = Column(String(100), nullable=True)
    company_name             = Column(String(255), nullable=True)
    environment              = Column(String(40), nullable=False, default="sandbox")

    oauth_state              = Column(String(128), nullable=True)
    oauth_state_expires_at   = Column(DateTime, nullable=True)

    status                   = Column(Enum(*QB_CREDENTIAL_STATUSES, name="qb_credential_status"),
                                      nullable=False, default="not_configured")
    last_safe_error_code     = Column(String(80), nullable=True)
    last_safe_error_message  = Column(String(500), nullable=True)

    last_tested_at           = Column(DateTime, nullable=True)
    last_verified_at         = Column(DateTime, nullable=True)

    created_by               = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by               = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at               = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at               = Column(DateTime, nullable=False, default=datetime.utcnow,
                                      onupdate=datetime.utcnow)


# ── Notifications ─────────────────────────────────────────────────────
class Notification(Base):
    __tablename__ = "notifications"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    recipient_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                               nullable=False, index=True)
    actor_user_id     = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                               nullable=True)
    entity_id         = Column(String(20), nullable=True, index=True)
    type              = Column(String(60), nullable=False, index=True)
    title             = Column(String(200), nullable=False)
    message           = Column(Text, nullable=False)
    link_url          = Column(String(500), nullable=True)
    metadata_json     = Column(JSON, nullable=True)
    is_read           = Column(Boolean, nullable=False, default=False, index=True)
    read_at           = Column(DateTime, nullable=True)
    created_at        = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    recipient = relationship("User", foreign_keys=[recipient_user_id])
    actor     = relationship("User", foreign_keys=[actor_user_id])
