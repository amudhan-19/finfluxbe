# @file    services/flux_generation.py
#
# Core logic for generating flux_data rows from Business Central GL entries
# via ontology mappings. Shared by the admin generate endpoint and by the
# diagnostics endpoint. Designed to be safe by default:
#
#   • Preview mode never writes the DB.
#   • Commit mode upserts BC-generated rows and stamps `source_type` /
#     `generated_from` / `generated_at` so Flux Review can label provenance.
#   • Existing seed rows are *replaced* only when the caller passes
#     ``overwrite_existing=True``; otherwise the seed survives so analysts
#     never lose context unexpectedly.

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from sqlalchemy import or_

from app.models.models import (
    Entity, ErpGeneralLedgerEntry, ErpGlAccount,
    FluxData, OntologyMapping, Threshold,
)
from app.services.flux_period_config import (
    FluxPeriodConfig,
    parse_flux_period_config,
    parse_period_string,
)
from app.services.flux_finance_variance import finance_variance_for_threshold
from app.services.subledger_catalog import (
    calculation_mode_for_statement,
    calculation_mode_label,
    entry_matches_subledger,
    extract_mapped_subledger_values,
    subledger_filter_warning,
)


# Known seed / demo entity IDs from scripts/seed.py.
# Used to label legacy rows when ``source_type IS NULL``.
SEED_ENTITY_IDS: frozenset[str] = frozenset({
    "ACM-US", "ACM-CA", "BET-US", "ACM-UK", "ACM-DE", "ACM-IN",
})

# Provenance tag stamped on rows we generate from BC.
BC_SOURCE_TYPE = "business_central"
BC_GENERATED_FROM = "ontology+bc"
QB_SOURCE_TYPE = "quickbooks"
QB_GENERATED_FROM = "ontology+quickbooks"

SKIP_NO_MANUAL_GL = "Caption has no manual GL mapping."
SKIP_NO_TRANSACTIONS = "No matching transactions in selected period."


# ── Public dataclasses ─────────────────────────────────────────────────────
@dataclass
class FluxRowPreview:
    statement: str
    caption: str
    current_value: Decimal
    comp_value: Decimal
    pct_variance: Optional[Decimal]
    is_material: bool
    is_bold: bool
    sort_order: int
    had_existing_row: bool = False
    matched_account_count: int = 0
    matched_transaction_count: int = 0
    skipped_reason: Optional[str] = None
    selected_gl_accounts: list[str] = field(default_factory=list)
    selected_subledger: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class StatementDiagnostics:
    """Per-statement (`pl` / `bs`) diagnostic counters.

    Lets the supervisor UI render a clean breakdown like
    "PL: 0 rows generated, 5 captions mapped, 0 transactions matched"
    even when BS regenerated cleanly — the most common reason a PL tab
    looks empty while BS works."""
    statement: str
    captions_total: int = 0
    captions_with_mappings: int = 0
    captions_without_mappings: list[str] = field(default_factory=list)
    captions_with_zero_transactions: list[str] = field(default_factory=list)
    matched_transaction_total: int = 0
    current_transactions_matched: int = 0
    comparison_transactions_matched: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_skipped: int = 0


@dataclass
class FluxGenerationDiagnostics:
    """Aggregate signals returned alongside the preview/commit rows so the
    admin UI can warn about unmapped captions and missing transactions.

    The class still exposes the original flat counters for back-compat
    with existing callers — but it ALSO carries a per-statement
    breakdown (`per_statement["pl"]`, `per_statement["bs"]`) so a PL-only
    failure is no longer hidden behind a combined total dominated by BS.
    """
    captions_total: int = 0
    captions_with_mappings: int = 0
    captions_without_mappings: list[str] = field(default_factory=list)
    captions_with_zero_transactions: list[str] = field(default_factory=list)
    matched_transaction_total: int = 0
    current_transactions_matched: int = 0
    comparison_transactions_matched: int = 0
    per_statement: dict[str, StatementDiagnostics] = field(default_factory=dict)

    def for_statement(self, statement: str) -> StatementDiagnostics:
        """Return (creating if absent) the per-statement diagnostic bucket."""
        key = normalize_statement_key(statement) or statement
        bucket = self.per_statement.get(key)
        if bucket is None:
            bucket = StatementDiagnostics(statement=key)
            self.per_statement[key] = bucket
        return bucket


# ── Statement / account-number normalisation ──────────────────────────────
# Single source of truth — the API surface, the supervisor UI, the seed
# loader, and the BC generator all flow through these helpers so a future
# `"P&L"` / `"PL"` / `"profit_loss"` legacy value never breaks PL rendering.
_STATEMENT_ALIASES: dict[str, str] = {
    "pl": "pl", "p&l": "pl", "p_l": "pl", "pnl": "pl",
    "profit_loss": "pl", "profit_and_loss": "pl",
    "income_statement": "pl",
    "bs": "bs", "balance_sheet": "bs", "b/s": "bs",
}


