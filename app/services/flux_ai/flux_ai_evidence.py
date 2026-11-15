# @file    services/flux_ai/flux_ai_evidence.py
#
# Backend-computed evidence for Flux AI commentary (read-only GL aggregation).

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import Entity, ErpCustomer, ErpGeneralLedgerEntry, ErpGlAccount, ErpVendor, OntologyMapping
from app.services.ai_text_clean import format_currency, format_percent
from app.services.flux_ai.caption_contracts import resolve_caption_contract
from app.services.flux_ai.caption_metrics import CaptionMetricEvidence, build_caption_metrics
from app.services.flux_ai.gl_entry_party import (
    GlEntryPartyRef,
    PartySummaryEvidence,
    build_top_party_summaries,
    enrich_party_ref,
    largest_party_link_line,
    load_party_name_maps,
    resolve_gl_entry_party,
)
from app.services.flux_generation import (
    GlAccountIdentitySet,
    match_transactions_for_caption,
    resolve_gl_account_identity,
)
from app.services.flux_finance_variance import (
    format_transaction_amount_label,
    resolve_normal_balance,
)
from app.services.flux_period_config import parse_flux_period_config
from app.services.subledger_catalog import (
    calculation_mode_for_statement,
    calculation_mode_label,
)


@dataclass
class MappedGlAccount:
    account_no: str
    account_name: str
    label: str


@dataclass
class GlMovementEvidence:
    account_no: str
    account_name: str
    current_amount: float
    comparison_amount: float
    variance_amount: float
    majority_period: str


@dataclass
class TransactionEvidence:
    posting_date: str
    account_no: str
    account_name: str
    document_no: str
    description: str
    amount: float
    period_bucket: str
    formatted_line: str
    external_document_no: str = "-"
    debit_amount: float | None = None
    credit_amount: float | None = None
    source_type: str | None = None
    source_number: str | None = None
    party_type: str | None = None
    party_no: str | None = None
    party_name: str | None = None
    narration_is_generic: bool = False
    # Aging in days from the entry posting date to the balance-at-date cut-off
    # (Balance Sheet captions only). ``None`` for net-change / P&L captions
    # where invoice aging is not meaningful.
    age_days: int | None = None


@dataclass
class PostingSignalEvidence:
    current_transaction_count: int
    comparison_transaction_count: int
    majority_period: str
    largest_transaction_amount: float
    period_end_concentration: bool = False
    manual_journal_present: bool = False
    generic_narration_count: int = 0
    timing_lines: list[str] = field(default_factory=list)


@dataclass
class AttributionCoverageEvidence:
    customer_vendor_master_synced: bool
    transaction_party_link_available: bool
    poster_source_available: bool
    selected_customers: list[str] = field(default_factory=list)
    selected_vendors: list[str] = field(default_factory=list)


@dataclass
class TransactionPatternEvidence:
    """Legacy alias — timing signals live in posting_signal."""
    current_transaction_count: int
    comparison_transaction_count: int
    majority_period: str
    largest_transaction_amount: float
    pattern_lines: list[str] = field(default_factory=list)


