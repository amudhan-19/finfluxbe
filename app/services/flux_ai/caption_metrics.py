# @file    services/flux_ai/caption_metrics.py
#
# Caption-specific computed evidence (turnover ratios, aging, effect splits,
# sub-ledger party/invoice rollups) for Flux AI commentary.
#
# This module is deterministic and read-only. It NEVER invents DSO/DPO/DIO,
# aging, customers, vendors, invoice ages, or decomposition amounts: every
# number is computed from already-synced data (FluxData balances, the customer
# / vendor sub-ledgers). When a required input is not synced, the metric is
# left as ``None`` and an explicit evidence-gap string is recorded instead of
# fabricating a value.
#
# Plain ASCII only. No emojis.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import (
    Entity,
    ErpCustomerLedgerEntry,
    ErpVendorLedgerEntry,
    FluxData,
)
from app.services.ai_text_clean import format_currency
from app.services.flux_ai.caption_contracts import resolve_caption_contract
from app.services.flux_period_config import FluxPeriodConfig

# Caption ids that carry a turnover ratio + sub-ledger aging profile.
_AR_CAPTION_ID = "accounts_receivable"
_AP_CAPTION_ID = "accounts_payable"
_INVENTORY_CAPTION_ID = "inventory"

# Aging bucket edges in days (upper-exclusive except the final open bucket).
_AGING_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("0-30 days", 30),
    ("31-60 days", 60),
    ("61-90 days", 90),
    ("90+ days", None),
)


@dataclass
class AgingBucketEvidence:
    label: str
    amount: float
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "amount": self.amount, "count": self.count}


@dataclass
class CaptionPartyRollup:
    party_no: str
    party_name: str
    open_amount: float
    entry_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "party_no": self.party_no,
            "party_name": self.party_name,
            "open_amount": self.open_amount,
            "entry_count": self.entry_count,
        }


@dataclass
class CaptionInvoiceEvidence:
    document_no: str
    party_no: str
    party_name: str
    posting_date: str
    amount: float
    age_days: int | None
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_no": self.document_no,
            "party_no": self.party_no,
            "party_name": self.party_name,
            "posting_date": self.posting_date,
            "amount": self.amount,
            "age_days": self.age_days,
            "description": self.description,
        }


@dataclass
class CaptionMetricEvidence:
    """Computed turnover / aging / effect evidence for a single caption.

    All ratio and effect fields are ``None`` when the required input was not
    synced; the reason is recorded in ``evidence_gaps`` so the prompt and the
    deterministic fallback both surface the limitation instead of inventing.
    """

    caption_id: str
    party_label: str            # "customer" | "vendor"
    turnover_label: str         # "DSO" | "DPO" | "DIO"
    flow_label: str             # "revenue" | "COGS" | "purchases (COGS proxy)"

    balance_current: float
    balance_comparison: float
    flow_current: float | None
    flow_comparison: float | None

    days_current: int
    days_comparison: int

    turnover_current: float | None
    turnover_comparison: float | None
    turnover_change: float | None

    flow_effect: float | None        # revenue effect / purchasing effect
    turnover_effect: float | None    # collection effect / payment-timing effect

    aging_buckets_current: list[AgingBucketEvidence] = field(default_factory=list)
    aging_comparison_available: bool = False

    top_parties: list[CaptionPartyRollup] = field(default_factory=list)
    top_invoices: list[CaptionInvoiceEvidence] = field(default_factory=list)
    concentration_pct: float | None = None

    evidence_gaps: list[str] = field(default_factory=list)

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "caption_id": self.caption_id,
            "party_label": self.party_label,
            "turnover_label": self.turnover_label,
            "flow_label": self.flow_label,
            "balance_current": self.balance_current,
            "balance_comparison": self.balance_comparison,
            "flow_current": self.flow_current,
            "flow_comparison": self.flow_comparison,
            "days_current": self.days_current,
            "days_comparison": self.days_comparison,
            "turnover_current": self.turnover_current,
            "turnover_comparison": self.turnover_comparison,
            "turnover_change": self.turnover_change,
            "flow_effect": self.flow_effect,
            "turnover_effect": self.turnover_effect,
            "aging_buckets_current": [b.to_dict() for b in self.aging_buckets_current],
            "aging_comparison_available": self.aging_comparison_available,
            "top_parties": [p.to_dict() for p in self.top_parties],
            "top_invoices": [i.to_dict() for i in self.top_invoices],
            "concentration_pct": self.concentration_pct,
            "evidence_gaps": list(self.evidence_gaps),
        }


# ── Internal helpers ───────────────────────────────────────────────────────