def normalize_statement_key(value: Any) -> Optional[str]:
    """Canonicalise a statement string to ``"pl"`` / ``"bs"`` / ``"both"``.

    Returns ``None`` on unknown input so callers can map that to a
    safe ``STATEMENT_KEY_MISMATCH`` error without leaking the raw value.
    """
    if value is None:
        return None
    s = str(value).strip().lower().replace("&", "_").replace(" ", "_")
    if s == "both":
        return "both"
    return _STATEMENT_ALIASES.get(s)


# Account-number tokens occasionally arrive carrying the human-readable
# label appended (e.g. ``"6100 - Revenue"``, ``"6100 \u2014 Revenue"``).
# We split on whitespace + dash/em-dash to recover the leading code so the
# matcher stays tolerant of imports / hand-typed values without the user
# having to clean their data first.
_LABEL_SEPARATOR_RE = re.compile(r"[\s]*[-\u2013\u2014][\s]+")


def normalize_account_number_token(value: Any) -> Optional[str]:
    """Clean a single GL-account-number token. Returns ``None`` when
    nothing usable remains.

      • strips surrounding whitespace and non-breaking spaces,
      • splits on the first ``"<code> - <label>"`` separator and keeps
        the code,
      • returns the cleaned string unchanged when no separator is found.
    """
    if value is None:
        return None
    s = str(value).replace("\u00a0", " ").strip()
    if not s:
        return None
    parts = _LABEL_SEPARATOR_RE.split(s, maxsplit=1)
    head = parts[0].strip()
    return head or None


# ── Period parsing ─────────────────────────────────────────────────────────
# The detailed parser lives in `app.services.flux_period_config` so the
# generator, the diagnostics endpoint, and the supervisor save-and-generate
# route share exactly the same date math. This thin wrapper preserves the
# legacy `parse_entity_period(...)` signature so older callers (admin
# router, scheduler) keep working without churn.
def parse_entity_period(period_str: str) -> tuple[date, date]:
    """Parse a T-Flux period label into an inclusive ``(start, end)`` range.

    Supports single labels (``"Mar 2026"``, ``"Q1 2026"``, ``"2026"``,
    ``"2026-03"``, ``"2026-03-15"``), explicit ranges (``"Jan 2026 to
    Mar 2026"``), and CSVs emitted by the supervisor MonthPicker
    (``"2025-01, 2025-02"``). Raises :class:`ValueError` when the string
    is unrecognisable — callers map that to a safe error code without
    leaking internals."""
    parsed = parse_period_string(period_str)
    if parsed is None:
        raise ValueError(f"Unrecognised period format: {period_str!r}")
    return parsed


# ── Mapping extractors ─────────────────────────────────────────────────────
# Multiple BC payload shapes are accepted because the ontology UI may store
# the user's selection as bare strings, dicts with `account_no`, or
# normalized rows from an import — the extractors normalise all of them
# back to a single set of strings.
_GL_ID_KEYS: tuple[str, ...] = (
    "account_no", "accountNumber", "accountNo", "number", "code",
    "value", "no", "external_id", "id",
)

_DIM_ID_KEYS: tuple[str, ...] = (
    "value_code", "valueCode", "code", "value", "id",
)


def extract_mapped_gl_account_nos(erp_gl_json: Any) -> set[str]:
    """Extract GL account_no strings from an OntologyMapping ``erp_gl_json``
    list. Returns an empty set when the input is missing or malformed —
    callers should treat that as “no mapping yet”."""
    return _extract_codes(erp_gl_json, _GL_ID_KEYS)


def extract_mapped_dimension_values(erp_subgl_json: Any) -> set[str]:
    """Legacy dimension codes (deprecated UI)."""
    return _extract_codes(erp_subgl_json, _DIM_ID_KEYS)


def _extract_codes(raw: Any, keys: tuple[str, ...]) -> set[str]:
    """Pull the candidate GL/dimension codes out of an ontology mapping
    payload. Tolerates strings (``"6100"``), dict shapes
    (``{"account_no": "6100"}``), and label strings
    (``"6100 - Revenue"``) — the latter recovered via
    :func:`normalize_account_number_token`."""
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            cleaned = normalize_account_number_token(item)
            if cleaned:
                out.add(cleaned)
            continue
        if isinstance(item, dict):
            for key in keys:
                val = item.get(key)
                if not val:
                    continue
                cleaned = normalize_account_number_token(val)
                if cleaned:
                    out.add(cleaned)
                break
    return out