@dataclass
class FluxAiEvidence:
    calculation_mode: str
    calculation_mode_label: str
    current_period_label: str
    comparison_period_label: str
    mapped_gl_accounts: list[MappedGlAccount] = field(default_factory=list)
    top_gl_movements: list[GlMovementEvidence] = field(default_factory=list)
    top_transactions: list[TransactionEvidence] = field(default_factory=list)
    transaction_pattern: TransactionPatternEvidence = field(
        default_factory=lambda: TransactionPatternEvidence(
            current_transaction_count=0,
            comparison_transaction_count=0,
            majority_period="none",
            largest_transaction_amount=0.0,
        ),
    )
    main_movement_line: str = ""
    largest_party_line: str = ""
    top_parties: list[PartySummaryEvidence] = field(default_factory=list)
    posting_signal: PostingSignalEvidence = field(
        default_factory=lambda: PostingSignalEvidence(
            current_transaction_count=0,
            comparison_transaction_count=0,
            majority_period="none",
            largest_transaction_amount=0.0,
        ),
    )
    attribution_coverage: AttributionCoverageEvidence = field(
        default_factory=lambda: AttributionCoverageEvidence(
            customer_vendor_master_synced=False,
            transaction_party_link_available=False,
            poster_source_available=False,
        ),
    )
    has_transaction_detail: bool = False
    has_gl_movement_detail: bool = False
    has_gl_party_links: bool = False
    # Caption-specific computed evidence (DSO/DPO/DIO, aging, effect split,
    # sub-ledger party/invoice rollups). None for captions without a turnover /
    # aging profile; callers then rely on GL-movement / roll-forward evidence.
    caption_metrics: CaptionMetricEvidence | None = None

    def to_prompt_dict(self, *, direction: str = "", is_material: bool = False) -> dict[str, Any]:
        return {
            "calculation_mode": self.calculation_mode,
            "calculation_mode_label": self.calculation_mode_label,
            "current_period": self.current_period_label,
            "comparison_period": self.comparison_period_label,
            "direction": direction,
            "is_material": is_material,
            "mapped_gl_accounts": [
                {"account_no": a.account_no, "account_name": a.account_name}
                for a in self.mapped_gl_accounts
            ],
            "top_gl_movements": [
                {
                    "account_no": m.account_no,
                    "account_name": m.account_name,
                    "current_amount": m.current_amount,
                    "comparison_amount": m.comparison_amount,
                    "variance_amount": m.variance_amount,
                    "majority_period": m.majority_period,
                }
                for m in self.top_gl_movements
            ],
            "top_transactions": [
                {
                    "posting_date": t.posting_date,
                    "account_no": t.account_no,
                    "account_name": t.account_name,
                    "document_no": t.document_no,
                    "external_document_no": t.external_document_no,
                    "description": t.description,
                    "amount": t.amount,
                    "debit_amount": t.debit_amount,
                    "credit_amount": t.credit_amount,
                    "source_type": t.source_type,
                    "source_number": t.source_number,
                    "party_type": t.party_type,
                    "party_no": t.party_no,
                    "party_name": t.party_name,
                    "period_bucket": t.period_bucket,
                    "narration_is_generic": t.narration_is_generic,
                    "age_days": t.age_days,
                    "formatted_line": t.formatted_line,
                }
                for t in self.top_transactions
            ],
            "top_parties": [
                {
                    "party_type": p.party_type,
                    "party_no": p.party_no,
                    "party_name": p.party_name,
                    "transaction_count": p.transaction_count,
                    "total_amount": p.total_amount,
                }
                for p in self.top_parties
            ],
            "posting_signal_summary": {
                "current_transaction_count": self.posting_signal.current_transaction_count,
                "comparison_transaction_count": (
                    self.posting_signal.comparison_transaction_count
                ),
                "majority_period": self.posting_signal.majority_period,
                "largest_transaction_amount": self.posting_signal.largest_transaction_amount,
                "period_end_concentration": self.posting_signal.period_end_concentration,
                "manual_journal_present": self.posting_signal.manual_journal_present,
                "generic_narration_count": self.posting_signal.generic_narration_count,
                "timing_lines": self.posting_signal.timing_lines,
            },
            "attribution_coverage": {
                "customer_vendor_master_synced": (
                    self.attribution_coverage.customer_vendor_master_synced
                ),
                "transaction_party_link_available": (
                    self.attribution_coverage.transaction_party_link_available
                ),
                "poster_source_available": self.attribution_coverage.poster_source_available,
                "selected_customers": self.attribution_coverage.selected_customers,
                "selected_vendors": self.attribution_coverage.selected_vendors,
            },
            "main_movement_line": self.main_movement_line,
            "largest_party_line": self.largest_party_line,
            "has_gl_party_links": self.has_gl_party_links,
            "caption_metrics": (
                self.caption_metrics.to_prompt_dict()
                if self.caption_metrics is not None else None
            ),
        }