def _f(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _period_days(start: date | None, end: date | None) -> int:
    if not start or not end:
        return 0
    delta = (end - start).days + 1
    return delta if delta > 0 else 0


def _lookup_flux_caption_totals(
    db: Session, entity: Entity, target_caption_id: str,
) -> tuple[float | None, float | None]:
    """Sum the current/comparison values of all FluxData rows whose caption
    resolves (via the contract alias map) to ``target_caption_id`` for the
    entity's active period. Returns ``(None, None)`` when no such row exists."""
    rows = (
        db.query(FluxData)
        .filter(
            FluxData.entity_id == entity.id,
            FluxData.period_from == entity.period_from,
            FluxData.period_to == entity.period_to,
        )
        .all()
    )
    cur_total = 0.0
    comp_total = 0.0
    found = False
    for row in rows:
        contract = resolve_caption_contract(row.caption, row.statement)
        if contract is None or contract.caption_id != target_caption_id:
            continue
        cur_total += _f(row.current_value)
        comp_total += _f(row.comp_value)
        found = True
    if not found:
        return None, None
    return cur_total, comp_total


def _turnover_days(balance: float, flow: float | None, period_days: int) -> float | None:
    """Turnover in days = |balance| / |flow| * period_days. ``None`` when the
    flow is missing or zero (cannot divide) or the period span is unknown."""
    if flow is None or period_days <= 0:
        return None
    flow_mag = abs(flow)
    if flow_mag <= 0:
        return None
    return abs(balance) / flow_mag * period_days


def _effect_split(
    *,
    balance_current: float,
    balance_comparison: float,
    flow_current: float | None,
    turnover_comparison: float | None,
    days_current: int,
) -> tuple[float | None, float | None]:
    """Two-factor balance bridge (flow effect + turnover effect) that sums
    exactly to (|balance_current| - |balance_comparison|).

    flow effect      = balance at constant turnover - prior balance
    turnover effect  = current balance - balance at constant turnover

    Returns ``(None, None)`` when the inputs needed are not all present."""
    if flow_current is None or turnover_comparison is None or days_current <= 0:
        return None, None
    bal_at_constant = (turnover_comparison / days_current) * abs(flow_current)
    flow_effect = bal_at_constant - abs(balance_comparison)
    turnover_effect = abs(balance_current) - bal_at_constant
    return flow_effect, turnover_effect


def _aging_buckets(
    rows: list[Any], as_of: date | None,
) -> list[AgingBucketEvidence]:
    """Bucket open sub-ledger entries by posting-date age as of period end."""
    buckets = [AgingBucketEvidence(label=lbl, amount=0.0, count=0) for lbl, _ in _AGING_BUCKETS]
    if not as_of:
        return buckets
    for row in rows:
        remaining = _f(getattr(row, "remaining_amount", None))
        if remaining == 0:
            continue
        posting = getattr(row, "posting_date", None)
        if not posting:
            continue
        age = (as_of - posting).days
        if age < 0:
            age = 0
        for idx, (_, upper) in enumerate(_AGING_BUCKETS):
            if upper is None or age <= upper:
                buckets[idx].amount += remaining
                buckets[idx].count += 1
                break
    return buckets


def _open_ledger_rows(
    db: Session, model: Any, entity_id: str,
) -> list[Any]:
    return (
        db.query(model)
        .filter(model.entity_id == entity_id)
        .all()
    )


def _party_rollups_and_invoices(
    rows: list[Any],
    *,
    no_attr: str,
    name_attr: str,
    as_of: date | None,
    limit: int = 5,
) -> tuple[list[CaptionPartyRollup], list[CaptionInvoiceEvidence], float, float | None]:
    """Aggregate open sub-ledger rows into party rollups + top invoices.

    Returns (top_parties, top_invoices, total_open, concentration_pct)."""
    party_totals: dict[str, dict[str, Any]] = {}
    invoices: list[CaptionInvoiceEvidence] = []
    total_open = 0.0
    for row in rows:
        remaining = _f(getattr(row, "remaining_amount", None))
        if remaining == 0:
            continue
        total_open += remaining
        pno = (getattr(row, no_attr, None) or "").strip() or "?"
        pname = (getattr(row, name_attr, None) or "").strip() or pno
        bucket = party_totals.setdefault(
            pno, {"name": pname, "amount": 0.0, "count": 0},
        )
        bucket["amount"] += remaining
        bucket["count"] += 1
        if bucket["name"] in ("", "?", pno) and pname not in ("", "?"):
            bucket["name"] = pname

        posting = getattr(row, "posting_date", None)
        age: int | None = None
        if posting and as_of:
            age = (as_of - posting).days
            if age < 0:
                age = 0
        invoices.append(CaptionInvoiceEvidence(
            document_no=(getattr(row, "document_no", None) or "-").strip() or "-",
            party_no=pno,
            party_name=pname,
            posting_date=posting.isoformat() if posting else "?",
            amount=remaining,
            age_days=age,
            description=(getattr(row, "raw_source_json", None) or {}).get("description", "")
            if isinstance(getattr(row, "raw_source_json", None), dict) else "",
        ))

    parties = [
        CaptionPartyRollup(
            party_no=pno,
            party_name=str(meta["name"]),
            open_amount=float(meta["amount"]),
            entry_count=int(meta["count"]),
        )
        for pno, meta in party_totals.items()
    ]
    parties.sort(key=lambda p: abs(p.open_amount), reverse=True)
    invoices.sort(key=lambda i: abs(i.amount), reverse=True)

    concentration_pct: float | None = None
    if total_open != 0 and parties:
        concentration_pct = abs(parties[0].open_amount) / abs(total_open) * 100.0

    return parties[:limit], invoices[:limit], total_open, concentration_pct


# ── Public builders ────────────────────────────────────────────────────────

def _build_subledger_turnover_metrics(
    db: Session,
    entity: Entity,
    period: FluxPeriodConfig,
    *,
    caption_id: str,
    party_label: str,
    turnover_label: str,
    flow_label: str,
    flow_caption_id: str,
    balance_current: float,
    balance_comparison: float,
    ledger_model: Any,
    party_no_attr: str,
    party_name_attr: str,
    flow_is_purchases_proxy: bool = False,
    compute_effects: bool = True,
) -> CaptionMetricEvidence:
    days_current = _period_days(period.current_start_date, period.current_end_date)
    days_comparison = _period_days(
        period.comparison_start_date, period.comparison_end_date,
    )
    flow_current, flow_comparison = _lookup_flux_caption_totals(
        db, entity, flow_caption_id,
    )

    gaps: list[str] = []
    turnover_current = _turnover_days(balance_current, flow_current, days_current)
    turnover_comparison = _turnover_days(
        balance_comparison, flow_comparison, days_comparison,
    )
    turnover_change = (
        turnover_current - turnover_comparison
        if turnover_current is not None and turnover_comparison is not None
        else None
    )
    if flow_current is None or flow_comparison is None:
        gaps.append(
            f"{turnover_label} could not be computed because the paired "
            f"{flow_label} value was not found in generated Flux data for "
            "both periods."
        )

    flow_effect: float | None = None
    turnover_effect: float | None = None
    if compute_effects:
        flow_effect, turnover_effect = _effect_split(
            balance_current=balance_current,
            balance_comparison=balance_comparison,
            flow_current=flow_current,
            turnover_comparison=turnover_comparison,
            days_current=days_current,
        )
        if flow_effect is None:
            gaps.append(
                "The flow-effect / timing-effect split could not be computed "
                f"without {turnover_label} for both periods; decomposition is "
                "limited to the balance roll-forward."
            )

    rows = _open_ledger_rows(db, ledger_model, entity.id)
    aging_current = _aging_buckets(rows, period.current_end_date)
    top_parties, top_invoices, _total_open, concentration_pct = (
        _party_rollups_and_invoices(
            rows,
            no_attr=party_no_attr,
            name_attr=party_name_attr,
            as_of=period.current_end_date,
        )
    )
    if not rows:
        gaps.append(
            f"{party_label.capitalize()} sub-ledger detail (open balances, "
            "aging, top parties/invoices) is not available in the current "
            "synced ERP payload."
        )

    # Comparison-period aging snapshot is not retained by the ERP sync (only
    # the current open balance is stored), so an aging shift cannot be computed.
    gaps.append(
        "Comparison-period aging snapshot is not retained by the ERP sync, so "
        "an aging-bucket shift cannot be computed; only the current aging "
        "profile is shown."
    )
    gaps.append(
        "Allowance / write-off movements and FX revaluation are not separately "
        "identified in the current synced payload."
    )
    if flow_is_purchases_proxy:
        gaps.append(
            "Purchases are approximated by COGS because a dedicated purchases "
            f"figure is not synced; {turnover_label} therefore uses COGS as a "
            "proxy denominator."
        )

    return CaptionMetricEvidence(
        caption_id=caption_id,
        party_label=party_label,
        turnover_label=turnover_label,
        flow_label=flow_label,
        balance_current=balance_current,
        balance_comparison=balance_comparison,
        flow_current=flow_current,
        flow_comparison=flow_comparison,
        days_current=days_current,
        days_comparison=days_comparison,
        turnover_current=turnover_current,
        turnover_comparison=turnover_comparison,
        turnover_change=turnover_change,
        flow_effect=flow_effect,
        turnover_effect=turnover_effect,
        aging_buckets_current=aging_current,
        aging_comparison_available=False,
        top_parties=top_parties,
        top_invoices=top_invoices,
        concentration_pct=concentration_pct,
        evidence_gaps=gaps,
    )


def build_caption_metrics(
    db: Session,
    entity: Entity,
    period: FluxPeriodConfig,
    caption_id: str,
    *,
    balance_current: float,
    balance_comparison: float,
) -> CaptionMetricEvidence | None:
    """Dispatch to the caption-specific computed-metric builder.

    Returns ``None`` for captions that do not carry a turnover/aging profile;
    callers then rely on the existing roll-forward / GL-movement evidence."""
    if caption_id == _AR_CAPTION_ID:
        return _build_subledger_turnover_metrics(
            db, entity, period,
            caption_id=_AR_CAPTION_ID,
            party_label="customer",
            turnover_label="DSO",
            flow_label="revenue",
            flow_caption_id="revenue",
            balance_current=balance_current,
            balance_comparison=balance_comparison,
            ledger_model=ErpCustomerLedgerEntry,
            party_no_attr="customer_no",
            party_name_attr="customer_name",
        )
    if caption_id == _AP_CAPTION_ID:
        return _build_subledger_turnover_metrics(
            db, entity, period,
            caption_id=_AP_CAPTION_ID,
            party_label="vendor",
            turnover_label="DPO",
            flow_label="purchases (COGS proxy)",
            flow_caption_id="cost_of_revenue",
            balance_current=balance_current,
            balance_comparison=balance_comparison,
            ledger_model=ErpVendorLedgerEntry,
            party_no_attr="vendor_no",
            party_name_attr="vendor_name",
            flow_is_purchases_proxy=True,
        )
    if caption_id == _INVENTORY_CAPTION_ID:
        # Inventory has no party sub-ledger; DIO is computable from COGS but the
        # purchase / consumption / write-down / cost-revaluation split is not
        # separable from synced data. Effects are therefore left as gaps.
        metrics = _build_subledger_turnover_metrics(
            db, entity, period,
            caption_id=_INVENTORY_CAPTION_ID,
            party_label="warehouse/SKU",
            turnover_label="DIO",
            flow_label="COGS",
            flow_caption_id="cost_of_revenue",
            balance_current=balance_current,
            balance_comparison=balance_comparison,
            ledger_model=ErpCustomerLedgerEntry,  # unused; produces empty rollups
            party_no_attr="customer_no",
            party_name_attr="customer_name",
            compute_effects=False,
        )
        # Inventory must not borrow the customer sub-ledger rollups.
        metrics.top_parties = []
        metrics.top_invoices = []
        metrics.aging_buckets_current = []
        metrics.concentration_pct = None
        metrics.evidence_gaps = [
            g for g in metrics.evidence_gaps
            if "sub-ledger detail" not in g and "aging" not in g
        ]
        metrics.evidence_gaps.append(
            "Purchase / consumption / write-down / cost-revaluation effects and "
            "warehouse/SKU detail are not separable from the current synced "
            "data; only DIO and the balance roll-forward are available."
        )
        return metrics
    return None


def format_turnover_days(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f} days"


def metric_evidence_status_lines(
    metrics: CaptionMetricEvidence | None,
    required_evidence: list[str] | tuple[str, ...] | None,
) -> list[str]:
    """Map each contract required_evidence item to an Available / Missing line
    using what the computed metrics actually contain."""
    if not required_evidence:
        return []
    have_turnover = metrics is not None and metrics.turnover_current is not None
    have_flow = metrics is not None and metrics.flow_current is not None
    have_parties = bool(metrics and metrics.top_parties)
    have_aging = bool(metrics and metrics.aging_buckets_current and any(
        b.count for b in metrics.aging_buckets_current
    ))
    have_effects = metrics is not None and metrics.flow_effect is not None

    lines: list[str] = []
    for item in required_evidence:
        low = item.lower()
        status = "Missing"
        if any(k in low for k in ("dso", "dpo", "dio", "turnover", "days")):
            status = "Available" if have_turnover else "Missing"
        elif any(k in low for k in ("revenue", "cogs", "purchase", "flow", "sales")):
            status = "Available" if have_flow else "Missing"
        elif any(k in low for k in ("customer", "vendor", "party", "concentration")):
            status = "Available" if have_parties else "Missing"
        elif "aging" in low or "ageing" in low:
            status = "Available (current only)" if have_aging else "Missing"
        elif any(k in low for k in ("allowance", "write-off", "writeoff", "fx", "poster")):
            status = "Missing"
        elif any(k in low for k in ("effect", "decomposition", "bridge")):
            status = "Available" if have_effects else "Missing"
        elif any(k in low for k in ("invoice", "entry", "transaction", "document")):
            status = "Available" if (metrics and metrics.top_invoices) else "Partial"
        lines.append(f"{item}: {status}.")
    return lines
