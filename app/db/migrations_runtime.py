# @file    db/migrations_runtime.py
#
# Idempotent runtime schema aligner for the Business Central / ERP
# normalization tables.
#
# Why this file exists
# --------------------
# `app/main.py` invokes `Base.metadata.create_all(bind=engine)` in dev to
# bootstrap a fresh database. `create_all` only creates *missing tables* —
# it never alters an existing table. As the BC ontology spec evolved, the
# `ErpGlAccount` and `ErpGeneralLedgerEntry` models grew new optional
# columns (external_id, display_name, sub_category, account_type, blocked,
# raw_source_json, last_synced_at, …). On databases that were initialised
# before those fields existed, INSERTs now fail with MySQL error 1054
# "Unknown column 'external_id' in 'field list'".
#
# This module fixes that without dropping any data:
#   1. Detects each missing column via INFORMATION_SCHEMA.
#   2. Issues a non-destructive `ALTER TABLE ... ADD COLUMN` only for the
#      columns that are missing.
#   3. Adds a small set of helpful indexes (entity/company/external_id),
#      again only if missing.
#
# Safe to call on every startup. Logs a tiny summary and never raises
# (a DB failure must not block the API from starting).
#
# Usage
# -----
#   • Wired into `app/main.py` and runs automatically on startup.
#   • For prod / one-off runs:
#         python -m app.db.migrations_runtime

from __future__ import annotations

import logging
from typing import List, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

_log = logging.getLogger(__name__)


# ── Column specs ────────────────────────────────────────────────────────────
# (table, column, "<MySQL DDL fragment after ADD COLUMN <name>>")
# All columns are nullable so the ALTERs are non-destructive on legacy rows.
_BC_COLUMNS: List[Tuple[str, str, str]] = [
    # erp_gl_accounts — extras added in 2026-05 BC ontology spec.
    ("erp_gl_accounts", "external_id",     "VARCHAR(100) NULL"),
    ("erp_gl_accounts", "display_name",    "VARCHAR(255) NULL"),
    ("erp_gl_accounts", "sub_category",    "VARCHAR(80)  NULL"),
    ("erp_gl_accounts", "account_type",    "VARCHAR(80)  NULL"),
    ("erp_gl_accounts", "blocked",         "TINYINT(1)   NULL"),
    ("erp_gl_accounts", "raw_source_json", "JSON         NULL"),
    ("erp_gl_accounts", "last_synced_at",  "DATETIME     NULL"),
    # erp_general_ledger_entries — extras for richer normalization.
    ("erp_general_ledger_entries", "external_id",      "VARCHAR(100)  NULL"),
    ("erp_general_ledger_entries", "entry_number",     "VARCHAR(80)   NULL"),
    ("erp_general_ledger_entries", "account_id",       "VARCHAR(100)  NULL"),
    ("erp_general_ledger_entries", "debit_amount",     "DECIMAL(20,2) NULL"),
    ("erp_general_ledger_entries", "credit_amount",    "DECIMAL(20,2) NULL"),
    ("erp_general_ledger_entries", "dimension_set_id", "VARCHAR(100)  NULL"),
    ("erp_general_ledger_entries", "source_type",    "VARCHAR(80)   NULL"),
    ("erp_general_ledger_entries", "source_number",  "VARCHAR(80)   NULL"),
    ("erp_general_ledger_entries", "bal_account_type", "VARCHAR(120) NULL"),
    ("erp_general_ledger_entries", "bal_account_number", "VARCHAR(80) NULL"),
    ("erp_general_ledger_entries", "external_document_number", "VARCHAR(100) NULL"),
    ("erp_general_ledger_entries", "raw_source_json",  "JSON          NULL"),
    ("erp_general_ledger_entries", "last_synced_at",   "DATETIME      NULL"),
    # flux_data — provenance columns added 2026-05 so Flux Review can show
    # whether a row came from the BC pipeline or from the seed/demo loader.
    # All nullable so existing seed rows stay intact (they are treated as
    # seed implicitly when source_type IS NULL).
    ("flux_data", "source_type",    "VARCHAR(40)  NULL"),
    ("flux_data", "generated_from", "VARCHAR(40)  NULL"),
    ("flux_data", "generated_at",   "DATETIME     NULL"),
    # ontology — sub-ledger detail tokens (replaces dimension mapping UI).
    ("ontology_mappings", "erp_subledger_json", "JSON NULL"),
    ("custom_captions", "erp_subledger_json", "JSON NULL"),
    # quickbooks_credentials — per-entity Intuit app registration (2026-06).
    ("quickbooks_credentials", "client_id_encrypted",     "TEXT         NULL"),
    ("quickbooks_credentials", "client_secret_encrypted", "TEXT         NULL"),
    ("quickbooks_credentials", "redirect_uri",            "VARCHAR(512) NULL"),
    ("quickbooks_credentials", "scopes",                  "VARCHAR(512) NULL"),
    ("quickbooks_credentials", "auth_url",                "VARCHAR(512) NULL"),
    ("quickbooks_credentials", "token_url",               "VARCHAR(512) NULL"),
    ("quickbooks_credentials", "api_base_sandbox",        "VARCHAR(512) NULL"),
    ("quickbooks_credentials", "api_base_production",     "VARCHAR(512) NULL"),
    ("quickbooks_credentials", "last4_client",            "VARCHAR(8)   NULL"),
    ("quickbooks_credentials", "last4_secret",            "VARCHAR(8)   NULL"),
]