def party_breakdown_lines(
    evidence: FluxAiEvidence | None,
    *,
    party_label: str = "party",
    limit: int = 3,
) -> list[str]:
    """Customer/vendor drill-down rollup lines from transaction-linked party
    summaries. Document-level association only — never presented as a
    standalone variance driver. Returns ``[]`` when no party linkage exists."""
    if not evidence or not evidence.top_parties:
        return []
    ranked = sorted(
        evidence.top_parties, key=lambda p: abs(p.total_amount), reverse=True,
    )
    top = ranked[:limit]
    lines: list[str] = []
    for p in top:
        name = (p.party_name or p.party_no or "").strip()
        prefix = "Customer" if p.party_type == "customer" else "Vendor"
        label = f"{prefix} {p.party_no} — {name}" if name and name != p.party_no else f"{prefix} {p.party_no}"
        entry_word = "entry" if p.transaction_count == 1 else "entries"
        lines.append(
            f"By {party_label}: {label} — {p.transaction_count} matched "
            f"{entry_word} associated with {format_currency(p.total_amount)} "
            "total (document-level link only; not a standalone variance driver).",
        )
    tail = ranked[limit:]
    if tail:
        tail_total = sum(p.total_amount for p in tail)
        lines.append(
            f"Remaining {len(tail)} parties aggregated "
            f"{format_currency(tail_total)} of associated entries.",
        )
    return lines


_GENERIC_NARRATIONS = frozenset({
    "adjustment", "reclass", "reclassification", "true-up", "true up",
    "journal", "misc", "correction", "reconcile", "accrual", "trueup",
})


def _is_generic_narration(desc: str) -> bool:
    d = (desc or "").strip().lower()
    if not d or d in ("(no description)", "-", "n/a"):
        return True
    if d in _GENERIC_NARRATIONS:
        return True
    if len(d) <= 3:
        return True
    for token in _GENERIC_NARRATIONS:
        if d == token or d.startswith(token + " ") or d.endswith(" " + token):
            return True
    return False


def _resolve_erp_type(db: Session, entity_id: str) -> str:
    from app.models.models import EntityErpMapping, ERP_TYPE_QUICKBOOKS
    row = (
        db.query(EntityErpMapping.erp_key)
        .filter(EntityErpMapping.entity_id == entity_id)
        .first()
    )
    if row and row.erp_key == ERP_TYPE_QUICKBOOKS:
        return ERP_TYPE_QUICKBOOKS
    return "bc"


def _detect_manual_journal(entry: ErpGeneralLedgerEntry) -> bool:
    raw = entry.raw_source_json if isinstance(entry.raw_source_json, dict) else {}
    txn_type = str(raw.get("transaction_type") or raw.get("entryType") or "").lower()
    source = str(raw.get("source") or raw.get("sourceType") or "").lower()
    if "journal" in txn_type or "manual" in source:
        return True
    desc = (entry.description or "").lower()
    return "journal entry" in desc or desc.startswith("je ")


def _period_end_concentration(
    entries: list[ErpGeneralLedgerEntry],
    period_end: date,
    *,
    window_days: int = 5,
) -> bool:
    if not entries or not period_end:
        return False
    window_start = period_end - timedelta(days=window_days)
    total_abs = sum(abs(float(e.amount or 0)) for e in entries)
    if total_abs <= 0:
        return False
    end_abs = sum(
        abs(float(e.amount or 0))
        for e in entries
        if e.posting_date and window_start <= e.posting_date <= period_end
    )
    return end_abs / total_abs >= 0.4


def _account_name_map(
    db: Session, entity_id: str, account_nos: set[str], *, erp_type: str,
) -> dict[str, str]:
    if not account_nos:
        return {}
    rows = (
        db.query(ErpGlAccount.account_no, ErpGlAccount.account_name, ErpGlAccount.display_name)
        .filter(
            ErpGlAccount.entity_id == entity_id,
            ErpGlAccount.erp_type == erp_type,
            ErpGlAccount.account_no.in_(account_nos),
        )
        .all()
    )
    out: dict[str, str] = {}
    for no, name, display in rows:
        if no:
            out[str(no)] = (display or name or str(no)).strip()
    return out