# ── Transaction matching ───────────────────────────────────────────────────
@dataclass(frozen=True)
class GlAccountIdentitySet:
    """Bundle of identifiers that can map an ontology selection back to the
    actual rows in ``erp_general_ledger_entries``.

    Different BC environments expose different fields on a GL entry:
      • some include ``accountNumber`` → maps to ``account_no``,
      • some only include the linked ``account.id`` GUID → maps to
        ``account_id``,
      • the catalog also stores ``external_id`` (BC accounts.id).

    We pre-resolve all three sets via :func:`resolve_gl_account_identity`
    and OR-match transactions on whichever column happens to be populated.
    Without this, an environment that returns entries with no
    ``accountNumber`` produces zero matches even though the GL chart is
    correctly mapped.
    """
    account_nos:  frozenset[str]
    account_ids:  frozenset[str]
    external_ids: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not (self.account_nos or self.account_ids or self.external_ids)


def resolve_gl_account_identity(
    db: Session, entity_id: str, mapping_codes: set[str],
    *, erp_type: str = "bc",
) -> GlAccountIdentitySet:
    """Translate ontology-selected codes into the full set of identifiers
    we can match transactions on.

    The supervisor picks an ``account_no`` in OntologyMultiSelect, but BC
    GL entries from some environments only carry ``account.id``. By
    joining the catalog (``erp_gl_accounts``) we widen the input codes
    into ``(account_no, account_id, external_id)`` so OR-matching covers
    every shape.
    """
    if not mapping_codes:
        return GlAccountIdentitySet(frozenset(), frozenset(), frozenset())

    accounts = (
        db.query(ErpGlAccount)
        .filter(
            ErpGlAccount.entity_id == entity_id,
            ErpGlAccount.erp_type == erp_type,
            or_(
                ErpGlAccount.account_no.in_(mapping_codes),
                ErpGlAccount.external_id.in_(mapping_codes),
            ),
        )
        .all()
    )

    nos:  set[str] = {c for c in mapping_codes if c}
    ids:  set[str] = set()
    exts: set[str] = set()
    for acct in accounts:
        if acct.account_no:
            nos.add(str(acct.account_no))
        if acct.external_id:
            ids.add(str(acct.external_id))
            exts.add(str(acct.external_id))
    return GlAccountIdentitySet(
        account_nos=frozenset(nos),
        account_ids=frozenset(ids),
        external_ids=frozenset(exts),
    )


def match_transactions_for_caption(
    db: Session,
    entity_id: str,
    account_nos: set[str] | GlAccountIdentitySet,
    period_start: date,
    period_end: date,
    *,
    erp_type: str = "bc",
    calculation_mode: str = "net_change",
    subledger_codes: set[str] | None = None,
    dimension_codes: set[str] | None = None,
    limit: Optional[int] = None,
) -> list[ErpGeneralLedgerEntry]:
    """Return GL entries for mapped accounts in the date window.

    ``calculation_mode``:
      • ``net_change`` (P&L): ``posting_date`` between start and end.
      • ``balance_at_date`` (BS): cumulative through ``period_end`` only.

    Optional ``subledger_codes`` further filter by customer/vendor tokens
    from ``erp_subledger_json``. Document-only selections are not supported.
    """
    subledger_codes = subledger_codes or dimension_codes
    if isinstance(account_nos, GlAccountIdentitySet):
        identity = account_nos
    else:
        identity = GlAccountIdentitySet(
            account_nos=frozenset(account_nos or ()),
            account_ids=frozenset(),
            external_ids=frozenset(),
        )
    if identity.is_empty:
        return []

    or_clauses = []
    if identity.account_nos:
        or_clauses.append(ErpGeneralLedgerEntry.account_no.in_(identity.account_nos))
    if identity.account_ids:
        or_clauses.append(ErpGeneralLedgerEntry.account_id.in_(identity.account_ids))
    if identity.external_ids:
        or_clauses.append(
            ErpGeneralLedgerEntry.external_id.in_(identity.external_ids),
        )

    date_filters = [
        ErpGeneralLedgerEntry.entity_id == entity_id,
        ErpGeneralLedgerEntry.erp_type == erp_type,
        or_(*or_clauses),
        ErpGeneralLedgerEntry.posting_date <= period_end,
    ]
    if calculation_mode != "balance_at_date":
        date_filters.append(ErpGeneralLedgerEntry.posting_date >= period_start)

    q = (
        db.query(ErpGeneralLedgerEntry)
        .filter(*date_filters)
        .order_by(ErpGeneralLedgerEntry.posting_date.desc())
    )
    if limit and limit > 0:
        q = q.limit(limit)
    rows = q.all()
    if subledger_codes:
        return [r for r in rows if entry_matches_subledger(r, subledger_codes)]
    return rows


def _sum_amounts(entries: Iterable[ErpGeneralLedgerEntry]) -> Decimal:
    total = Decimal("0")
    for e in entries:
        if e.amount is None:
            continue
        total += Decimal(str(e.amount))
    return total


# ── Variance math ──────────────────────────────────────────────────────────
def calculate_pct_variance(
    current: Decimal, comp: Decimal,
) -> Optional[Decimal]:
    """((current − comp) / |comp|) × 100. Returns 0 when both are zero,
    None when comp is zero but current is not (so the UI shows ``n/a``
    instead of an infinity)."""
    if not comp:
        if not current:
            return Decimal("0")
        return None
    return ((current - comp) / abs(comp)) * Decimal("100")


