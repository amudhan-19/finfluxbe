# @file    services/ontology_automap.py
#
# Demo auto-map service — **disabled at the HTTP layer**. Kept for unit
# tests and historical reference only; production mapping must be manual.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.models import (
    ErpDimensionValue,
    ErpGeneralLedgerEntry,
    ErpGlAccount,
    OntologyMapping,
)


# ── Keyword catalogue ───────────────────────────────────────────────────────
# Captions are normalized to lowercase before lookup so the mapping survives
# minor caption renames. Multiple keyword phrases per caption are AND-free
# (any match wins) so a chart of accounts with creative naming still resolves.
#
# Order matters when two captions overlap (e.g. "professional services" vs
# "service revenue"); we sort matches per account so the most-specific
# caption wins via the longest-match rule in `_score_match`.
KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    # ── Profit & Loss
    "Revenue":               ("sales, retail", "sales revenue", "sales local",
                              "sales export", "consulting revenue",
                              "service revenue", "product revenue"),
    "Cost of Revenue":       ("cost of sales", "cost of revenue",
                              "direct cost", "cogs"),
    "Salaries & Wages":      ("salary", "salaries", "wages", "payroll"),
    "Salaries and Wages":    ("salary", "salaries", "wages", "payroll"),
    "Professional Services": ("professional", "consulting expense", "legal",
                              "audit", "advisory"),
    "Software & Technology": ("software", "technology", "subscription",
                              "saas", "it expense", "it cost"),
    "Travel & Entertainment":("travel", "entertainment", "meals", "lodging"),
    "Depreciation":          ("depreciation",),
    "Amortization":          ("amortization", "amortisation"),
    "Interest Expense":      ("interest expense", "interest"),
    "Interest Income":       ("interest income",),
    "Income Tax":            ("income tax", "tax expense", "corporate tax"),
    "Marketing":             ("marketing", "advertising", "promotion"),
    "Rent":                  ("rent", "lease"),
    "Utilities":             ("utility", "utilities", "electric", "water",
                              "gas"),
    "Insurance":             ("insurance",),
    "Other Operating Expenses": ("other operating", "miscellaneous expense",
                                 "general expense"),

    # ── Balance Sheet
    "Cash":                  ("cash", "bank", "petty cash"),
    "Accounts Receivable":   ("receivable", "debtors", "ar "),
    "Prepaid Expenses":      ("prepaid", "prepayment"),
    "Inventory":             ("inventory", "stock"),
    "Fixed Assets / PP&E":   ("fixed asset", "property", "plant", "equipment",
                              "ppe"),
    "Fixed Assets":          ("fixed asset", "property", "plant", "equipment",
                              "ppe"),
    "Accounts Payable":      ("payable", "creditors", "ap "),
    "Accrued Expenses":      ("accrued", "accrual"),
    "Loans / Borrowings":    ("loan", "borrowing", "debt", "note payable"),
    "Loans":                 ("loan", "borrowing", "debt"),
    "Retained Earnings":     ("retained earnings",),
    "Common Stock":          ("common stock", "share capital", "capital stock"),
    "Equity":                ("equity",),
    "Long-term Debt":        ("long-term debt", "long term debt",
                              "bonds payable"),
}


# Dimension code keywords used when an entity has dimensions synced. Optional;
# if no dimensions match, mappings still proceed without sub-GL data.
DIMENSION_KEYWORDS: tuple[str, ...] = (
    "area", "department", "region", "business group", "cost center",
    "location",
)


# ── Result types ───────────────────────────────────────────────────────────
@dataclass
class AutoMapSkipStats:
    """Per-caption counts of accounts rejected by demo quality rules."""
    skipped_subtotal_accounts: int = 0
    skipped_non_posting_accounts: int = 0
    skipped_suspicious_category_accounts: int = 0


@dataclass
class AutoMapEntry:
    """One caption-level outcome of the auto-map run."""
    statement: str
    tflux_caption: str
    matched_gl_accounts: list[str] = field(default_factory=list)
    matched_dimension_values: list[str] = field(default_factory=list)
    is_new_mapping: bool = False
    skipped_reason: str | None = None
    mapped_accounts_count: int = 0
    matched_transaction_count: int = 0
    skipped_subtotal_accounts: int = 0
    skipped_non_posting_accounts: int = 0
    skipped_suspicious_category_accounts: int = 0


@dataclass
class AutoMapResult:
    """Aggregate outcome returned to both the HTTP endpoint and the CLI."""
    entity_id: str
    statement: str
    dry_run: bool
    overwrite: bool
    captions_total: int = 0
    captions_mapped: int = 0
    captions_skipped: int = 0
    entries: list[AutoMapEntry] = field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────────