def _sum_entry_amounts(entries: list[ErpGeneralLedgerEntry]) -> float:
    total = Decimal("0")
    for e in entries:
        if e.amount is not None:
            total += Decimal(str(e.amount))
    return float(total)


def _entries_by_account(
    entries: list[ErpGeneralLedgerEntry],
) -> dict[str, list[ErpGeneralLedgerEntry]]:
    by_acct: dict[str, list[ErpGeneralLedgerEntry]] = {}
    for e in entries:
        acct = (e.account_no or "").strip()
        if not acct:
            continue
        by_acct.setdefault(acct, []).append(e)
    return by_acct


def _majority_period_label(
    current_total: float, comparison_total: float,
) -> str:
    cur = abs(current_total)
    comp = abs(comparison_total)
    if cur == 0 and comp == 0:
        return "none"
    if cur > comp * 1.1:
        return "current"
    if comp > cur * 1.1:
        return "comparison"
    return "both"


def _format_txn_line(
    entry: ErpGeneralLedgerEntry,
    account_names: dict[str, str],
    period_bucket: str,
    *,
    party: GlEntryPartyRef | None = None,
    normal_balance: str = "debit",
    as_of_date: date | None = None,
) -> TransactionEvidence:
    acct_no = (entry.account_no or "").strip() or "?"
    acct_name = account_names.get(acct_no, acct_no)
    post = entry.posting_date.isoformat() if entry.posting_date else "?"
    # Invoice/entry aging is only meaningful for Balance Sheet (balance-at-date)
    # captions: the number of days between the entry posting date and the
    # period-end cut-off. ``as_of_date`` is only supplied for those captions.
    age_days: int | None = None
    if as_of_date is not None and entry.posting_date:
        delta = (as_of_date - entry.posting_date).days
        age_days = delta if delta >= 0 else 0
    doc = (entry.document_no or "").strip() or "-"
    ext_doc = (getattr(entry, "external_document_number", None) or "").strip() or "-"
    desc = (entry.description or "").strip() or "(no description)"
    generic_narr = _is_generic_narration(desc)
    amt = float(entry.amount) if entry.amount is not None else 0.0
    debit = float(entry.debit_amount) if entry.debit_amount is not None else None
    credit = float(entry.credit_amount) if entry.credit_amount is not None else None
    source_type = getattr(entry, "source_type", None) or None
    source_number = getattr(entry, "source_number", None) or None
    gl_label = f"GL {acct_no} — {acct_name}" if acct_name != acct_no else f"GL {acct_no}"
    party_segment = ""
    party_type: str | None = None
    party_no: str | None = None
    party_name: str | None = None
    if party is not None:
        party_type = party.party_type
        party_no = party.party_no
        party_name = party.party_name
        party_segment = f" | {party.display_label()}"
    ext_segment = f" | Ext Doc {ext_doc}" if ext_doc != "-" else ""
    age_segment = f" | aged {age_days} days" if age_days is not None else ""
    amt_label = format_transaction_amount_label(amt, normal_balance)  # type: ignore[arg-type]
    formatted = (
        f"{post} | {gl_label}{party_segment}{ext_segment} | "
        f"{amt_label}{age_segment} | {desc}"
    )
    return TransactionEvidence(
        posting_date=post,
        account_no=acct_no,
        account_name=acct_name,
        document_no=doc,
        external_document_no=ext_doc,
        description=desc,
        amount=amt,
        debit_amount=debit,
        credit_amount=credit,
        source_type=source_type,
        source_number=source_number,
        period_bucket=period_bucket,
        formatted_line=formatted,
        age_days=age_days,
        party_type=party_type,
        party_no=party_no,
        party_name=party_name,
        narration_is_generic=generic_narr,
    )