def apply_threshold(
    variance: Decimal, pct_variance: Optional[Decimal], threshold: Threshold,
) -> bool:
    """Return True if the flux row crosses the entity threshold."""
    abs_breach = abs(variance) >= Decimal(str(threshold.abs_value))
    pct_breach = (
        pct_variance is not None
        and abs(pct_variance) >= Decimal(str(threshold.pct_value))
    )
    if threshold.mode == "pct":
        return pct_breach
    if threshold.mode == "abs":
        return abs_breach
    if threshold.logic == "and":
        return abs_breach and pct_breach
    return abs_breach or pct_breach


# ── Preview row builder ────────────────────────────────────────────────────
@dataclass
class _CalcCtx:
    """Bundle the per-mapping calculation inputs so `_build_preview_row`
    stays small and SonarQube-friendly (no 7-arg fn)."""
    current_range: tuple[date, date]
    comp_range: tuple[date, date]
    existing_keys: set[tuple[str, str]]
    threshold: Optional[Threshold]


@dataclass
class _BuiltRow:
    """Wrapper so the diagnostics builder can read per-period match counts
    without re-querying transactions."""
    preview:                FluxRowPreview
    current_match_count:    int
    comparison_match_count: int


def _build_preview_row(
    db: Session, entity: Entity, mapping: OntologyMapping, ctx: _CalcCtx,
    *, erp_type: str = "bc",
) -> _BuiltRow:
    raw_codes = extract_mapped_gl_account_nos(mapping.erp_gl_json)
    sub_codes = extract_mapped_subledger_values(mapping)
    mode = calculation_mode_for_statement(mapping.statement)
    identity = resolve_gl_account_identity(db, entity.id, raw_codes, erp_type=erp_type)

    current_entries = match_transactions_for_caption(
        db, entity.id, identity,
        ctx.current_range[0], ctx.current_range[1],
        erp_type=erp_type,
        calculation_mode=mode,
        subledger_codes=sub_codes or None,
    )
    comp_entries = match_transactions_for_caption(
        db, entity.id, identity,
        ctx.comp_range[0], ctx.comp_range[1],
        erp_type=erp_type,
        calculation_mode=mode,
        subledger_codes=sub_codes or None,
    )
    party_pool: list[ErpGeneralLedgerEntry] = []
    if sub_codes:
        party_pool = (
            match_transactions_for_caption(
                db, entity.id, identity,
                ctx.current_range[0], ctx.current_range[1],
                erp_type=erp_type,
                calculation_mode=mode,
            )
            + match_transactions_for_caption(
                db, entity.id, identity,
                ctx.comp_range[0], ctx.comp_range[1],
                erp_type=erp_type,
                calculation_mode=mode,
            )
        )

    current_value = _sum_amounts(current_entries)
    comp_value    = _sum_amounts(comp_entries)
    pct_variance  = calculate_pct_variance(current_value, comp_value)

    is_material = False
    if ctx.threshold:
        mat_var, mat_pct = finance_variance_for_threshold(
            current_value,
            comp_value,
            mapping.tflux_caption,
            mapping.statement,
        )
        is_material = apply_threshold(mat_var, mat_pct, ctx.threshold)

    skipped: Optional[str] = None
    if not raw_codes:
        skipped = SKIP_NO_MANUAL_GL
    elif not current_entries and not comp_entries:
        skipped = SKIP_NO_TRANSACTIONS

    warnings: list[str] = [skipped] if skipped else []
    gl_list = sorted(raw_codes)
    sub_list = sorted(sub_codes) if sub_codes else []
    if sub_codes:
        party_warn = subledger_filter_warning(sub_codes, party_pool)
        if party_warn:
            warnings.append(party_warn)
        elif not current_entries and not comp_entries:
            warnings.append(
                "Selected customer/vendor ledger entries did not match any "
                "transactions in the mapped GL accounts.",
            )

    preview = FluxRowPreview(
        statement=mapping.statement,
        caption=mapping.tflux_caption,
        current_value=current_value,
        comp_value=comp_value,
        pct_variance=pct_variance,
        is_material=is_material,
        is_bold=False,
        sort_order=mapping.sort_order,
        had_existing_row=(mapping.statement, mapping.tflux_caption) in ctx.existing_keys,
        matched_account_count=len(raw_codes),
        matched_transaction_count=len(current_entries) + len(comp_entries),
        skipped_reason=skipped,
        selected_gl_accounts=gl_list,
        selected_subledger=sub_list,
        warnings=warnings,
    )
    return _BuiltRow(
        preview=preview,
        current_match_count=len(current_entries),
        comparison_match_count=len(comp_entries),
    )