# (index_name, table, "(col1[, col2])"). Created only if absent.
_BC_INDEXES: List[Tuple[str, str, str]] = [
    ("ix_erp_gl_accounts_external_id", "erp_gl_accounts",            "(external_id)"),
    ("ix_erp_gl_accounts_company",     "erp_gl_accounts",            "(entity_id, company_id)"),
    ("ix_erp_gl_entries_external_id",  "erp_general_ledger_entries", "(external_id)"),
    ("ix_erp_gl_entries_company",      "erp_general_ledger_entries", "(entity_id, company_id)"),
    ("ix_erp_dim_values_value_id",     "erp_dimension_values",       "(value_id)"),
]


# Columns removed during the May-2026 schema cleanup. Each tuple is
# (table, column). The aligner drops these from existing databases when
# present so legacy installs match the trimmed ORM shape:
#   • users.name        — derived at runtime from first_name + last_name.
#   • users.is_active   — derived from `status` (status != 'disabled').
#   • entity_assignments.gl_level — never read or written by app code.
#   • api_credentials.key_version — never read or written by app code.
# Drops are guarded by INFORMATION_SCHEMA so the migration is idempotent
# and safe to re-run on databases that have already been cleaned up.
# IMPORTANT: `users.name` is dropped *after* the backfill step below.
_DROP_COLUMNS: List[Tuple[str, str]] = [
    ("users",               "name"),
    ("users",               "is_active"),
    ("entity_assignments",  "gl_level"),
    ("api_credentials",     "key_version"),
]


# Columns whose nullability is tightened to NOT NULL. The aligner first
# backfills any NULL/empty rows with the supplied fallback so the MODIFY
# never fails on legacy data.
#   (table, column, "<MySQL DDL fragment after MODIFY COLUMN ...>")
_TIGHTEN_NOT_NULL: List[Tuple[str, str, str]] = [
    ("users", "first_name", "VARCHAR(60) NOT NULL"),
    ("users", "last_name",  "VARCHAR(60) NOT NULL"),
]


def _is_mysql(engine: Engine) -> bool:
    return engine.dialect.name in {"mysql", "mariadb"}


def _table_exists(conn, schema: str, table: str) -> bool:
    return bool(conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = :schema AND table_name = :table"
    ), {"schema": schema, "table": table}).first())


def _column_exists(conn, schema: str, table: str, column: str) -> bool:
    return bool(conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = :schema AND table_name = :table AND column_name = :column"
    ), {"schema": schema, "table": table, "column": column}).first())


def _index_exists(conn, schema: str, table: str, index: str) -> bool:
    return bool(conn.execute(text(
        "SELECT 1 FROM information_schema.statistics "
        "WHERE table_schema = :schema AND table_name = :table AND index_name = :index"
    ), {"schema": schema, "table": table, "index": index}).first())


def _column_meta(conn, schema: str, table: str, column: str):
    """Returns (data_type, character_maximum_length) or None if absent."""
    row = conn.execute(text(
        "SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns "
        "WHERE table_schema = :schema AND table_name = :table AND column_name = :column"
    ), {"schema": schema, "table": table, "column": column}).first()
    if row is None:
        return None
    return (str(row[0]).lower(), row[1])


def _add_missing_columns(conn, schema: str) -> int:
    added = 0
    for table, column, ddl in _BC_COLUMNS:
        if not _table_exists(conn, schema, table):
            continue
        if _column_exists(conn, schema, table, column):
            continue
        # Backticks are safe — names are hard-coded above, never user input.
        conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ddl}"))
        added += 1
        _log.info("Added column %s.%s", table, column)
    return added


def _add_missing_indexes(conn, schema: str) -> int:
    added = 0
    for index_name, table, cols_expr in _BC_INDEXES:
        if not _table_exists(conn, schema, table):
            continue
        if _index_exists(conn, schema, table, index_name):
            continue
        conn.execute(text(
            f"CREATE INDEX `{index_name}` ON `{table}` {cols_expr}"
        ))
        added += 1
        _log.info("Added index %s on %s", index_name, table)
    return added