def _build_timing_lines(
    *,
    current_count: int,
    comparison_count: int,
    majority_period: str,
    top_txns: list[TransactionEvidence],
    period_end_concentration: bool,
    manual_journal_present: bool,
    generic_narration_count: int,
) -> list[str]:
    lines: list[str] = []
    if current_count == 0 and comparison_count == 0:
        lines.append(
            "No matching transactions were found for the mapped GL accounts "
            "in the selected current or comparison periods.",
        )
        return lines
    if current_count == 0:
        lines.append(
            "Most of the movement occurred in the comparison period, while the "
            "current period has no matching transactions for this mapped caption.",
        )
    elif comparison_count == 0:
        lines.append(
            "Movement is concentrated in the current period; the comparison "
            "period has no matching transactions for this mapped caption.",
        )
    elif majority_period == "comparison":
        lines.append(
            "Movement is concentrated in the comparison period based on "
            "transaction counts and amounts for mapped GL accounts.",
        )
    elif majority_period == "current":
        lines.append(
            "Movement is concentrated in the current period based on "
            "transaction counts and amounts for mapped GL accounts.",
        )
    else:
        lines.append(
            "Movement is spread across both current and comparison periods "
            "for the mapped GL accounts.",
        )

    if len(top_txns) >= 2:
        abs_amounts = [abs(t.amount) for t in top_txns]
        if abs_amounts:
            top_two = sum(sorted(abs_amounts, reverse=True)[:2])
            total = sum(abs_amounts)
            if total > 0 and top_two / total >= 0.6:
                lines.append(
                    "The variance is concentrated in a small number of large "
                    "entries, so the reviewer should validate whether they are "
                    "recurring or one-time.",
                )
            elif len(top_txns) >= 5:
                lines.append(
                    "Many smaller entries contributed to the overall movement.",
                )

    if period_end_concentration:
        lines.append(
            "A material share of entries posted near period-end; review cut-off "
            "before sign-off.",
        )
    if manual_journal_present:
        lines.append(
            "Manual or journal-style entries are present in the matched "
            "transactions; validate supporting documentation.",
        )
    if generic_narration_count >= 2:
        lines.append(
            f"{generic_narration_count} matched entries carry generic narration; "
            "supporting evidence should be requested for material lines.",
        )
    return lines


def _build_pattern_lines(
    *,
    current_count: int,
    comparison_count: int,
    majority_period: str,
    top_txns: list[TransactionEvidence],
) -> list[str]:
    lines: list[str] = []
    if current_count == 0 and comparison_count == 0:
        lines.append(
            "No matching transactions were found for the mapped GL accounts "
            "in the selected current or comparison periods.",
        )
        return lines
    if current_count == 0:
        lines.append(
            "Most of the movement occurred in the comparison period; the "
            "current period has no matching transactions for this mapped caption.",
        )
    elif comparison_count == 0:
        lines.append(
            "Movement is concentrated in the current period; the comparison "
            "period has no matching transactions for this mapped caption.",
        )
    elif majority_period == "comparison":
        lines.append(
            "Movement is concentrated in the comparison period based on "
            "transaction counts and amounts for mapped GL accounts.",
        )
    elif majority_period == "current":
        lines.append(
            "Movement is concentrated in the current period based on "
            "transaction counts and amounts for mapped GL accounts.",
        )
    else:
        lines.append(
            "Movement is spread across both current and comparison periods "
            "for the mapped GL accounts.",
        )

    if len(top_txns) >= 2:
        abs_amounts = [abs(t.amount) for t in top_txns]
        if abs_amounts:
            top_two = sum(sorted(abs_amounts, reverse=True)[:2])
            total = sum(abs_amounts)
            if total > 0 and top_two / total >= 0.6:
                lines.append(
                    "One or two large entries account for most of the "
                    "observed movement.",
                )
            elif len(top_txns) >= 5:
                lines.append(
                    "Many smaller entries contributed to the overall movement.",
                )
    return lines


