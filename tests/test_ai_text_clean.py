# @file    tests/test_ai_text_clean.py
#
# Smoke tests for the shared AI text-hygiene helpers.

from __future__ import annotations

from app.services.ai_text_clean import (
    clean_ai_text,
    clean_ai_text_iter,
    format_currency,
    format_percent,
)


def test_clean_ai_text_empty_inputs() -> None:
    assert clean_ai_text(None) == ""
    assert clean_ai_text("") == ""
    assert clean_ai_text("   ") == ""


def test_clean_ai_text_repairs_em_dash_mojibake() -> None:
    raw = "Prepaid Expenses \u00e2\u20ac\u201d drivers"
    assert clean_ai_text(raw) == "Prepaid Expenses \u2014 drivers"


def test_clean_ai_text_repairs_smart_apostrophe() -> None:
    raw = "It\u00e2\u20ac\u2122s a material variance."
    assert clean_ai_text(raw) == "It\u2019s a material variance."


def test_clean_ai_text_drops_emoji_by_default() -> None:
    raw = "Material \u26a0\ufe0f review required."
    assert clean_ai_text(raw) == "Material  review required."


def test_clean_ai_text_keeps_safe_punctuation() -> None:
    raw = "Drivers:\n\u2022 Volume/mix \u2014 ~$185,318\n\u2022 Rate/price \u2013 ~$123,546"
    cleaned = clean_ai_text(raw)
    assert "\u2022" in cleaned         # bullet preserved
    assert "\u2014" in cleaned         # em dash preserved
    assert "\u2013" in cleaned         # en dash preserved
    assert "$185,318" in cleaned
    assert "$123,546" in cleaned


def test_clean_ai_text_strips_control_chars_but_keeps_newlines() -> None:
    raw = "summary\nline\twith\x07bell"
    cleaned = clean_ai_text(raw)
    assert "\x07" not in cleaned
    assert "\n" in cleaned
    assert "\t" in cleaned


def test_clean_ai_text_iter_drops_empty_after_cleanup() -> None:
    items = ["hello", "", "   ", None, "world"]
    assert clean_ai_text_iter(items) == ["hello", "world"]


def test_format_currency_sign_and_thousands() -> None:
    # Magnitudes < 100 keep two decimals so cents-style amounts read naturally.
    assert format_currency(0) == "$0.00"
    assert format_currency(308864) == "$308,864"
    assert format_currency(-12345.67) == "-$12,346"
    assert format_currency(99.5) == "$99.50"
    assert format_currency(None) == "n/a"
    assert format_currency("not-a-number") == "n/a"


def test_format_percent_explicit_sign() -> None:
    assert format_percent(67) == "+67%"
    assert format_percent(-12) == "-12%"
    assert format_percent(0) == "+0%"
    assert format_percent(None) == "n/a"
    assert format_percent(1.234, decimals=2) == "+1.23%"
