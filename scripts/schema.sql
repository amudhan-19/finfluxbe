# @file    scripts/schema.sql
#
# FinFlux — MySQL database schema aligned with SQLAlchemy models in
# `app/models/models.py` (as of May 2026). Use for fresh installs or
# documentation; production may also rely on `Base.metadata.create_all` plus
# `app.db.migrations_runtime.align_business_central_schema`.
#
# Rule: created_at / updated_at only where workflow or audit needs them.

-- ============================================================
-- FinFlux — MySQL Database Schema
-- @version 2.0.0 | May 2026 — aligned with ORM models
-- ============================================================

CREATE DATABASE IF NOT EXISTS finflux CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE finflux;

-- ============================================================
-- 1. USERS — auth + profile (ORM: User)
-- ============================================================
-- NOTE: `name` removed — derive at the application layer from
-- `first_name + last_name`. `is_active` removed — derivable from `status`
-- (status='disabled' ⇒ inactive). first_name/last_name are now the
-- canonical, required identity fields. Column `password` holds the bcrypt
-- hash (never plaintext).
CREATE TABLE IF NOT EXISTS users (
    id                  INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    email               VARCHAR(150)    NOT NULL,
    `password`          VARCHAR(255)    NULL,
    first_name          VARCHAR(60)     NOT NULL,
    last_name           VARCHAR(60)     NOT NULL,
    initials            VARCHAR(5)      NOT NULL,
    color               VARCHAR(10)     NOT NULL DEFAULT '#64748b',
    role                ENUM('analyst','supervisor','director','admin') NOT NULL DEFAULT 'analyst',
    department          VARCHAR(100)    NULL,
    supervisor_user_id  INT UNSIGNED    NULL,
    status              ENUM('active','invited','disabled') NOT NULL DEFAULT 'invited',
    invited_at          DATETIME        NULL,
    last_login_at       DATETIME        NULL,
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_email     (email),
    UNIQUE KEY uq_users_initials  (initials),
    KEY        idx_users_role     (role),
    KEY        ix_users_status   (status),
    CONSTRAINT fk_users_supervisor FOREIGN KEY (supervisor_user_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 2. REFRESH TOKENS (ORM: RefreshToken)
-- ============================================================
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    user_id     INT UNSIGNED    NOT NULL,
    jti         VARCHAR(64)     NOT NULL,
    expires_at  DATETIME        NOT NULL,
    revoked_at  DATETIME        NULL,
    ip_address  VARCHAR(45)     NULL,
    user_agent  VARCHAR(255)    NULL,
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_refresh_jti (jti),
    KEY ix_refresh_user (user_id),
    CONSTRAINT fk_refresh_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 3. ROLES / PERMISSIONS / RBAC (ORM: Role, Permission, …)
-- ============================================================
CREATE TABLE IF NOT EXISTS roles (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    `key`       VARCHAR(20)     NOT NULL,
    label       VARCHAR(50)     NOT NULL,
    description VARCHAR(255)    NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_roles_key (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS permissions (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    `key`       VARCHAR(80)     NOT NULL,
    `group`     VARCHAR(40)     NOT NULL,
    label       VARCHAR(120)    NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_permissions_key (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS role_permissions (
    id             INT UNSIGNED NOT NULL AUTO_INCREMENT,
    role_id        INT UNSIGNED NOT NULL,
    permission_id  INT UNSIGNED NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_role_perm (role_id, permission_id),
    CONSTRAINT fk_rp_role FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE,
    CONSTRAINT fk_rp_perm FOREIGN KEY (permission_id) REFERENCES permissions (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS module_access (
    id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
    role_id    INT UNSIGNED NOT NULL,
    module_key VARCHAR(40)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_module_access (role_id, module_key),
    CONSTRAINT fk_ma_role FOREIGN KEY (role_id) REFERENCES roles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 4. ENTITIES (ORM: Entity)
-- ============================================================
CREATE TABLE IF NOT EXISTS entities (
    id              VARCHAR(20)     NOT NULL,
    name            VARCHAR(150)    NOT NULL,
    region          ENUM('North America','Europe','APAC') NOT NULL,
    supervisor_id   INT UNSIGNED    NOT NULL,
    director_id     INT UNSIGNED    NULL,
    status          ENUM('assigned','wip','under_review','rejected','approved','pending_director') NOT NULL DEFAULT 'assigned',
    tags            JSON            NOT NULL DEFAULT (JSON_ARRAY()),
    period_type     ENUM('YoY','MoM','QoQ','Custom') NOT NULL DEFAULT 'YoY',
    period_from     VARCHAR(20)     NOT NULL,
    period_to       VARCHAR(20)     NOT NULL,
    sub_due_date    DATE            NULL,
    rev_due_date    DATE            NULL,
    approval_level  ENUM('supervisor','director') NOT NULL DEFAULT 'supervisor',
    currency        VARCHAR(10)     NULL,
    fiscal_year_end VARCHAR(10)   NULL,
    tax_id          VARCHAR(50)     NULL,
    is_active       TINYINT(1)      NOT NULL DEFAULT 1,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_entities_region (region),
    KEY idx_entities_status (status),
    CONSTRAINT fk_entities_supervisor FOREIGN KEY (supervisor_id) REFERENCES users (id),
    CONSTRAINT fk_entities_director   FOREIGN KEY (director_id)   REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 5. USER ENTITY ACCESS (ORM: UserEntityAccess)
-- ============================================================
CREATE TABLE IF NOT EXISTS user_entity_access (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    user_id     INT UNSIGNED    NOT NULL,
    entity_id   VARCHAR(20)     NOT NULL,
    role_scope  ENUM('read','write','supervise') NOT NULL DEFAULT 'write',
    granted_by  INT UNSIGNED    NULL,
    granted_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_user_entity (user_id, entity_id),
    KEY ix_uea_user (user_id),
    KEY ix_uea_entity (entity_id),
    CONSTRAINT fk_uea_user    FOREIGN KEY (user_id)    REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_uea_entity  FOREIGN KEY (entity_id)  REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_uea_granter FOREIGN KEY (granted_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 6. ENTITY ASSIGNMENTS
-- ============================================================
-- NOTE: `gl_level` removed — never read or written by application code.
CREATE TABLE IF NOT EXISTS entity_assignments (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id   VARCHAR(20)     NOT NULL,
    caption     VARCHAR(100)    NOT NULL,
    assignee_id INT UNSIGNED    NOT NULL,
    start_date  DATE            NULL,
    end_date    DATE            NULL,
    is_active   TINYINT(1)      NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    KEY idx_ea_entity   (entity_id),
    KEY idx_ea_assignee (assignee_id),
    CONSTRAINT fk_ea_entity   FOREIGN KEY (entity_id)   REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_ea_assignee FOREIGN KEY (assignee_id) REFERENCES users    (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 7. COMPANY PROFILES
-- ============================================================
CREATE TABLE IF NOT EXISTS company_profiles (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id       VARCHAR(20)     NOT NULL,
    industry        VARCHAR(150)    NOT NULL DEFAULT '',
    geography       VARCHAR(100)    NOT NULL DEFAULT '',
    revenue_range   VARCHAR(50)     NOT NULL DEFAULT '',
    entity_type     VARCHAR(50)     NOT NULL DEFAULT '',
    brief           TEXT            NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_cp_entity (entity_id),
    CONSTRAINT fk_cp_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 8. THRESHOLDS
-- ============================================================
CREATE TABLE IF NOT EXISTS thresholds (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id       VARCHAR(20)     NOT NULL,
    statement       ENUM('pl','bs') NOT NULL,
    mode            ENUM('pct','abs','both') NOT NULL DEFAULT 'both',
    pct_value       DECIMAL(5,2)    NOT NULL DEFAULT 10.00,
    abs_value       DECIMAL(15,2)   NOT NULL DEFAULT 100000.00,
    logic           ENUM('and','or') NOT NULL DEFAULT 'or',
    apply_all       TINYINT(1)      NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    UNIQUE KEY uq_thr_entity_stmt (entity_id, statement),
    CONSTRAINT fk_thr_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 9. FLUX DATA (no generated column — matches ORM)
-- ============================================================
CREATE TABLE IF NOT EXISTS flux_data (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id       VARCHAR(20)     NOT NULL,
    statement       ENUM('pl','bs') NOT NULL,
    period_from     VARCHAR(20)     NOT NULL,
    period_to       VARCHAR(20)     NOT NULL,
    caption         VARCHAR(100)    NOT NULL,
    current_value   DECIMAL(15,2)   NOT NULL DEFAULT 0,
    comp_value      DECIMAL(15,2)   NOT NULL DEFAULT 0,
    pct_variance    DECIMAL(8,2)    NULL,
    is_material     TINYINT(1)      NOT NULL DEFAULT 0,
    is_bold         TINYINT(1)      NOT NULL DEFAULT 0,
    sort_order      SMALLINT        NOT NULL DEFAULT 0,
    -- Provenance: 'seed' / 'business_central' / 'manual'. NULL ⇒ legacy seed.
    source_type     VARCHAR(40)     NULL,
    generated_from  VARCHAR(40)     NULL,
    generated_at    DATETIME        NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_flux_entity_stmt_cap (entity_id, statement, period_from, period_to, caption),
    KEY idx_flux_entity   (entity_id),
    KEY idx_flux_material (is_material),
    KEY idx_flux_source   (entity_id, source_type),
    CONSTRAINT fk_flux_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 10. AI COMMENTARY
-- ============================================================
CREATE TABLE IF NOT EXISTS ai_commentary (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id       VARCHAR(20)     NOT NULL,
    caption         VARCHAR(100)    NOT NULL,
    concise_text    TEXT            NOT NULL,
    detailed_text   TEXT            NOT NULL,
    edited_text     TEXT            NULL,
    edited_by       INT UNSIGNED    NULL,
    edited_at       DATETIME        NULL,
    ai_score        INT             NOT NULL DEFAULT 80,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_ai_entity_cap (entity_id, caption),
    CONSTRAINT fk_ai_entity    FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_ai_edited_by FOREIGN KEY (edited_by) REFERENCES users    (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 11. CAPTION VERSIONS
-- ============================================================
CREATE TABLE IF NOT EXISTS caption_versions (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id   VARCHAR(20)     NOT NULL,
    caption     VARCHAR(100)    NOT NULL,
    version     INT UNSIGNED    NOT NULL DEFAULT 0,
    updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_cv_entity_cap (entity_id, caption),
    CONSTRAINT fk_cv_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 12. COMMENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS comments (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id   VARCHAR(20)     NOT NULL,
    caption     VARCHAR(100)    NOT NULL,
    user_id     INT UNSIGNED    NOT NULL,
    text        TEXT            NOT NULL,
    parent_id   INT UNSIGNED    NULL,
    is_resolved TINYINT(1)      NOT NULL DEFAULT 0,
    resolved_by INT UNSIGNED    NULL,
    resolved_at DATETIME        NULL,
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_comments_entity_cap (entity_id, caption),
    KEY idx_comments_parent     (parent_id),
    KEY idx_comments_resolved   (is_resolved),
    CONSTRAINT fk_comments_entity      FOREIGN KEY (entity_id)   REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_comments_user        FOREIGN KEY (user_id)     REFERENCES users    (id),
    CONSTRAINT fk_comments_parent      FOREIGN KEY (parent_id)   REFERENCES comments (id),
    CONSTRAINT fk_comments_resolved_by FOREIGN KEY (resolved_by) REFERENCES users    (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 13. SIGN-OFFS
-- ============================================================
CREATE TABLE IF NOT EXISTS signoffs (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id   VARCHAR(20)     NOT NULL,
    caption     VARCHAR(100)    NOT NULL,
    signed_by   INT UNSIGNED    NOT NULL,
    signed_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    version     INT UNSIGNED    NOT NULL DEFAULT 0,
    is_stale    TINYINT(1)      NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_signoffs_entity_cap (entity_id, caption),
    CONSTRAINT fk_signoffs_entity    FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_signoffs_signed_by FOREIGN KEY (signed_by) REFERENCES users    (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 14. MACRO FACTORS
-- ============================================================
CREATE TABLE IF NOT EXISTS macro_factors (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id   VARCHAR(20)     NOT NULL,
    category    VARCHAR(50)     NOT NULL,
    icon        VARCHAR(10)     NOT NULL DEFAULT '📌',
    text        TEXT            NOT NULL,
    is_custom   TINYINT(1)      NOT NULL DEFAULT 0,
    created_by  INT UNSIGNED    NOT NULL,
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_mf_entity   (entity_id),
    KEY idx_mf_category (category),
    CONSTRAINT fk_mf_entity     FOREIGN KEY (entity_id)  REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_mf_created_by FOREIGN KEY (created_by) REFERENCES users    (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 15. TASKS (phase enum matches ORM: preparation, review, submission, approval)
-- ============================================================
CREATE TABLE IF NOT EXISTS tasks (
    id           INT UNSIGNED   NOT NULL AUTO_INCREMENT,
    entity_id    VARCHAR(20)    NOT NULL,
    title        VARCHAR(200)   NOT NULL,
    description  TEXT           NULL,
    assignee_id  INT UNSIGNED   NOT NULL,
    due_date     DATE           NOT NULL,
    status       ENUM('todo','in_progress','pending','done') NOT NULL DEFAULT 'todo',
    priority     ENUM('low','medium','high','critical') NOT NULL DEFAULT 'medium',
    phase        ENUM('preparation','review','submission','approval') NOT NULL DEFAULT 'preparation',
    is_recurring TINYINT(1)     NOT NULL DEFAULT 0,
    created_at   DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_tasks_entity   (entity_id),
    KEY idx_tasks_assignee (assignee_id),
    KEY idx_tasks_status   (status),
    KEY idx_tasks_due      (due_date),
    CONSTRAINT fk_tasks_entity   FOREIGN KEY (entity_id)   REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_tasks_assignee FOREIGN KEY (assignee_id) REFERENCES users    (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 16. TASK REMINDERS
-- ============================================================
CREATE TABLE IF NOT EXISTS task_reminders (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    task_id     INT UNSIGNED    NOT NULL,
    days_before INT             NOT NULL DEFAULT 1,
    remind_time TIME            NOT NULL DEFAULT '09:00:00',
    email       VARCHAR(150)    NOT NULL,
    is_sent     TINYINT(1)      NOT NULL DEFAULT 0,
    sent_at     DATETIME        NULL,
    PRIMARY KEY (id),
    KEY idx_tr_task (task_id),
    CONSTRAINT fk_tr_task FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 17. TASK RECURRENCE
-- ============================================================
CREATE TABLE IF NOT EXISTS task_recurrence (
    id           INT UNSIGNED   NOT NULL AUTO_INCREMENT,
    task_id      INT UNSIGNED   NOT NULL,
    frequency    ENUM('weekly','monthly','quarterly') NOT NULL DEFAULT 'monthly',
    day_of_month INT            NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_recur_task (task_id),
    CONSTRAINT fk_recur_task FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 18. PERSONAL REMINDERS
-- ============================================================
CREATE TABLE IF NOT EXISTS personal_reminders (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    user_id     INT UNSIGNED    NOT NULL,
    text        TEXT            NOT NULL,
    due_date    DATE            NOT NULL,
    remind_time TIME            NOT NULL DEFAULT '09:00:00',
    is_done     TINYINT(1)      NOT NULL DEFAULT 0,
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_pr_user (user_id),
    CONSTRAINT fk_pr_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 19. CHAT MESSAGES
-- ============================================================
CREATE TABLE IF NOT EXISTS chat_messages (
    id           INT UNSIGNED   NOT NULL AUTO_INCREMENT,
    entity_id    VARCHAR(20)    NOT NULL,
    user_id      INT UNSIGNED   NOT NULL,
    text         TEXT           NOT NULL,
    is_flagged   TINYINT(1)     NOT NULL DEFAULT 0,
    forwarded_to INT UNSIGNED   NULL,
    created_at   DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_cm_entity (entity_id),
    KEY idx_cm_user   (user_id),
    CONSTRAINT fk_cm_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_cm_user   FOREIGN KEY (user_id)   REFERENCES users    (id),
    CONSTRAINT fk_cm_fwd    FOREIGN KEY (forwarded_to) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 20. CHAT REPLIES
-- ============================================================
CREATE TABLE IF NOT EXISTS chat_replies (
    id         INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    message_id INT UNSIGNED    NOT NULL,
    user_id    INT UNSIGNED    NOT NULL,
    text       TEXT            NOT NULL,
    created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_cr_message (message_id),
    CONSTRAINT fk_cr_message FOREIGN KEY (message_id) REFERENCES chat_messages (id) ON DELETE CASCADE,
    CONSTRAINT fk_cr_user    FOREIGN KEY (user_id)    REFERENCES users          (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 21. DELEGATION LOG
-- ============================================================
CREATE TABLE IF NOT EXISTS delegation_log (
    id           INT UNSIGNED   NOT NULL AUTO_INCREMENT,
    entity_id    VARCHAR(20)    NOT NULL,
    from_user_id INT UNSIGNED   NOT NULL,
    to_user_id   INT UNSIGNED   NOT NULL,
    note         TEXT           NULL,
    created_at   DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_dl_entity (entity_id),
    CONSTRAINT fk_dl_entity    FOREIGN KEY (entity_id)    REFERENCES entities (id),
    CONSTRAINT fk_dl_from_user FOREIGN KEY (from_user_id) REFERENCES users    (id),
    CONSTRAINT fk_dl_to_user   FOREIGN KEY (to_user_id)   REFERENCES users    (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 22. EXPORT LOG
-- ============================================================
CREATE TABLE IF NOT EXISTS export_log (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id   VARCHAR(20)     NOT NULL,
    exported_by INT UNSIGNED    NOT NULL,
    mode        ENUM('concise','detailed')  NOT NULL DEFAULT 'concise',
    statement   ENUM('pl','bs','both')      NOT NULL DEFAULT 'both',
    file_path   VARCHAR(500)    NULL,
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_el_entity (entity_id),
    CONSTRAINT fk_el_entity      FOREIGN KEY (entity_id)   REFERENCES entities (id),
    CONSTRAINT fk_el_exported_by FOREIGN KEY (exported_by) REFERENCES users    (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 23. AUDIT LOGS (ORM: AuditLog)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    id             INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    actor_user_id  INT UNSIGNED    NOT NULL,
    action         VARCHAR(60)     NOT NULL,
    table_name     VARCHAR(60)     NOT NULL,
    row_pk         VARCHAR(80)     NULL,
    before_json    JSON            NULL,
    after_json     JSON            NULL,
    ip_address     VARCHAR(45)     NULL,
    created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_audit_actor (actor_user_id),
    KEY ix_audit_action (action),
    KEY ix_audit_table (table_name),
    KEY ix_audit_created (created_at),
    CONSTRAINT fk_audit_actor FOREIGN KEY (actor_user_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 24. BRANDING SETTINGS (ORM: BrandingSettings)
-- ============================================================
CREATE TABLE IF NOT EXISTS branding_settings (
    id             INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    logo_url       VARCHAR(500)    NULL,
    primary_color  VARCHAR(10)     NULL,
    updated_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by     INT UNSIGNED    NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_branding_user FOREIGN KEY (updated_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 25. ERP CATALOG (ORM: ErpCatalog)
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_catalog (
    id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    `key`       VARCHAR(40)     NOT NULL,
    name        VARCHAR(100)    NOT NULL,
    description VARCHAR(255)    NULL,
    icon        VARCHAR(10)     NULL,
    color       VARCHAR(20)     NULL,
    auth_type   VARCHAR(40)     NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_erp_catalog_key (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 26. ENTITY ERP MAPPINGS (ORM: EntityErpMapping)
-- ============================================================
CREATE TABLE IF NOT EXISTS entity_erp_mappings (
    id            INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id     VARCHAR(20)     NOT NULL,
    erp_key       VARCHAR(40)     NOT NULL,
    status        ENUM('disconnected','connecting','connected','error') NOT NULL DEFAULT 'disconnected',
    last_sync_at  DATETIME        NULL,
    connected_by  INT UNSIGNED    NULL,
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_entity_erp (entity_id, erp_key),
    KEY ix_eem_entity (entity_id),
    CONSTRAINT fk_eem_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_eem_user   FOREIGN KEY (connected_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 27. API CREDENTIALS — legacy mock-ERP layers (ORM: ApiCredential)
-- ============================================================
-- NOTE: `key_version` removed — was never read or written.
CREATE TABLE IF NOT EXISTS api_credentials (
    id               INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id        VARCHAR(20)     NOT NULL,
    layer            ENUM('gl','sub_gl','transaction') NOT NULL,
    item_name        VARCHAR(100)    NULL,
    endpoint         VARCHAR(500)    NULL,
    auth_type        VARCHAR(40)     NULL,
    encrypted_secret TEXT            NULL,
    last4            VARCHAR(8)      NULL,
    status           ENUM('missing','unverified','verified','error') NOT NULL DEFAULT 'missing',
    last_tested_at   DATETIME        NULL,
    last_synced_at   DATETIME        NULL,
    PRIMARY KEY (id),
    KEY ix_api_cred_entity (entity_id),
    CONSTRAINT fk_api_cred_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 28. API SYNC RUNS — legacy per-credential runs (ORM: ApiSyncRun)
-- ============================================================
CREATE TABLE IF NOT EXISTS api_sync_runs (
    id             INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    credential_id  INT UNSIGNED    NOT NULL,
    started_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at    DATETIME        NULL,
    ok             TINYINT(1)      NOT NULL DEFAULT 0,
    safe_error     VARCHAR(500)    NULL,
    rows_synced    INT             NULL,
    PRIMARY KEY (id),
    KEY ix_api_sync_cred (credential_id),
    CONSTRAINT fk_api_sync_cred FOREIGN KEY (credential_id) REFERENCES api_credentials (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 29. ONTOLOGY MAPPINGS (ORM: OntologyMapping)
-- ============================================================
CREATE TABLE IF NOT EXISTS ontology_mappings (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id       VARCHAR(20)     NOT NULL,
    statement       ENUM('pl','bs') NOT NULL,
    tflux_caption   VARCHAR(100)    NOT NULL,
    erp_gl_json     JSON            NOT NULL DEFAULT (JSON_ARRAY()),
    erp_subgl_json  JSON            NOT NULL DEFAULT (JSON_ARRAY()),
    erp_subledger_json JSON         NULL,
    custom_caption  VARCHAR(150)    NULL,
    is_hidden       TINYINT(1)      NOT NULL DEFAULT 0,
    display_column  ENUM('tflux','erp','custom') NOT NULL DEFAULT 'tflux',
    sort_order      SMALLINT        NOT NULL DEFAULT 0,
    version         INT             NOT NULL DEFAULT 1,
    updated_by      INT UNSIGNED    NULL,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_ontology (entity_id, statement, tflux_caption),
    CONSTRAINT fk_ontology_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_ontology_user   FOREIGN KEY (updated_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 30. CUSTOM CAPTIONS (ORM: CustomCaption)
-- ============================================================
CREATE TABLE IF NOT EXISTS custom_captions (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id       VARCHAR(20)     NOT NULL,
    statement       ENUM('pl','bs') NOT NULL,
    name            VARCHAR(150)    NOT NULL,
    is_subtotal     TINYINT(1)      NOT NULL DEFAULT 0,
    erp_gl_json     JSON            NOT NULL DEFAULT (JSON_ARRAY()),
    erp_subgl_json  JSON            NOT NULL DEFAULT (JSON_ARRAY()),
    erp_subledger_json JSON         NULL,
    sort_order      SMALLINT        NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY ix_custom_entity (entity_id),
    CONSTRAINT fk_custom_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 31. BUSINESS CENTRAL CREDENTIALS (ORM: BusinessCentralCredential)
--     Sensitive columns are ciphertext at rest; never store plaintext here.
-- ============================================================
CREATE TABLE IF NOT EXISTS bc_credentials (
    id                       INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id                VARCHAR(20)     NOT NULL,
    erp_type                 VARCHAR(40)     NOT NULL DEFAULT 'bc',
    auth_type                VARCHAR(40)     NOT NULL DEFAULT 'oauth2_client_credentials',
    tenant_id_encrypted      TEXT            NOT NULL,
    client_id_encrypted      TEXT            NOT NULL,
    client_secret_encrypted  TEXT            NOT NULL,
    environment_name         VARCHAR(100)    NOT NULL,
    scope                    VARCHAR(255)    NOT NULL,
    base_url                 VARCHAR(255)    NOT NULL,
    company_id               VARCHAR(100)    NULL,
    company_name             VARCHAR(150)    NULL,
    last4_client             VARCHAR(8)      NULL,
    last4_secret             VARCHAR(8)      NULL,
    status                   ENUM(
        'not_configured','saved','testing','verified','failed','syncing','synced'
    ) NOT NULL DEFAULT 'not_configured',
    last_safe_error_code     VARCHAR(80)     NULL,
    last_safe_error_message  VARCHAR(500)    NULL,
    last_tested_at           DATETIME        NULL,
    last_verified_at         DATETIME        NULL,
    created_by               INT UNSIGNED    NULL,
    updated_by               INT UNSIGNED    NULL,
    created_at               DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_bc_credentials_entity (entity_id),
    KEY ix_bc_cred_entity (entity_id),
    CONSTRAINT fk_bc_cred_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_bc_cred_created FOREIGN KEY (created_by) REFERENCES users (id),
    CONSTRAINT fk_bc_cred_updated FOREIGN KEY (updated_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 32. ERP SYNC RUNS — BC / connector history (ORM: ErpSyncRun)
--     sync_type is VARCHAR(64) — new sync types do not require ALTER ENUM.
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_sync_runs (
    id                  INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id           VARCHAR(20)     NOT NULL,
    erp_type            VARCHAR(40)     NOT NULL DEFAULT 'bc',
    sync_type           VARCHAR(64)     NOT NULL,
    status              ENUM('running','success','failed') NOT NULL DEFAULT 'running',
    started_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at         DATETIME        NULL,
    safe_error_code     VARCHAR(80)     NULL,
    safe_error_message  VARCHAR(500)    NULL,
    records_processed   INT             NULL,
    created_by          INT UNSIGNED    NULL,
    PRIMARY KEY (id),
    KEY ix_erp_sync_runs_entity_started (entity_id, started_at),
    KEY ix_erp_sync_entity (entity_id),
    CONSTRAINT fk_erp_sync_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_erp_sync_user   FOREIGN KEY (created_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 33. ERP COMPANIES (ORM: ErpCompany)
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_companies (
    id           INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id    VARCHAR(20)     NOT NULL,
    erp_type     VARCHAR(40)     NOT NULL DEFAULT 'bc',
    company_id   VARCHAR(100)    NOT NULL,
    company_name VARCHAR(200)    NOT NULL,
    updated_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_erp_companies_entity_erp_company (entity_id, erp_type, company_id),
    KEY ix_erp_companies_entity (entity_id),
    CONSTRAINT fk_erp_companies_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 34. ERP GL ACCOUNTS (ORM: ErpGlAccount)
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_gl_accounts (
    id               INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id        VARCHAR(20)     NOT NULL,
    erp_type         VARCHAR(40)     NOT NULL DEFAULT 'bc',
    company_id       VARCHAR(100)    NOT NULL,
    account_no       VARCHAR(80)     NOT NULL,
    account_name     VARCHAR(255)    NULL,
    account_category VARCHAR(80)   NULL,
    external_id      VARCHAR(100)    NULL,
    display_name     VARCHAR(255)    NULL,
    sub_category     VARCHAR(80)     NULL,
    account_type     VARCHAR(80)     NULL,
    blocked          TINYINT(1)      NULL,
    raw_source_json  JSON            NULL,
    last_synced_at   DATETIME        NULL,
    updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_erp_gl_accounts_unique (entity_id, erp_type, company_id, account_no),
    KEY ix_erp_gl_entity (entity_id),
    CONSTRAINT fk_erp_gl_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 35. ERP DIMENSION VALUES — Sub-GL / BC dimensionValues (ORM: ErpDimensionValue)
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_dimension_values (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id       VARCHAR(20)     NOT NULL,
    erp_type        VARCHAR(40)     NOT NULL DEFAULT 'bc',
    company_id      VARCHAR(100)    NOT NULL,
    dimension_id    VARCHAR(100)    NULL,
    dimension_code  VARCHAR(80)     NOT NULL,
    dimension_name  VARCHAR(255)    NULL,
    value_id        VARCHAR(100)    NULL,
    value_code      VARCHAR(80)     NOT NULL,
    value_name      VARCHAR(255)    NULL,
    display_name    VARCHAR(255)    NULL,
    raw_source_json JSON            NULL,
    last_synced_at  DATETIME        NULL,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_erp_dim_values_unique (entity_id, erp_type, company_id, dimension_code, value_code),
    KEY ix_erp_dim_values_entity_company (entity_id, company_id),
    CONSTRAINT fk_erp_dim_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 36. ERP GENERAL LEDGER ENTRIES (ORM: ErpGeneralLedgerEntry)
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_general_ledger_entries (
    id                INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id         VARCHAR(20)     NOT NULL,
    erp_type          VARCHAR(40)     NOT NULL DEFAULT 'bc',
    company_id        VARCHAR(100)    NOT NULL,
    external_entry_no VARCHAR(80)     NOT NULL,
    posting_date      DATE            NULL,
    document_no       VARCHAR(100)    NULL,
    account_no        VARCHAR(80)     NULL,
    amount            DECIMAL(20,2)   NULL,
    description       VARCHAR(255)    NULL,
    external_id       VARCHAR(100)    NULL,
    entry_number      VARCHAR(80)     NULL,
    account_id        VARCHAR(100)    NULL,
    debit_amount      DECIMAL(20,2)   NULL,
    credit_amount     DECIMAL(20,2)   NULL,
    dimension_set_id  VARCHAR(100)    NULL,
    source_type       VARCHAR(80)     NULL,
    source_number     VARCHAR(80)     NULL,
    bal_account_type  VARCHAR(120)    NULL,
    bal_account_number VARCHAR(80)    NULL,
    external_document_number VARCHAR(100) NULL,
    raw_source_json   JSON            NULL,
    last_synced_at    DATETIME        NULL,
    updated_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_erp_gl_entries_unique (entity_id, erp_type, company_id, external_entry_no),
    KEY ix_erp_gl_entries_post_date (entity_id, company_id, posting_date),
    CONSTRAINT fk_erp_gl_entries_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 37. ERP CUSTOMERS (ORM: ErpCustomer)
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_customers (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id       VARCHAR(20)     NOT NULL,
    erp_type        VARCHAR(40)     NOT NULL DEFAULT 'bc',
    company_id      VARCHAR(100)    NOT NULL,
    customer_no     VARCHAR(80)     NOT NULL,
    display_name    VARCHAR(255)    NULL,
    external_id     VARCHAR(100)    NULL,
    raw_source_json JSON            NULL,
    last_synced_at  DATETIME        NULL,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_erp_customers_unique (entity_id, erp_type, company_id, customer_no),
    KEY ix_erp_customers_entity_company (entity_id, company_id),
    CONSTRAINT fk_erp_customers_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 38. ERP VENDORS (ORM: ErpVendor)
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_vendors (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id       VARCHAR(20)     NOT NULL,
    erp_type        VARCHAR(40)     NOT NULL DEFAULT 'bc',
    company_id      VARCHAR(100)    NOT NULL,
    vendor_no       VARCHAR(80)     NOT NULL,
    display_name    VARCHAR(255)    NULL,
    external_id     VARCHAR(100)    NULL,
    raw_source_json JSON            NULL,
    last_synced_at  DATETIME        NULL,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_erp_vendors_unique (entity_id, erp_type, company_id, vendor_no),
    KEY ix_erp_vendors_entity_company (entity_id, company_id),
    CONSTRAINT fk_erp_vendors_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 39. ERP CUSTOMER LEDGER ENTRIES (ORM: ErpCustomerLedgerEntry)
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_customer_ledger_entries (
    id                INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id         VARCHAR(20)     NOT NULL,
    erp_type          VARCHAR(40)     NOT NULL DEFAULT 'bc',
    company_id        VARCHAR(100)    NOT NULL,
    customer_no       VARCHAR(80)     NOT NULL,
    customer_name     VARCHAR(255)    NULL,
    document_no       VARCHAR(100)    NULL,
    posting_date      DATE            NULL,
    amount            DECIMAL(20,2)   NULL,
    remaining_amount  DECIMAL(20,2)   NULL,
    external_entry_no VARCHAR(80)     NOT NULL,
    raw_source_json   JSON            NULL,
    last_synced_at    DATETIME        NULL,
    updated_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_erp_customer_ledger_unique (entity_id, erp_type, company_id, external_entry_no),
    KEY ix_erp_customer_ledger_post (entity_id, company_id, posting_date),
    CONSTRAINT fk_erp_customer_ledger_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 40. ERP VENDOR LEDGER ENTRIES (ORM: ErpVendorLedgerEntry)
-- ============================================================
CREATE TABLE IF NOT EXISTS erp_vendor_ledger_entries (
    id                INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id         VARCHAR(20)     NOT NULL,
    erp_type          VARCHAR(40)     NOT NULL DEFAULT 'bc',
    company_id        VARCHAR(100)    NOT NULL,
    vendor_no         VARCHAR(80)     NOT NULL,
    vendor_name       VARCHAR(255)    NULL,
    document_no       VARCHAR(100)    NULL,
    posting_date      DATE            NULL,
    amount            DECIMAL(20,2)   NULL,
    remaining_amount  DECIMAL(20,2)   NULL,
    external_entry_no VARCHAR(80)     NOT NULL,
    raw_source_json   JSON            NULL,
    last_synced_at    DATETIME        NULL,
    updated_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_erp_vendor_ledger_unique (entity_id, erp_type, company_id, external_entry_no),
    KEY ix_erp_vendor_ledger_post (entity_id, company_id, posting_date),
    CONSTRAINT fk_erp_vendor_ledger_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- 41. QUICKBOOKS CREDENTIALS (ORM: QuickBooksCredential)
--     Sensitive columns are ciphertext at rest; never store plaintext here.
-- ============================================================
CREATE TABLE IF NOT EXISTS quickbooks_credentials (
    id                       INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    entity_id                VARCHAR(20)     NOT NULL,
    erp_type                 VARCHAR(40)     NOT NULL DEFAULT 'quickbooks',
    auth_type                VARCHAR(40)     NOT NULL DEFAULT 'oauth2_authorization_code',
    client_id_encrypted      TEXT            NULL,
    client_secret_encrypted  TEXT            NULL,
    redirect_uri             VARCHAR(512)    NULL,
    scopes                   VARCHAR(512)    NULL,
    auth_url                 VARCHAR(512)    NULL,
    token_url                VARCHAR(512)    NULL,
    api_base_sandbox         VARCHAR(512)    NULL,
    api_base_production      VARCHAR(512)    NULL,
    last4_client             VARCHAR(8)      NULL,
    last4_secret             VARCHAR(8)      NULL,
    access_token_encrypted   TEXT            NULL,
    refresh_token_encrypted  TEXT            NULL,
    token_expiry             DATETIME        NULL,
    realm_id                 VARCHAR(100)    NULL,
    company_name             VARCHAR(255)    NULL,
    environment              VARCHAR(40)     NOT NULL DEFAULT 'sandbox',
    oauth_state              VARCHAR(128)    NULL,
    oauth_state_expires_at   DATETIME        NULL,
    status                   ENUM(
        'not_configured','saved','testing','verified','failed','syncing','synced'
    ) NOT NULL DEFAULT 'not_configured',
    last_safe_error_code     VARCHAR(80)     NULL,
    last_safe_error_message  VARCHAR(500)    NULL,
    last_tested_at           DATETIME        NULL,
    last_verified_at         DATETIME        NULL,
    created_by               INT UNSIGNED    NULL,
    updated_by               INT UNSIGNED    NULL,
    created_at               DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_qb_credentials_entity (entity_id),
    KEY ix_qb_cred_entity (entity_id),
    CONSTRAINT fk_qb_cred_entity FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE,
    CONSTRAINT fk_qb_cred_created FOREIGN KEY (created_by) REFERENCES users (id),
    CONSTRAINT fk_qb_cred_updated FOREIGN KEY (updated_by) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ── Notifications ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id                  INT             NOT NULL AUTO_INCREMENT,
    recipient_user_id   INT UNSIGNED    NOT NULL,
    actor_user_id       INT UNSIGNED    NULL,
    entity_id           VARCHAR(20)     NULL,
    type                VARCHAR(60)     NOT NULL,
    title               VARCHAR(200)    NOT NULL,
    message             TEXT            NOT NULL,
    link_url            VARCHAR(500)    NULL,
    metadata_json       JSON            NULL,
    is_read             TINYINT(1)      NOT NULL DEFAULT 0,
    read_at             DATETIME        NULL,
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_notifications_recipient (recipient_user_id),
    KEY ix_notifications_entity (entity_id),
    KEY ix_notifications_type (type),
    KEY ix_notifications_is_read (is_read),
    KEY ix_notifications_created_at (created_at),
    CONSTRAINT fk_notifications_recipient FOREIGN KEY (recipient_user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_notifications_actor     FOREIGN KEY (actor_user_id)     REFERENCES users (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
