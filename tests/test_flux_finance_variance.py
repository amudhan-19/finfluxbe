# @file    tests/test_flux_finance_variance.py

from __future__ import annotations

import pytest

from app.services.flux_finance_variance import (
    build_movement_summary_clause,
    compute_finance_variance,
    format_signed_value_phrase,
    format_transaction_amount_label,
    resolve_normal_balance,
    value_sign_label,
    variance_bridge_signed_narrative,
)


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("Product Revenue", "credit"),
        ("Revenue", "credit"),
        ("Total Revenue", "credit"),
        ("Cost of Revenue", "debit"),
        ("Salaries & Wages", "debit"),
        ("Gross Profit", "neutral"),
        ("Operating Income", "neutral"),
        ("Net Income", "neutral"),
    ],
)
def test_resolve_normal_balance(caption: str, expected: str) -> None:
    assert resolve_normal_balance(caption, "pl") == expected


def test_product_revenue_decrease_credit_normal() -> None:
    """Demo case: revenue magnitude fell despite positive raw variance."""
    fin = compute_finance_variance(-19958.94, -97628.70, "Product Revenue", "pl")
    assert fin.normal_balance == "credit"
    assert fin.raw_variance_amount == pytest.approx(77669.76, rel=1e-4)
    assert fin.finance_variance_amount == pytest.approx(-77669.76, rel=1e-4)
    assert fin.finance_direction == "decreased"
    assert fin.finance_variance_percentage == pytest.approx(-79.6, abs=0.1)


def test_product_revenue_increase_credit_normal() -> None:
    fin = compute_finance_variance(-80000, -50000, "Product Revenue", "pl")
    assert fin.finance_direction == "increased"
    assert fin.finance_variance_amount == pytest.approx(30000, rel=1e-4)


def test_product_revenue_decrease_case_2() -> None:
    fin = compute_finance_variance(-20000, -50000, "Product Revenue", "pl")
    assert fin.finance_direction == "decreased"
    assert fin.finance_variance_amount == pytest.approx(-30000, rel=1e-4)


def test_expense_increase_debit_normal() -> None:
    fin = compute_finance_variance(80000, 50000, "Salaries & Wages", "pl")
    assert fin.normal_balance == "debit"
    assert fin.finance_direction == "increased"
    assert fin.finance_variance_amount == pytest.approx(30000, rel=1e-4)


def test_expense_decrease_debit_normal() -> None:
    fin = compute_finance_variance(20000, 50000, "Salaries & Wages", "pl")
    assert fin.finance_direction == "decreased"
    assert fin.finance_variance_amount == pytest.approx(-30000, rel=1e-4)


def test_balance_sheet_uses_neutral_raw_logic() -> None:
    fin = compute_finance_variance(100000, 80000, "Cash", "bs")
    assert fin.normal_balance == "neutral"
    assert fin.finance_variance_amount == pytest.approx(20000, rel=1e-4)


def test_debit_negative_comparison_sign_label() -> None:
    assert value_sign_label(-280681.0, "debit") == "net credit / negative cost position"
    phrase = format_signed_value_phrase(-280681.0, "debit", role="comparison")
    assert "net credit / negative cost position" in phrase
    assert "280,681" in phrase
    assert phrase.startswith("a ")


def test_cost_of_revenue_movement_clause() -> None:
    clause = build_movement_summary_clause(
        18191.20, -280681.0, "debit", include_period_labels=False,
    )
    assert "net credit / negative cost position" in clause
    assert "current period cost" in clause


def test_revenue_credit_balance_phrase() -> None:
    clause = build_movement_summary_clause(
        -19958.94, -97628.70, "credit", include_period_labels=False,
    )
    assert "revenue credit balance" in clause


def test_variance_bridge_signed_debit() -> None:
    narrative = variance_bridge_signed_narrative(18191.20, -280681.0, "debit")
    assert narrative is not None
    assert "negative cost adjustments" in narrative


def test_transaction_amount_label_expense_credit() -> None:
    label = format_transaction_amount_label(-5000.0, "debit")
    assert "credit / negative adjustment" in label