# ── Public preview / commit entry points ───────────────────────────────────
def preview_flux_generation(
    db: Session,
    entity: Entity,
    statement_filter: str,
    *,
    erp_type: str = "bc",
) -> tuple[list[FluxRowPreview], FluxGenerationDiagnostics]:
    """Build preview rows + diagnostics for the entity's active period.
    No DB writes — safe to call repeatedly. Returns rows in caption order
    so the admin UI can render them directly.

    Period convention (matches the seed loader and the FluxReview UI):
        ``entity.period_from`` → CURRENT period
        ``entity.period_to``   → COMPARISON period

    The actual ``(start, end)`` dates are resolved through
    :func:`parse_flux_period_config`, which understands single labels,
    explicit ranges, and CSVs emitted by the supervisor MonthPicker.
    """
    period = parse_flux_period_config(entity)
    current_range = (period.current_start_date, period.current_end_date)
    comp_range    = (period.comparison_start_date, period.comparison_end_date)

    mapping_q = (
        db.query(OntologyMapping)
        .filter(
            OntologyMapping.entity_id == entity.id,
            OntologyMapping.is_hidden.is_(False),
        )
    )
    if statement_filter != "both":
        mapping_q = mapping_q.filter(OntologyMapping.statement == statement_filter)
    mappings = mapping_q.order_by(
        OntologyMapping.statement, OntologyMapping.sort_order,
    ).all()

    thresholds: dict[str, Threshold] = {
        t.statement: t
        for t in db.query(Threshold).filter(Threshold.entity_id == entity.id).all()
    }

    existing_keys: set[tuple[str, str]] = set(
        db.query(FluxData.statement, FluxData.caption)
        .filter(
            FluxData.entity_id == entity.id,
            FluxData.period_from == entity.period_from,
            FluxData.period_to == entity.period_to,
        )
        .all()
    )

    rows: list[FluxRowPreview] = []
    diag = FluxGenerationDiagnostics(captions_total=len(mappings))

    for mapping in mappings:
        ctx = _CalcCtx(
            current_range=current_range,
            comp_range=comp_range,
            existing_keys=existing_keys,
            threshold=thresholds.get(mapping.statement),
        )
        built = _build_preview_row(db, entity, mapping, ctx, erp_type=erp_type)
        rows.append(built.preview)

        # Update both the flat counters AND the per-statement bucket so
        # the supervisor UI can render breakdowns like "PL: 0 / 8 mapped"
        # while the back-compat counters keep older callers working.
        per = diag.for_statement(mapping.statement)
        per.captions_total += 1

        if built.preview.matched_account_count > 0:
            diag.captions_with_mappings += 1
            per.captions_with_mappings += 1
        else:
            diag.captions_without_mappings.append(mapping.tflux_caption)
            per.captions_without_mappings.append(mapping.tflux_caption)
        if (
            built.preview.matched_transaction_count == 0
            and built.preview.matched_account_count > 0
        ):
            diag.captions_with_zero_transactions.append(mapping.tflux_caption)
            per.captions_with_zero_transactions.append(mapping.tflux_caption)
        diag.matched_transaction_total += built.preview.matched_transaction_count
        diag.current_transactions_matched += built.current_match_count
        diag.comparison_transactions_matched += built.comparison_match_count
        per.matched_transaction_total += built.preview.matched_transaction_count
        per.current_transactions_matched += built.current_match_count
        per.comparison_transactions_matched += built.comparison_match_count

    return rows, diag


def upsert_bc_generated_flux_rows(
    db: Session,
    entity: Entity,
    rows: list[FluxRowPreview],
    *,
    overwrite_existing: bool,
    diag: Optional["FluxGenerationDiagnostics"] = None,
) -> tuple[int, int, int]:
    """Upsert BC-generated flux_data rows. Returns
    ``(inserted, updated, skipped)``.

    Behaviour:
      • Inserts captions that don't exist yet for the active period.
      • Updates existing rows when ``overwrite_existing=True``.
      • Skips existing rows otherwise — and surfaces the count so the UI
        can warn that some captions were preserved.
      • Stamps ``source_type='business_central'`` and ``generated_at`` on
        every written/updated row so Flux Review can label provenance.
      • NEVER deletes rows (even seed rows) — comments / signoffs / AI
        commentary keyed by caption stay intact across regenerations.
    """
    existing_map: dict[tuple[str, str], FluxData] = {
        (r.statement, r.caption): r
        for r in db.query(FluxData).filter(
            FluxData.entity_id == entity.id,
            FluxData.period_from == entity.period_from,
            FluxData.period_to == entity.period_to,
        ).all()
    }

    inserted = updated = skipped = 0
    now = datetime.utcnow()

    def _bump(stmt: str, field_name: str) -> None:
        """Update the per-statement counter on the optional diagnostics
        object so the response can break inserts/updates/skips out by
        statement. Never raises when ``diag`` is None (legacy callers)."""
        if diag is None:
            return
        bucket = diag.for_statement(stmt)
        setattr(bucket, field_name, getattr(bucket, field_name, 0) + 1)

    for row in rows:
        # Captions with no GL mapping or no transactions deserve a flux row
        # only when they previously existed — otherwise we'd silently
        # insert zero-value placeholders.
        if row.skipped_reason and (row.statement, row.caption) not in existing_map:
            skipped += 1
            _bump(row.statement, "rows_skipped")
            continue

        existing = existing_map.get((row.statement, row.caption))

        if existing is not None and not overwrite_existing:
            skipped += 1
            _bump(row.statement, "rows_skipped")
            continue

        if existing is None:
            db.add(FluxData(
                entity_id=entity.id,
                statement=row.statement,
                period_from=entity.period_from,
                period_to=entity.period_to,
                caption=row.caption,
                current_value=row.current_value,
                comp_value=row.comp_value,
                pct_variance=row.pct_variance,
                is_material=row.is_material,
                is_bold=row.is_bold,
                sort_order=row.sort_order,
                source_type=BC_SOURCE_TYPE,
                generated_from=BC_GENERATED_FROM,
                generated_at=now,
            ))
            inserted += 1
            _bump(row.statement, "rows_inserted")
        else:
            existing.current_value = row.current_value
            existing.comp_value    = row.comp_value
            existing.pct_variance  = row.pct_variance
            existing.is_material   = row.is_material
            existing.source_type   = BC_SOURCE_TYPE
            existing.generated_from = BC_GENERATED_FROM
            existing.generated_at  = now
            updated += 1
            _bump(row.statement, "rows_updated")

    return inserted, updated, skipped


