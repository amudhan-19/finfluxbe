# @file    services/flux_finance_variance.py
#
# Finance-normalized flux variance for credit-normal revenue/income captions.

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional, Union

from app.models.models import FluxData
from app.schemas.schemas import FluxRowOut
from app.services.ai_text_clean import format_currency

NormalBalance = Literal["debit", "credit", "neutral"]
FinanceDirection = Literal["increased", "decreased", "flat"]

_CREDIT_NORMAL_EXACT = frozenset({
    "revenue",
    "consulting revenue",
    "product revenue",
    "total revenue",
})

_SUBTOTAL_NEUTRAL = frozenset({
    "gross profit",
    "operating income",
    "net income",
    "ebitda",
    "ebit",
})

_EXPENSE_NAME_MARKERS = (
    "cost of revenue",
    "salaries",
    "professional services",
    "software & technology",
    "software and technology",
    "travel & entertainment",
    "travel and entertainment",
    "depreciation",
    "interest expense",
    "income tax",
)


@dataclass(frozen=True)
class FinanceVariance:
    normal_balance: NormalBalance
    raw_current_value: float
    raw_comparison_value: float
    raw_variance_amount: float
    finance_current_value: float
    finance_comparison_value: float
    finance_variance_amount: float
    finance_variance_percentage: Optional[float]
    finance_direction: FinanceDirection


def resolve_normal_balance(caption: str, statement: str) -> NormalBalance:
    """Classify caption polarity for finance-normalized variance."""
    c = (caption or "").strip().lower()
    if statement == "bs":
        return "neutral"
    if c in _SUBTOTAL_NEUTRAL:
        return "neutral"
    if c in _CREDIT_NORMAL_EXACT:
        return "credit"
    if c.startswith("cost of"):
        return "debit"
    for marker in _EXPENSE_NAME_MARKERS:
        if marker in c:
            return "debit"
    if "operating income" in c or c == "net income" or "gross profit" in c:
        return "neutral"
    if "revenue" in c or "sales" in c:
        return "credit"
    if "income" in c and "expense" not in c and "tax" not in c:
        return "credit"
    return "debit"


def _to_float(value: Union[float, Decimal, int, None]) -> float:
    if value is None:
        return 0.0
    return float(value)


def _finance_direction(amount: float) -> FinanceDirection:
    if amount > 0:
        return "increased"
    if amount < 0:
        return "decreased"
    return "flat"


def _finance_pct(
    finance_variance: float,
    finance_comparison: float,
    *,
    normal_balance: NormalBalance,
    raw_comparison: float,
) -> Optional[float]:
    """Percentage on finance movement; None when comparison base is zero."""
    if normal_balance == "credit":
        base = abs(finance_comparison)
        if base == 0:
            return 0.0 if finance_variance == 0 else None
        return (finance_variance / base) * 100.0
    if raw_comparison == 0:
        return 0.0 if finance_variance == 0 else None
    return (finance_variance / abs(raw_comparison)) * 100.0


def compute_finance_variance(
    current: Union[float, Decimal],
    comparison: Union[float, Decimal],
    caption: str,
    statement: str,
) -> FinanceVariance:
    """Derive finance-normalized variance without altering raw stored amounts."""
    cur = _to_float(current)
    cmp = _to_float(comparison)
    raw_var = cur - cmp
    normal_balance = resolve_normal_balance(caption, statement)

    if normal_balance == "credit":
        fin_cur = abs(cur)
        fin_cmp = abs(cmp)
        fin_var = fin_cur - fin_cmp
    else:
        fin_cur = cur
        fin_cmp = cmp
        fin_var = raw_var

    fin_pct = _finance_pct(
        fin_var,
        fin_cmp,
        normal_balance=normal_balance,
        raw_comparison=cmp,
    )

    return FinanceVariance(
        normal_balance=normal_balance,
        raw_current_value=cur,
        raw_comparison_value=cmp,
        raw_variance_amount=raw_var,
        finance_current_value=fin_cur,
        finance_comparison_value=fin_cmp,
        finance_variance_amount=fin_var,
        finance_variance_percentage=fin_pct,
        finance_direction=_finance_direction(fin_var),
    )


def finance_variance_for_threshold(
    current: Decimal,
    comparison: Decimal,
    caption: str,
    statement: str,
) -> tuple[Decimal, Optional[Decimal]]:
    """Return (variance_amount, pct_variance) for materiality threshold checks."""
    fin = compute_finance_variance(current, comparison, caption, statement)
    var = Decimal(str(fin.finance_variance_amount))
    pct: Optional[Decimal] = None
    if fin.finance_variance_percentage is not None:
        pct = Decimal(str(round(fin.finance_variance_percentage, 2)))
    return var, pct


def value_sign_label(
    raw_value: float,
    normal_balance: NormalBalance,
) -> str | None:
    """Short label when a raw ERP value is sign-inverted for its caption type."""
    if raw_value == 0:
        return None
    if normal_balance == "credit" and raw_value < 0:
        return "revenue credit balance"
    if normal_balance == "debit" and raw_value < 0:
        return "net credit / negative cost position"
    return None


def build_value_sign_labels(
    raw_current: float,
    raw_comparison: float,
    normal_balance: NormalBalance,
) -> tuple[str | None, str | None]:
    return (
        value_sign_label(raw_current, normal_balance),
        value_sign_label(raw_comparison, normal_balance),
    )