def _normalize(value: str | None) -> str:
    """Lower-case + trim helper used for keyword lookups. ``None`` becomes
    an empty string so callers can chain `_normalize(x) and ...`."""
    if not value:
        return ""
    return str(value).strip().lower()


def _score_match(account_name: str, keyword: str) -> int:
    """Tiny relevance score: prefer exact substring matches over loose ones,
    and longer keywords over shorter so "interest expense" beats a bare
    "interest" hit on the same account."""
    if not account_name or not keyword:
        return 0
    name = _normalize(account_name)
    kw = _normalize(keyword)
    if kw == name:
        return 1000 + len(kw)
    if name.startswith(kw):
        return 500 + len(kw)
    if kw in name:
        return 100 + len(kw)
    return 0


def _normalize_type(value: str | None) -> str:
    """Normalize BC OData enum tokens like ``Begin_x002D_Total``."""
    v = _normalize(value)
    return v.replace("_x002d_", "-").replace("_x0020_", " ")


def _normalize_category(value: str | None) -> str:
    return _normalize(value).replace("_x0020_", " ")


_NON_POSTING_TYPES = frozenset({
    "heading",
    "total",
    "begin-total",
    "end-total",
})


def _account_display_name(acct: ErpGlAccount) -> str:
    return str(acct.account_name or acct.display_name or acct.account_no or "")


def _is_non_posting_account(acct: ErpGlAccount) -> bool:
    """Demo mappings should use leaf posting accounts only."""
    acct_type = _normalize_type(acct.account_type)
    if not acct_type:
        return False
    if acct_type == "posting":
        return False
    if acct_type in _NON_POSTING_TYPES:
        return True
    return "total" in acct_type


def _is_subtotal_account_name(name: str) -> bool:
    n = _normalize(name)
    if not n:
        return False
    if n.endswith(", total") or n.endswith(" total"):
        return True
    if "begin total" in n or "end total" in n:
        return True
    if n.startswith("total ") or n.endswith(" total"):
        return True
    return False


def _is_suspicious_for_caption(caption: str, acct: ErpGlAccount) -> bool:
    """Caption-aware exclusions for demo-quality direct mappings."""
    name = _normalize(_account_searchable(acct))
    cat = _normalize_category(acct.account_category)

    if caption == "Revenue":
        if cat in ("liabilities", "expense", "cost of goods sold"):
            return True
        if any(tok in name for tok in (
            "vat", "gst", "sales tax", "tax deposit", "tax payable",
            "total revenue", "unbilled revenue", "wip sales",
        )):
            return True
        return False

    if caption in ("Fixed Assets / PP&E", "Fixed Assets"):
        if cat == "expense":
            return True
        if "expense" in name and "asset" not in name:
            return True
        return False

    if caption == "Accounts Receivable":
        if any(tok in name for tok in ("consumable", "spare", "inventory", "cogs")):
            if "receivable" not in name and "debtor" not in name:
                return True
        return False

    if caption == "Inventory":
        if "receivable" in name and "inventory" not in name:
            return True
        return False

    if caption == "Cash":
        if cat == "expense":
            return True
        return False

    return False


def _evaluate_account_for_caption(
    acct: ErpGlAccount, caption: str,
) -> tuple[bool, Optional[str]]:
    """Return ``(eligible, skip_bucket)`` where skip_bucket is one of
    ``subtotal``, ``non_posting``, ``suspicious``."""
    if _is_non_posting_account(acct):
        return False, "non_posting"
    if _is_subtotal_account_name(_account_display_name(acct)):
        return False, "subtotal"
    if _is_suspicious_for_caption(caption, acct):
        return False, "suspicious"
    return True, None


def _count_transactions_for_accounts(
    db: Session, entity_id: str, account_nos: list[str],
) -> int:
    if not account_nos:
        return 0
    return int(
        db.query(func.count(ErpGeneralLedgerEntry.id))
        .filter(
            ErpGeneralLedgerEntry.entity_id == entity_id,
            ErpGeneralLedgerEntry.account_no.in_(account_nos),
        )
        .scalar() or 0
    )


def _keywords_for(caption: str) -> tuple[str, ...]:
    """Return keyword list for a caption, falling back to the caption itself
    so unknown custom captions still get a best-effort match attempt."""
    if caption in KEYWORD_MAP:
        return KEYWORD_MAP[caption]
    fallback = _normalize(caption)
    return (fallback,) if fallback else ()


def _account_searchable(acct: ErpGlAccount) -> str:
    """Concatenate the searchable text for one GL account row."""
    parts: list[str] = []
    for raw in (acct.account_name, acct.display_name, acct.account_no):
        if raw:
            parts.append(str(raw))
    return " ".join(parts)