def upsert_qb_generated_flux_rows(
    db: Session,
    entity: Entity,
    rows: list[FluxRowPreview],
    *,
    overwrite_existing: bool,
    diag: Optional["FluxGenerationDiagnostics"] = None,
) -> tuple[int, int, int]:
    """Upsert QuickBooks-generated flux_data rows (same rules as BC path)."""
    existing_map: dict[tuple[str, str], FluxData] = {
        (r.statement, r.caption): r
        for r in db.query(FluxData).filter(
            FluxData.entity_id == entity.id,
            FluxData.period_from == entity.period_from,
            FluxData.period_to == entity.period_to,
        ).all()
    }

    inserted = updated = skipped = 0
    now = datetime.utcnow()

    def _bump(stmt: str, field_name: str) -> None:
        if diag is None:
            return
        bucket = diag.for_statement(stmt)
        setattr(bucket, field_name, getattr(bucket, field_name, 0) + 1)

    for row in rows:
        if row.skipped_reason and (row.statement, row.caption) not in existing_map:
            skipped += 1
            _bump(row.statement, "rows_skipped")
            continue

        existing = existing_map.get((row.statement, row.caption))

        if existing is not None and not overwrite_existing:
            skipped += 1
            _bump(row.statement, "rows_skipped")
            continue

        if existing is None:
            db.add(FluxData(
                entity_id=entity.id,
                statement=row.statement,
                period_from=entity.period_from,
                period_to=entity.period_to,
                caption=row.caption,
                current_value=row.current_value,
                comp_value=row.comp_value,
                pct_variance=row.pct_variance,
                is_material=row.is_material,
                is_bold=row.is_bold,
                sort_order=row.sort_order,
                source_type=QB_SOURCE_TYPE,
                generated_from=QB_GENERATED_FROM,
                generated_at=now,
            ))
            inserted += 1
            _bump(row.statement, "rows_inserted")
        else:
            existing.current_value = row.current_value
            existing.comp_value    = row.comp_value
            existing.pct_variance  = row.pct_variance
            existing.is_material   = row.is_material
            existing.source_type   = QB_SOURCE_TYPE
            existing.generated_from = QB_GENERATED_FROM
            existing.generated_at  = now
            updated += 1
            _bump(row.statement, "rows_updated")

    return inserted, updated, skipped


# ── Diagnostics helpers ────────────────────────────────────────────────────
def detect_seed_flux_rows(
    db: Session, entity_id: str,
) -> list[FluxData]:
    """Return rows that look like demo/seed data.

    A row is treated as seed when:
      • ``source_type`` IS NULL (legacy rows from the seed loader), OR
      • ``source_type = 'seed'``.
    """
    rows = (
        db.query(FluxData)
        .filter(FluxData.entity_id == entity_id)
        .filter((FluxData.source_type.is_(None)) | (FluxData.source_type == "seed"))
        .all()
    )
    return rows


# ── Backwards-compatible aliases ───────────────────────────────────────────
# Some existing tests / callers import the older names. We keep them as
# thin wrappers so this refactor is non-breaking.
def upsert_flux_data_rows(
    db: Session,
    entity: Entity,
    rows: list[FluxRowPreview],
    overwrite_demo: bool,
) -> tuple[int, int, int]:
    """Legacy name. New callers should use
    :func:`upsert_bc_generated_flux_rows` directly."""
    return upsert_bc_generated_flux_rows(
        db, entity, rows, overwrite_existing=overwrite_demo,
    )