def format_signed_value_phrase(
    raw_value: float,
    normal_balance: NormalBalance,
    *,
    role: Literal["current", "comparison"] = "current",
) -> str:
    """Finance-friendly phrase for one period value (never leads with a bare negative)."""
    sign = value_sign_label(raw_value, normal_balance)
    if normal_balance == "credit":
        mag = abs(raw_value)
        if sign:
            return f"a {sign} of {format_currency(mag)}"
        return f"a revenue amount of {format_currency(mag)}"
    if normal_balance == "debit":
        if raw_value < 0:
            return (
                f"a net credit / negative cost position of "
                f"{format_currency(abs(raw_value))}"
            )
        if role == "current":
            return f"a current period cost of {format_currency(raw_value)}"
        return f"a comparison period cost of {format_currency(raw_value)}"
    return format_currency(raw_value)


def build_movement_summary_clause(
    raw_current: float,
    raw_comparison: float,
    normal_balance: NormalBalance,
    *,
    comparison_period_label: str = "",
    current_period_label: str = "",
    include_period_labels: bool = False,
) -> str:
    """Clause describing movement between periods using sign-aware wording."""
    comp_p = format_signed_value_phrase(
        raw_comparison, normal_balance, role="comparison",
    )
    cur_p = format_signed_value_phrase(
        raw_current, normal_balance, role="current",
    )
    comp_suffix = f" in {comparison_period_label}" if include_period_labels and comparison_period_label else ""
    cur_suffix = f" in {current_period_label}" if include_period_labels and current_period_label else ""

    if normal_balance == "credit":
        return (
            f"moving from {comp_p}{comp_suffix} to {cur_p}{cur_suffix}"
        )
    if normal_balance == "debit":
        if raw_comparison < 0 and raw_current >= 0:
            base = f"moving from {comp_p}"
            if include_period_labels and comparison_period_label:
                base += f" in {comparison_period_label}"
            base += f" to {cur_p}"
            if include_period_labels and current_period_label:
                base += f" in {current_period_label}"
            return base
        if raw_current < 0 and raw_comparison >= 0:
            base = f"moving from {comp_p}"
            if include_period_labels and comparison_period_label:
                base += f" in {comparison_period_label}"
            base += f" to {cur_p}"
            if include_period_labels and current_period_label:
                base += f" in {current_period_label}"
            return base
        return (
            f"moving from {comp_p}{comp_suffix} to {cur_p}{cur_suffix}"
        )
    return (
        f"moving from {format_currency(raw_comparison)}{comp_suffix} to "
        f"{format_currency(raw_current)}{cur_suffix}"
    )


def variance_bridge_signed_narrative(
    raw_current: float,
    raw_comparison: float,
    normal_balance: NormalBalance,
) -> str | None:
    """Evidence-style variance bridge when sign mix explains the movement."""
    if normal_balance == "debit" and raw_comparison < 0 and raw_current >= 0:
        return (
            "The variance is driven by the swing from prior-period negative cost "
            "adjustments to current-period positive cost activity."
        )
    if normal_balance == "debit" and raw_current < 0 and raw_comparison >= 0:
        return (
            "The variance is driven by the swing from prior-period positive cost "
            "activity to a current-period net credit / negative cost position."
        )
    if normal_balance == "credit" and (raw_current < 0 or raw_comparison < 0):
        return (
            "The variance reflects a change in revenue credit balance magnitude "
            "between the comparison and current periods."
        )
    return None


def normal_balance_explanation(
    raw_current: float,
    raw_comparison: float,
    normal_balance: NormalBalance,
) -> str:
    """Short explanation of sign context for concise/fallback commentary."""
    if normal_balance == "debit" and raw_comparison < 0:
        return (
            "a movement from prior-period credit adjustment / negative cost activity "
            "to current-period cost activity"
        )
    if normal_balance == "debit" and raw_current < 0:
        return (
            "a movement to a current-period net credit / negative cost position"
        )
    if normal_balance == "credit":
        return "a change in revenue credit balance magnitude"
    return "mapped GL account movement between periods"


def format_transaction_amount_label(
    amount: float,
    normal_balance: NormalBalance,
) -> str:
    """Format a transaction amount with sign-aware label for evidence lines."""
    base = format_currency(amount)
    if amount >= 0:
        return base
    if normal_balance == "credit":
        return f"{base} (revenue credit movement)"
    if normal_balance == "debit":
        return f"{base} (credit / negative adjustment)"
    return base


def to_flux_row_out(row: FluxData) -> FluxRowOut:
    """Map a FluxData ORM row to FluxRowOut with finance-normalized fields."""
    cur = _to_float(row.current_value)
    cmp = _to_float(row.comp_value)
    fin = compute_finance_variance(cur, cmp, row.caption, row.statement)
    return FluxRowOut(
        id=row.id,
        caption=row.caption,
        current_value=cur,
        comp_value=cmp,
        pct_variance=float(row.pct_variance) if row.pct_variance is not None else None,
        is_material=bool(row.is_material),
        is_bold=bool(row.is_bold),
        sort_order=int(row.sort_order or 0),
        source_type=row.source_type,
        generated_from=row.generated_from,
        generated_at=row.generated_at,
        normal_balance=fin.normal_balance,
        raw_variance_amount=fin.raw_variance_amount,
        finance_current_value=fin.finance_current_value,
        finance_comparison_value=fin.finance_comparison_value,
        finance_variance_amount=fin.finance_variance_amount,
        finance_variance_percentage=fin.finance_variance_percentage,
        finance_direction=fin.finance_direction,
    )