def _backfill_user_names(conn, schema: str) -> int:
    """If the legacy `users.name` column is still present, copy each row's
    name into first_name / last_name (when those are NULL or empty) before
    the column is dropped. Splits on the first space; trailing tokens form
    last_name. Returns the number of rows updated.

    Idempotent: a no-op on databases where `name` has already been dropped
    or where every row already has populated first/last names.
    """
    if not _table_exists(conn, schema, "users"):
        return 0
    if not _column_exists(conn, schema, "users", "name"):
        return 0

    result = conn.execute(text(
        "UPDATE `users` "
        "SET "
        "  first_name = CASE "
        "    WHEN first_name IS NULL OR TRIM(first_name) = '' "
        "    THEN COALESCE(NULLIF(SUBSTRING_INDEX(TRIM(name), ' ', 1), ''), 'User') "
        "    ELSE first_name "
        "  END, "
        "  last_name = CASE "
        "    WHEN last_name IS NULL OR TRIM(last_name) = '' "
        "    THEN CASE "
        "      WHEN LOCATE(' ', TRIM(name)) > 0 "
        "      THEN TRIM(SUBSTRING(TRIM(name), LOCATE(' ', TRIM(name)) + 1)) "
        "      ELSE COALESCE(NULLIF(TRIM(name), ''), '-') "
        "    END "
        "    ELSE last_name "
        "  END "
        "WHERE first_name IS NULL OR last_name IS NULL "
        "   OR TRIM(first_name) = '' OR TRIM(last_name) = ''"
    ))
    return int(result.rowcount or 0)


def _ensure_user_names_present(conn, schema: str) -> int:
    """Last-resort backfill that runs even when the legacy `name` column has
    already been dropped. Replaces any remaining NULL / empty first_name or
    last_name with safe placeholders so the subsequent NOT NULL tightening
    cannot fail. Returns the number of rows touched.
    """
    if not _table_exists(conn, schema, "users"):
        return 0
    result = conn.execute(text(
        "UPDATE `users` "
        "SET "
        "  first_name = CASE WHEN first_name IS NULL OR TRIM(first_name) = '' "
        "                   THEN 'User' ELSE first_name END, "
        "  last_name  = CASE WHEN last_name  IS NULL OR TRIM(last_name)  = '' "
        "                   THEN '-'    ELSE last_name  END "
        "WHERE first_name IS NULL OR last_name IS NULL "
        "   OR TRIM(first_name) = '' OR TRIM(last_name) = ''"
    ))
    return int(result.rowcount or 0)