# ── BC date-range probe + selected-period validation ──────────────────────
@dataclass(frozen=True)
class BcTransactionDateRange:
    """Synced BC transaction window for one entity.
    ``oldest`` / ``latest`` are ``None`` when the entity has zero rows."""
    oldest: Optional[date]
    latest: Optional[date]
    entry_count: int

    @property
    def is_empty(self) -> bool:
        return self.entry_count == 0 or self.oldest is None or self.latest is None


def get_bc_transaction_date_range(
    db: Session, entity_id: str,
) -> BcTransactionDateRange:
    """Inspect ``erp_general_ledger_entries`` for the entity and return the
    earliest / latest posting date plus the row count. Used by the
    diagnostics endpoint and by selected-period validation."""
    from sqlalchemy import func as sa_func
    row = (
        db.query(
            sa_func.min(ErpGeneralLedgerEntry.posting_date),
            sa_func.max(ErpGeneralLedgerEntry.posting_date),
            sa_func.count(ErpGeneralLedgerEntry.id),
        )
        .filter(ErpGeneralLedgerEntry.entity_id == entity_id)
        .first()
    )
    if row is None:
        return BcTransactionDateRange(None, None, 0)
    oldest, latest, count = row
    return BcTransactionDateRange(oldest, latest, int(count or 0))


@dataclass(frozen=True)
class PeriodValidationOutcome:
    """Outcome of comparing a selected period config against the BC data
    range. ``warnings`` carries human-readable strings — we keep these in
    English so the route can pass them through unchanged."""
    is_within_bc_range: bool
    bc_range: BcTransactionDateRange
    warnings: list[str] = field(default_factory=list)


def validate_selected_period_against_bc_data(
    db: Session, entity_id: str, period: FluxPeriodConfig,
) -> PeriodValidationOutcome:
    """Confirm the selected period overlaps the entity's synced BC window.

    The check is informational — generation still runs even when the
    period falls outside the BC window. Empty match counts then surface
    as warnings on the per-row preview."""
    bc_range = get_bc_transaction_date_range(db, entity_id)
    warnings: list[str] = []

    if bc_range.is_empty:
        warnings.append(
            "No Business Central transactions are synced for this entity yet.",
        )
        return PeriodValidationOutcome(
            is_within_bc_range=False, bc_range=bc_range, warnings=warnings,
        )

    selected_start = min(period.current_start_date, period.comparison_start_date)
    selected_end   = max(period.current_end_date,   period.comparison_end_date)

    if selected_end < bc_range.oldest or selected_start > bc_range.latest:
        warnings.append(
            f"Selected period ({selected_start.isoformat()} to "
            f"{selected_end.isoformat()}) is outside the synced BC range "
            f"({bc_range.oldest.isoformat()} to {bc_range.latest.isoformat()}).",
        )
        return PeriodValidationOutcome(
            is_within_bc_range=False, bc_range=bc_range, warnings=warnings,
        )

    return PeriodValidationOutcome(
        is_within_bc_range=True, bc_range=bc_range, warnings=warnings,
    )


# ── Aggregated diagnostics ────────────────────────────────────────────────
@dataclass
class FluxGenerationDiagnosticsReport:
    """Composite payload returned by the ``flux-generation-diagnostics``
    endpoint. Mirrors the safe-serialisable subset that the supervisor /
    admin UI renders."""
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

    bc_oldest_posting_date: Optional[date]
    bc_latest_posting_date: Optional[date]
    bc_entry_count: int
    is_selected_range_within_bc_data: bool
    warnings: list[str]

    ontology_mappings_total: int
    captions_with_mapped_gl: int
    captions_without_mapped_gl: list[str]
    gl_entries_in_current_range: int
    gl_entries_in_comparison_range: int
    flux_rows_for_selected_period: int
    statement_filter: Optional[str] = None
    captions: list[dict[str, Any]] = field(default_factory=list)


def _count_entries_in_range(
    db: Session, entity_id: str, start: date, end: date,
) -> int:
    return (
        db.query(ErpGeneralLedgerEntry)
        .filter(
            ErpGeneralLedgerEntry.entity_id == entity_id,
            ErpGeneralLedgerEntry.posting_date >= start,
            ErpGeneralLedgerEntry.posting_date <= end,
        )
        .count()
    )