def _score_account_against_caption(
    acct: ErpGlAccount, caption: str,
) -> int:
    """Score one (account, caption) pair using the caption's keyword list.
    Returns 0 when nothing matches. Centralized so the per-caption helper
    and the cross-caption resolver compute the same number."""
    haystack = _account_searchable(acct)
    if not haystack:
        return 0
    keywords = _keywords_for(caption)
    if not keywords:
        return 0
    return max((_score_match(haystack, kw) for kw in keywords), default=0)


def find_matching_gl_accounts(
    accounts: Iterable[ErpGlAccount], caption: str,
) -> list[str]:
    """Return GL account_no strings whose name/display_name match any of
    the caption's keywords. Sorted by descending relevance score; ties
    broken by account_no for determinism in tests.

    NOTE: this returns *all* accounts that match the caption — it is
    intentionally per-caption with no cross-caption deduplication. The
    higher-level :func:`auto_map_ontology_for_demo` does global resolution
    so a single account ends up under exactly one caption."""
    scored: list[tuple[int, str]] = []
    for acct in accounts:
        score = _score_account_against_caption(acct, caption)
        if score > 0:
            scored.append((score, str(acct.account_no)))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [acct_no for _, acct_no in scored]


def find_matching_dimension_values(
    dim_values: Iterable[ErpDimensionValue], caption: str,
) -> list[str]:
    """Return dimension value codes whose dimension_name / value_name match
    one of the well-known demo dimension keywords. Captions don't filter
    dimensions in the MVP — we attach the same set of common dimensions to
    each mapped caption so the demo at least *shows* sub-GL data when it's
    present."""
    del caption  # reserved for future per-caption dimension affinity
    seen: list[str] = []
    seen_codes: set[str] = set()
    for dv in dim_values:
        haystack_parts = [
            _normalize(dv.dimension_name),
            _normalize(dv.dimension_code),
            _normalize(dv.value_name),
        ]
        haystack = " ".join(p for p in haystack_parts if p)
        if not haystack:
            continue
        if any(kw in haystack for kw in DIMENSION_KEYWORDS):
            code = str(dv.value_code)
            if code and code not in seen_codes:
                seen.append(code)
                seen_codes.add(code)
    return seen


# ── Persistence ────────────────────────────────────────────────────────────
def _load_existing_mappings(
    db: Session, entity_id: str, statement_filter: str,
) -> dict[tuple[str, str], OntologyMapping]:
    q = db.query(OntologyMapping).filter(OntologyMapping.entity_id == entity_id)
    if statement_filter != "both":
        q = q.filter(OntologyMapping.statement == statement_filter)
    return {(m.statement, m.tflux_caption): m for m in q.all()}


def _captions_for_statement(statement: str) -> list[tuple[str, str]]:
    """Default caption catalogue used when the entity has no existing
    ontology rows yet. Pairs of (statement, caption). Conservative — only
    includes captions covered by KEYWORD_MAP so we never invent labels
    the analyst dashboard doesn't already render."""
    pl_captions = [
        "Revenue", "Cost of Revenue", "Salaries & Wages",
        "Professional Services", "Software & Technology",
        "Travel & Entertainment", "Depreciation", "Interest Expense",
        "Income Tax",
    ]
    bs_captions = [
        "Cash", "Accounts Receivable", "Prepaid Expenses", "Inventory",
        "Fixed Assets / PP&E", "Accounts Payable", "Accrued Expenses",
        "Loans / Borrowings", "Retained Earnings",
    ]
    if statement == "pl":
        return [("pl", c) for c in pl_captions]
    if statement == "bs":
        return [("bs", c) for c in bs_captions]
    return [("pl", c) for c in pl_captions] + [("bs", c) for c in bs_captions]