def _gl_movement_sentence(
    move: GlMovementEvidence,
    *,
    normal_balance: str = "debit",
) -> str:
    name = move.account_name or move.account_no
    period_phrase = (
        "comparison period"
        if move.majority_period == "comparison"
        else "current period"
        if move.majority_period == "current"
        else "both periods"
    )
    if (
        normal_balance == "debit"
        and move.comparison_amount < 0
        and move.current_amount >= 0
    ):
        return (
            f"GL {move.account_no} — {name} contributed the largest mapped GL "
            f"movement, reflecting the shift between prior-period negative "
            f"adjustment activity ({format_currency(abs(move.comparison_amount))} "
            f"in comparison) and current-period cost activity "
            f"({format_currency(move.current_amount)})."
        )
    if (
        normal_balance == "debit"
        and move.current_amount < 0
        and move.comparison_amount >= 0
    ):
        return (
            f"GL {move.account_no} — {name} contributed the largest mapped GL "
            f"movement, reflecting the shift from prior-period cost activity "
            f"({format_currency(move.comparison_amount)}) to a current-period "
            f"net credit / negative adjustment position "
            f"({format_currency(abs(move.current_amount))})."
        )
    if move.majority_period == "comparison" and move.current_amount == 0:
        return (
            f"GL {move.account_no} — {name} contributed the largest movement in "
            f"the comparison period, including negative adjustment activity."
        )
    if move.majority_period == "current" and move.comparison_amount == 0:
        return (
            f"GL {move.account_no} — {name} drove the current-period movement."
        )
    direction = "higher" if move.variance_amount >= 0 else "lower"
    return (
        f"GL {move.account_no} — {name} contributed "
        f"{format_currency(abs(move.variance_amount))} of account-level variance, "
        f"primarily in the {period_phrase} ({direction} activity)."
    )


def _dedupe_transactions(
    txns: list[TransactionEvidence],
) -> list[TransactionEvidence]:
    """Collapse repeated entries that share the same document number, party,
    amount and posting date into a single representative line. When a group
    has more than one member the representative line is annotated with the
    occurrence count and aggregated amount so the reviewer sees it once."""
    groups: dict[tuple[Any, ...], list[TransactionEvidence]] = {}
    order: list[tuple[Any, ...]] = []
    for t in txns:
        key = (t.document_no, t.party_no or "", round(t.amount, 2), t.posting_date)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(t)
    out: list[TransactionEvidence] = []
    for key in order:
        grp = groups[key]
        rep = grp[0]
        if len(grp) > 1:
            total = sum(g.amount for g in grp)
            note = (
                f" (x{len(grp)} identical entries, "
                f"{format_currency(total)} aggregated)"
            )
            rep = replace(rep, formatted_line=rep.formatted_line + note)
        out.append(rep)
    return out