def build_flux_generation_diagnostics(
    db: Session, entity: Entity, *,
    statement_filter: Optional[str] = None,
) -> FluxGenerationDiagnosticsReport:
    """Aggregate every signal a supervisor needs to debug an empty Flux
    Review: parsed period, BC sync window, ontology coverage, and the
    flux_data row count for the selected period.

    When ``statement_filter`` is supplied (``"pl"`` or ``"bs"``) the
    response narrows the ontology mapping count + caption-level breakdown
    to that statement only — used by the supervisor PL-empty diagnosis
    UI without scanning BS captions for nothing.
    """
    normalized_filter: Optional[str] = None
    if statement_filter is not None and statement_filter != "both":
        normalized_filter = normalize_statement_key(statement_filter)

    period = parse_flux_period_config(entity)
    validation = validate_selected_period_against_bc_data(
        db, entity.id, period,
    )

    mapping_q = db.query(OntologyMapping).filter(
        OntologyMapping.entity_id == entity.id,
    )
    if normalized_filter:
        mapping_q = mapping_q.filter(OntologyMapping.statement == normalized_filter)
    mappings = mapping_q.all()

    captions_with_gl = 0
    captions_without_gl: list[str] = []
    caption_details: list[dict[str, Any]] = []
    for m in mappings:
        codes = extract_mapped_gl_account_nos(m.erp_gl_json)
        sub_codes = extract_mapped_subledger_values(m)
        mode = calculation_mode_for_statement(m.statement)
        identity = resolve_gl_account_identity(db, entity.id, codes)
        cur = match_transactions_for_caption(
            db, entity.id, identity,
            period.current_start_date, period.current_end_date,
            calculation_mode=mode,
            subledger_codes=sub_codes or None,
        ) if identity and not identity.is_empty else []
        comp = match_transactions_for_caption(
            db, entity.id, identity,
            period.comparison_start_date, period.comparison_end_date,
            calculation_mode=mode,
            subledger_codes=sub_codes or None,
        ) if identity and not identity.is_empty else []
        caption_details.append({
            "statement":                       m.statement,
            "caption":                         m.tflux_caption,
            "calculation_mode":                mode,
            "mapped_gl_accounts":              sorted(codes),
            "selected_subledger":              sorted(sub_codes),
            "matched_current_transactions":    len(cur),
            "matched_comparison_transactions": len(comp),
            "current_value":                   float(_sum_amounts(cur)),
            "comparison_value":                float(_sum_amounts(comp)),
        })
        if codes:
            captions_with_gl += 1
        else:
            captions_without_gl.append(m.tflux_caption)

    flux_rows_q = db.query(FluxData).filter(
        FluxData.entity_id == entity.id,
        FluxData.period_from == entity.period_from,
        FluxData.period_to == entity.period_to,
    )
    if normalized_filter:
        flux_rows_q = flux_rows_q.filter(FluxData.statement == normalized_filter)
    flux_rows_count = flux_rows_q.count()

    return FluxGenerationDiagnosticsReport(
        entity_id=entity.id,
        period_type=entity.period_type,
        period_from_raw=period.current_period_raw,
        period_to_raw=period.comparison_period_raw,
        current_label=period.current_label,
        comparison_label=period.comparison_label,
        current_start_date=period.current_start_date,
        current_end_date=period.current_end_date,
        comparison_start_date=period.comparison_start_date,
        comparison_end_date=period.comparison_end_date,
        bc_oldest_posting_date=validation.bc_range.oldest,
        bc_latest_posting_date=validation.bc_range.latest,
        bc_entry_count=validation.bc_range.entry_count,
        is_selected_range_within_bc_data=validation.is_within_bc_range,
        warnings=validation.warnings,
        ontology_mappings_total=len(mappings),
        captions_with_mapped_gl=captions_with_gl,
        captions_without_mapped_gl=captions_without_gl,
        gl_entries_in_current_range=_count_entries_in_range(
            db, entity.id,
            period.current_start_date, period.current_end_date,
        ),
        statement_filter=normalized_filter,
        captions=caption_details,
        gl_entries_in_comparison_range=_count_entries_in_range(
            db, entity.id,
            period.comparison_start_date, period.comparison_end_date,
        ),
        flux_rows_for_selected_period=flux_rows_count,
    )


def build_statement_calculation_summaries(
    period: FluxPeriodConfig,
    diag: FluxGenerationDiagnostics,
    rows: list[FluxRowPreview] | None = None,
) -> list[dict[str, Any]]:
    """Per-statement calculation metadata for generate/diagnostics APIs."""
    summaries: list[dict[str, Any]] = []
    for stmt in ("pl", "bs"):
        bucket = diag.per_statement.get(stmt)
        if bucket is None:
            continue
        mode = calculation_mode_for_statement(stmt)
        stmt_rows = [r for r in (rows or []) if r.statement == stmt]
        sub_count = len({t for r in stmt_rows for t in r.selected_subledger})
        summaries.append({
            "statement": stmt,
            "calculation_mode": mode,
            "calculation_label": calculation_mode_label(mode),
            "current_start_date": period.current_start_date,
            "current_end_date": period.current_end_date,
            "comparison_start_date": period.comparison_start_date,
            "comparison_end_date": period.comparison_end_date,
            "mapped_gl_accounts_count": bucket.captions_with_mappings,
            "selected_subledger_count": sub_count,
            "matched_transactions": bucket.matched_transaction_total,
            "rows_generated": bucket.rows_inserted + bucket.rows_updated,
            "warnings": [],
        })
    return summaries