def auto_map_ontology_for_demo(
    db: Session,
    *,
    entity_id: str,
    statement: Literal["pl", "bs", "both"] = "both",
    overwrite: bool = False,
    dry_run: bool = True,
) -> AutoMapResult:
    """Build (and optionally persist) demo ontology mappings.

    Behaviour matrix
    -----------------
    ``dry_run=True``   ──▶ compute matches, never touches the DB.
    ``dry_run=False``  ──▶ upsert mappings; respects ``overwrite``.
    ``overwrite=False``──▶ existing rows with non-empty erp_gl_json are kept.
    ``overwrite=True`` ──▶ existing rows are replaced.

    The function is intentionally side-effect-free until the very last
    block so callers can render the result list (which captions matched,
    which were skipped) before committing.
    """
    accounts = db.query(ErpGlAccount).filter(
        ErpGlAccount.entity_id == entity_id,
    ).all()
    dim_values = db.query(ErpDimensionValue).filter(
        ErpDimensionValue.entity_id == entity_id,
    ).all()

    existing = _load_existing_mappings(db, entity_id, statement)

    # Captions to consider: union of (existing mappings ∩ statement filter)
    # and the static fallback list. Existing rows always win so we never
    # lose hand-edits, even when the same caption isn't in our static list.
    caption_pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key in existing.keys():
        if key not in seen:
            caption_pairs.append(key)
            seen.add(key)
    for pair in _captions_for_statement(statement):
        if pair not in seen:
            caption_pairs.append(pair)
            seen.add(pair)

    # Cross-caption resolution: an account belongs to the caption with the
    # highest score. Demo quality filters drop subtotal / non-posting /
    # caption-suspicious accounts before scoring.
    account_winners: dict[str, tuple[str, str]] = {}
    account_scores: dict[str, int] = {}
    skip_stats_by_caption: dict[tuple[str, str], AutoMapSkipStats] = {}

    for stmt, caption in caption_pairs:
        skip_stats_by_caption.setdefault((stmt, caption), AutoMapSkipStats())

    for stmt, caption in caption_pairs:
        stats = skip_stats_by_caption[(stmt, caption)]
        for acct in accounts:
            eligible, bucket = _evaluate_account_for_caption(acct, caption)
            if not eligible:
                if bucket == "subtotal":
                    stats.skipped_subtotal_accounts += 1
                elif bucket == "non_posting":
                    stats.skipped_non_posting_accounts += 1
                elif bucket == "suspicious":
                    stats.skipped_suspicious_category_accounts += 1
                continue
            score = _score_account_against_caption(acct, caption)
            if score <= 0:
                continue
            acct_no = str(acct.account_no)
            best = account_scores.get(acct_no, 0)
            if score > best:
                account_winners[acct_no] = (stmt, caption)
                account_scores[acct_no] = score

    # Group winners back by caption.
    matches_by_caption: dict[tuple[str, str], list[str]] = {}
    for acct_no, key in account_winners.items():
        matches_by_caption.setdefault(key, []).append(acct_no)
    for acct_list in matches_by_caption.values():
        acct_list.sort()

    result = AutoMapResult(
        entity_id=entity_id,
        statement=statement,
        dry_run=dry_run,
        overwrite=overwrite,
        captions_total=len(caption_pairs),
    )

    for stmt, caption in caption_pairs:
        gl_accounts = list(matches_by_caption.get((stmt, caption), []))
        dimensions  = find_matching_dimension_values(dim_values, caption)
        stats = skip_stats_by_caption.get((stmt, caption), AutoMapSkipStats())
        tx_count = _count_transactions_for_accounts(db, entity_id, gl_accounts)

        existing_row = existing.get((stmt, caption))
        already_mapped = bool(existing_row and existing_row.erp_gl_json)

        entry_kwargs = dict(
            statement=stmt,
            tflux_caption=caption,
            mapped_accounts_count=len(gl_accounts),
            matched_transaction_count=tx_count,
            skipped_subtotal_accounts=stats.skipped_subtotal_accounts,
            skipped_non_posting_accounts=stats.skipped_non_posting_accounts,
            skipped_suspicious_category_accounts=stats.skipped_suspicious_category_accounts,
        )

        if not gl_accounts and not already_mapped:
            result.entries.append(AutoMapEntry(
                **entry_kwargs,
                skipped_reason="no_matching_gl_accounts",
                is_new_mapping=existing_row is None,
            ))
            result.captions_skipped += 1
            continue

        if already_mapped and not overwrite:
            preserved = list(existing_row.erp_gl_json or [])
            result.entries.append(AutoMapEntry(
                statement=stmt,
                tflux_caption=caption,
                matched_gl_accounts=preserved,
                mapped_accounts_count=len(preserved),
                matched_transaction_count=_count_transactions_for_accounts(
                    db, entity_id, preserved,
                ),
                matched_dimension_values=list(existing_row.erp_subgl_json or []),
                is_new_mapping=False,
                skipped_reason="existing_mapping_preserved",
                skipped_subtotal_accounts=stats.skipped_subtotal_accounts,
                skipped_non_posting_accounts=stats.skipped_non_posting_accounts,
                skipped_suspicious_category_accounts=stats.skipped_suspicious_category_accounts,
            ))
            result.captions_skipped += 1
            continue

        result.entries.append(AutoMapEntry(
            **entry_kwargs,
            matched_gl_accounts=list(gl_accounts),
            matched_dimension_values=list(dimensions),
            is_new_mapping=existing_row is None,
        ))
        result.captions_mapped += 1

        if dry_run:
            continue

        if existing_row is None:
            existing_row = OntologyMapping(
                entity_id=entity_id,
                statement=stmt,
                tflux_caption=caption,
            )
            db.add(existing_row)
        existing_row.erp_gl_json = list(gl_accounts)
        existing_row.erp_subgl_json = list(dimensions)
        existing_row.version = (existing_row.version or 1) + 1

    return result