def _tighten_not_null(conn, schema: str) -> int:
    """Promotes columns listed in `_TIGHTEN_NOT_NULL` to NOT NULL when they
    are currently nullable. Idempotent — only emits MODIFY when the column
    exists and is_nullable='YES'. Returns the count of columns altered."""
    altered = 0
    for table, column, ddl in _TIGHTEN_NOT_NULL:
        if not _table_exists(conn, schema, table):
            continue
        row = conn.execute(text(
            "SELECT IS_NULLABLE FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table "
            "AND column_name = :column"
        ), {"schema": schema, "table": table, "column": column}).first()
        if row is None:
            continue
        if str(row[0]).upper() == "NO":
            continue
        conn.execute(text(
            f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` {ddl}"
        ))
        altered += 1
        _log.info("Tightened %s.%s to %s", table, column, ddl)
    return altered


def _drop_obsolete_columns(conn, schema: str) -> int:
    """Drops columns listed in `_DROP_COLUMNS` when present. Each drop is
    independent and guarded by INFORMATION_SCHEMA so re-running the
    aligner on an already-clean database is a no-op."""
    dropped = 0
    for table, column in _DROP_COLUMNS:
        if not _table_exists(conn, schema, table):
            continue
        if not _column_exists(conn, schema, table, column):
            continue
        conn.execute(text(
            f"ALTER TABLE `{table}` DROP COLUMN `{column}`"
        ))
        dropped += 1
        _log.info("Dropped obsolete column %s.%s", table, column)
    return dropped


# Target shape for `erp_sync_runs.sync_type`. Originally created as a MySQL
# ENUM whose value list froze at first startup; widening the enum requires a
# destructive `ALTER ... MODIFY` and breaks each time a new sync type is
# added. We migrate to VARCHAR(64) once — non-destructive, future-proof —
# and the application enforces the allow-list at write time.
_SYNC_TYPE_TARGET_LENGTH = 64


def _align_sync_type_column(conn, schema: str) -> int:
    """If `erp_sync_runs.sync_type` is still a MySQL ENUM (or a too-short
    VARCHAR), widen it to VARCHAR(64). Returns 1 if migrated, else 0.
    Existing rows are preserved — MySQL serializes the active ENUM label to
    its text form when converting to a text column."""
    if not _table_exists(conn, schema, "erp_sync_runs"):
        return 0
    meta = _column_meta(conn, schema, "erp_sync_runs", "sync_type")
    if meta is None:
        return 0
    data_type, char_len = meta
    needs_change = (
        data_type == "enum"
        or (data_type in {"varchar", "char"}
            and (char_len is None or int(char_len) < _SYNC_TYPE_TARGET_LENGTH))
    )
    if not needs_change:
        return 0
    conn.execute(text(
        "ALTER TABLE `erp_sync_runs` MODIFY COLUMN `sync_type` "
        f"VARCHAR({_SYNC_TYPE_TARGET_LENGTH}) NOT NULL"
    ))
    _log.info("Migrated erp_sync_runs.sync_type from %s to VARCHAR(%d)",
              data_type, _SYNC_TYPE_TARGET_LENGTH)
    return 1


def _align_users_password_column(conn, schema: str) -> tuple[int, int]:
    """Ensure `users.password` exists for auth / forgot-password flows.

    Legacy databases may have:
      • `password_hash` only  → rename to `password`
      • neither column        → add `password` (bcrypt hash at rest)
      • `password` already    → no-op

    Returns ``(renamed_count, added_count)`` — each 0 or 1.
    """
    if not _table_exists(conn, schema, "users"):
        return 0, 0

    has_old = _column_exists(conn, schema, "users", "password_hash")
    has_new = _column_exists(conn, schema, "users", "password")

    if has_new and not has_old:
        return 0, 0

    if has_old and not has_new:
        conn.execute(text(
            "ALTER TABLE `users` CHANGE COLUMN `password_hash` `password` "
            "VARCHAR(255) NULL"
        ))
        _log.info("Renamed users.password_hash -> users.password")
        return 1, 0

    if not has_old and not has_new:
        conn.execute(text(
            "ALTER TABLE `users` ADD COLUMN `password` VARCHAR(255) NULL"
        ))
        _log.info("Added missing users.password column")
        return 0, 1

    if has_old and has_new:
        _log.warning(
            "users has both password_hash and password; manual cleanup may be needed"
        )
    return 0, 0


def align_business_central_schema(engine: Engine) -> dict:
    """Adds any missing BC normalization columns/indexes and applies the
    May-2026 cleanup (drops obsolete columns, tightens user-name nullability,
    renames ``password_hash`` → ``password`` on ``users`` when needed).
    Returns a small summary (added counts) for logging. Never raises — any
    failure is logged so startup is never blocked by a transient DB issue.

    A no-op on SQLite (used by the test suite); the test path relies on
    `Base.metadata.create_all` to produce the latest shape.
    """
    summary = {
        "columns_added":          0,
        "indexes_added":          0,
        "sync_type_migrated":     0,
        "password_column_renamed": 0,
        "password_column_added":   0,
        "user_names_backfilled":  0,
        "user_names_finalized":   0,
        "columns_tightened":      0,
        "columns_dropped":        0,
        "skipped":                True,
    }
    if not _is_mysql(engine):
        return summary

    try:
        with engine.begin() as conn:
            schema = conn.execute(text("SELECT DATABASE()")).scalar()
            if not schema:
                _log.warning("BC schema aligner: no database selected; skipping.")
                return summary
            summary["skipped"] = False
            summary["columns_added"]         = _add_missing_columns(conn, schema)
            summary["indexes_added"]         = _add_missing_indexes(conn, schema)
            summary["sync_type_migrated"]    = _align_sync_type_column(conn, schema)
            renamed, added = _align_users_password_column(conn, schema)
            summary["password_column_renamed"] = renamed
            summary["password_column_added"]   = added
            # Order matters: backfill first/last from legacy `name`, then
            # ensure no NULLs remain, tighten NOT NULL, and only THEN drop
            # the obsolete columns (including `users.name`).
            summary["user_names_backfilled"] = _backfill_user_names(conn, schema)
            summary["user_names_finalized"]  = _ensure_user_names_present(conn, schema)
            summary["columns_tightened"]     = _tighten_not_null(conn, schema)
            summary["columns_dropped"]       = _drop_obsolete_columns(conn, schema)
    except SQLAlchemyError:
        _log.exception(
            "BC schema aligner: failed (continuing without aligning)."
        )
    return summary


def _run_cli() -> None:
    """CLI entry — `python -m app.db.migrations_runtime`.

    Ensures all model tables exist (CREATE TABLE IF NOT EXISTS via SQLAlchemy)
    and then runs the column/index aligner. Suitable for prod migrations.
    """
    from app.db.session import Base, engine  # local to avoid import-time cost
    import app.models.models  # noqa: F401  — register all models

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    _log.info("BC migrator: ensuring model tables exist via create_all …")
    Base.metadata.create_all(bind=engine)
    summary = align_business_central_schema(engine)
    _log.info("BC migrator: done — %s", summary)


if __name__ == "__main__":
    _run_cli()