def build_flux_ai_evidence(
    db: Session,
    entity: Entity,
    statement: str,
    mapping: OntologyMapping | None,
) -> FluxAiEvidence:
    """Aggregate GL evidence for one caption using the same period rules as flux generation."""
    period = parse_flux_period_config(entity)
    mode = calculation_mode_for_statement(statement)
    mode_label = calculation_mode_label(mode)
    erp_type = _resolve_erp_type(db, entity.id)

    raw_codes: set[str] = set()
    if mapping and mapping.erp_gl_json:
        raw_codes = {str(x).strip() for x in mapping.erp_gl_json if x}
    # GL-only ontology mapping — do not filter transactions by customer/vendor selection.

    identity = resolve_gl_account_identity(db, entity.id, raw_codes, erp_type=erp_type)
    current_entries = match_transactions_for_caption(
        db, entity.id, identity,
        period.current_start_date, period.current_end_date,
        erp_type=erp_type,
        calculation_mode=mode,
        subledger_codes=None,
    )
    comparison_entries = match_transactions_for_caption(
        db, entity.id, identity,
        period.comparison_start_date, period.comparison_end_date,
        erp_type=erp_type,
        calculation_mode=mode,
        subledger_codes=None,
    )

    name_map = _account_name_map(db, entity.id, raw_codes, erp_type=erp_type)
    customer_names, vendor_names = load_party_name_maps(db, entity.id)
    all_matched = current_entries + comparison_entries
    caption_name = mapping.tflux_caption if mapping else ""
    normal_balance = resolve_normal_balance(caption_name, statement)

    def _party_for_entry(entry: ErpGeneralLedgerEntry) -> GlEntryPartyRef | None:
        ref = resolve_gl_entry_party(entry)
        if ref is None:
            return None
        return enrich_party_ref(
            ref, customer_names=customer_names, vendor_names=vendor_names,
        )
    mapped_accounts = [
        MappedGlAccount(
            account_no=no,
            account_name=name_map.get(no, no),
            label=f"{no} — {name_map.get(no, no)}",
        )
        for no in sorted(raw_codes)
    ]

    cur_by = _entries_by_account(current_entries)
    comp_by = _entries_by_account(comparison_entries)
    all_accts = set(cur_by) | set(comp_by) | set(raw_codes)

    movements: list[GlMovementEvidence] = []
    for acct in all_accts:
        cur_amt = _sum_entry_amounts(cur_by.get(acct, []))
        comp_amt = _sum_entry_amounts(comp_by.get(acct, []))
        var_amt = cur_amt - comp_amt
        if cur_amt == 0 and comp_amt == 0:
            continue
        movements.append(GlMovementEvidence(
            account_no=acct,
            account_name=name_map.get(acct, acct),
            current_amount=cur_amt,
            comparison_amount=comp_amt,
            variance_amount=var_amt,
            majority_period=_majority_period_label(cur_amt, comp_amt),
        ))
    movements.sort(key=lambda m: abs(m.variance_amount), reverse=True)
    top_movements = movements[:5]

    # Invoice/entry aging is only surfaced for Balance Sheet captions, which
    # use balance-at-date; for those, age is measured to each bucket's
    # period-end cut-off. P&L (net-change) captions get no aging.
    is_balance_at_date = mode == "balance_at_date"
    current_as_of = period.current_end_date if is_balance_at_date else None
    comparison_as_of = period.comparison_end_date if is_balance_at_date else None

    tagged: list[TransactionEvidence] = []
    for e in current_entries:
        tagged.append(_format_txn_line(
            e, name_map, "current",
            party=_party_for_entry(e),
            normal_balance=normal_balance,
            as_of_date=current_as_of,
        ))
    for e in comparison_entries:
        tagged.append(_format_txn_line(
            e, name_map, "comparison",
            party=_party_for_entry(e),
            normal_balance=normal_balance,
            as_of_date=comparison_as_of,
        ))
    tagged.sort(key=lambda t: abs(t.amount), reverse=True)
    # De-duplicate identical repeated entries before limiting to the top 5
    # material lines so the same invoice is not shown several times.
    top_txns = _dedupe_transactions(tagged)[:5]
    top_parties = build_top_party_summaries(
        all_matched,
        customer_names=customer_names,
        vendor_names=vendor_names,
    )
    has_party_links = any(t.party_type for t in tagged)
    cust_count = db.query(ErpCustomer).filter(
        ErpCustomer.entity_id == entity.id, ErpCustomer.erp_type == erp_type,
    ).count()
    vend_count = db.query(ErpVendor).filter(
        ErpVendor.entity_id == entity.id, ErpVendor.erp_type == erp_type,
    ).count()
    largest_party_line = ""
    master_synced = (cust_count + vend_count) > 0
    if top_txns and top_txns[0].party_type and top_txns[0].party_no:
        largest_party_line = largest_party_link_line(GlEntryPartyRef(
            party_type=top_txns[0].party_type,
            party_no=top_txns[0].party_no,
            party_name=top_txns[0].party_name,
        ), master_synced=master_synced)

    cur_total = _sum_entry_amounts(current_entries)
    comp_total = _sum_entry_amounts(comparison_entries)
    majority = _majority_period_label(cur_total, comp_total)
    largest_amt = max((abs(t.amount) for t in tagged), default=0.0)
    generic_narr_count = sum(1 for t in tagged if t.narration_is_generic)
    manual_journal = any(
        _detect_manual_journal(e) for e in current_entries + comparison_entries
    )
    period_end_flag = (
        _period_end_concentration(current_entries, period.current_end_date)
        or _period_end_concentration(comparison_entries, period.comparison_end_date)
    )

    timing_lines = _build_timing_lines(
        current_count=len(current_entries),
        comparison_count=len(comparison_entries),
        majority_period=majority,
        top_txns=top_txns,
        period_end_concentration=period_end_flag,
        manual_journal_present=manual_journal,
        generic_narration_count=generic_narr_count,
    )

    posting_signal = PostingSignalEvidence(
        current_transaction_count=len(current_entries),
        comparison_transaction_count=len(comparison_entries),
        majority_period=majority,
        largest_transaction_amount=largest_amt,
        period_end_concentration=period_end_flag,
        manual_journal_present=manual_journal,
        generic_narration_count=generic_narr_count,
        timing_lines=timing_lines,
    )

    pattern = TransactionPatternEvidence(
        current_transaction_count=len(current_entries),
        comparison_transaction_count=len(comparison_entries),
        majority_period=majority,
        largest_transaction_amount=largest_amt,
        pattern_lines=timing_lines,
    )

    attribution = AttributionCoverageEvidence(
        customer_vendor_master_synced=(cust_count + vend_count) > 0,
        transaction_party_link_available=has_party_links,
        poster_source_available=manual_journal,
    )

    main_line = ""
    if top_movements:
        main_line = _gl_movement_sentence(top_movements[0])
    elif top_txns:
        main_line = (
            f"Largest transaction: {top_txns[0].formatted_line} "
            f"({top_txns[0].period_bucket} period)."
        )

    # Caption-specific computed metrics (DSO/DPO/DIO, aging, effect split,
    # sub-ledger rollups). Uses the balance-at-date totals as the caption
    # balances. Returns None for captions without a turnover/aging profile.
    caption_metrics = None
    contract = resolve_caption_contract(caption_name, statement)
    if contract is not None:
        caption_metrics = build_caption_metrics(
            db, entity, period, contract.caption_id,
            balance_current=cur_total,
            balance_comparison=comp_total,
        )

    return FluxAiEvidence(
        calculation_mode=mode,
        calculation_mode_label=mode_label,
        current_period_label=period.current_label,
        comparison_period_label=period.comparison_label,
        mapped_gl_accounts=mapped_accounts,
        top_gl_movements=top_movements,
        top_transactions=top_txns,
        transaction_pattern=pattern,
        posting_signal=posting_signal,
        attribution_coverage=attribution,
        main_movement_line=main_line,
        largest_party_line=largest_party_line,
        top_parties=top_parties,
        has_transaction_detail=bool(top_txns),
        has_gl_movement_detail=bool(top_movements),
        has_gl_party_links=has_party_links,
        caption_metrics=caption_metrics,
    )


def build_summary_line(
    *,
    caption: str,
    current_value: float,
    comp_value: float,
    variance_amount: float,
    variance_pct: float | None,
    evidence: FluxAiEvidence,
) -> str:
    direction = "increased" if variance_amount >= 0 else "decreased"
    pct = format_percent(variance_pct)
    calc_note = (
        "calculated using Balance at Date as of the selected period end date"
        if evidence.calculation_mode == "balance_at_date"
        else "calculated using Net Change for the selected period"
    )
    return (
        f"{caption} {direction} by {format_currency(variance_amount)} ({pct}) "
        f"from {format_currency(current_value)} in {evidence.current_period_label} "
        f"compared with {format_currency(comp_value)} in "
        f"{evidence.comparison_period_label}, {calc_note}."
    )
