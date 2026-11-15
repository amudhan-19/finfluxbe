# @file    tests/test_flux_period_config.py
#
# Unit tests for the new period parser. Pure-Python — no DB, no fixtures.

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from app.services.flux_period_config import (
    FluxPeriodConfig,
    build_period_label,
    build_period_range_key,
    expand_period_range_key,
    parse_flux_period_config,
    parse_period_string,
    sanitize_period_storage_string,
)


# ── parse_period_string ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value, expected_start, expected_end",
    [
        ("Mar 2026",        date(2026, 3, 1),  date(2026, 3, 31)),
        ("March 2026",      date(2026, 3, 1),  date(2026, 3, 31)),
        ("2026-03",         date(2026, 3, 1),  date(2026, 3, 31)),
        ("2026-03-15",      date(2026, 3, 1),  date(2026, 3, 31)),
        ("Q1 2026",         date(2026, 1, 1),  date(2026, 3, 31)),
        ("Q4 2025",         date(2025, 10, 1), date(2025, 12, 31)),
        ("2026",            date(2026, 1, 1),  date(2026, 12, 31)),
        ("FY 2025",         date(2025, 1, 1),  date(2025, 12, 31)),
    ],
)
def test_single_segment_parses(
    value: str, expected_start: date, expected_end: date,
) -> None:
    assert parse_period_string(value) == (expected_start, expected_end)


def test_csv_of_months_returns_min_max_window() -> None:
    # Multi-month CSV emitted by the supervisor MonthPicker.
    parsed = parse_period_string("2025-01, 2025-02, 2025-03")
    assert parsed == (date(2025, 1, 1), date(2025, 3, 31))


def test_compact_range_key_parses() -> None:
    parsed = parse_period_string("2025-01..2025-03")
    assert parsed == (date(2025, 1, 1), date(2025, 3, 31))


def test_build_period_range_key_from_csv() -> None:
    assert build_period_range_key("2025-01, 2025-02, 2025-03") == "2025-01..2025-03"
    assert build_period_range_key("2026-03") == "2026-03"
    assert len(build_period_range_key("2025-01, 2025-02, 2026-02")) <= 20


def test_expand_period_range_key() -> None:
    assert expand_period_range_key("2025-01..2025-03") == [
        "2025-01", "2025-02", "2025-03",
    ]


def test_explicit_range_with_to_separator() -> None:
    parsed = parse_period_string("Jan 2026 to Mar 2026")
    assert parsed == (date(2026, 1, 1), date(2026, 3, 31))


def test_explicit_range_with_dash_separator() -> None:
    parsed = parse_period_string("Jan 2026 - Mar 2026")
    assert parsed == (date(2026, 1, 1), date(2026, 3, 31))


def test_mixed_csv_tolerates_unparseable_token() -> None:
    parsed = parse_period_string("2025-01, garbage, Mar 2026")
    assert parsed == (date(2025, 1, 1), date(2026, 3, 31))


@pytest.mark.parametrize("value", [None, "", "   ", "totally not a date"])
def test_unparseable_returns_none(value: str | None) -> None:
    assert parse_period_string(value) is None


# ── build_period_label ─────────────────────────────────────────────────
def test_label_single_month() -> None:
    assert build_period_label(date(2026, 3, 1), date(2026, 3, 31)) == "Mar 2026"


def test_label_multi_month_same_year() -> None:
    label = build_period_label(date(2026, 1, 1), date(2026, 3, 31))
    assert label == "Jan 2026 - Mar 2026"


def test_label_cross_year() -> None:
    label = build_period_label(date(2025, 11, 1), date(2026, 2, 28))
    assert label == "Nov 2025 - Feb 2026"


# ── parse_flux_period_config (entity-shaped) ───────────────────────────
@dataclass
class _FakeEntity:
    period_from: str
    period_to: str
    period_type: str = "MoM"
    id: str = "ENT-X"


def test_seed_convention_period_from_is_current() -> None:
    entity = _FakeEntity(period_from="Mar 2026", period_to="Feb 2026")
    cfg = parse_flux_period_config(entity)
    assert isinstance(cfg, FluxPeriodConfig)
    assert cfg.current_start_date == date(2026, 3, 1)
    assert cfg.current_end_date == date(2026, 3, 31)
    assert cfg.comparison_start_date == date(2026, 2, 1)
    assert cfg.comparison_end_date == date(2026, 2, 28)
    assert cfg.current_label == "Mar 2026"
    assert cfg.comparison_label == "Feb 2026"


def test_supervisor_csv_pickers_resolve_to_ranges() -> None:
    entity = _FakeEntity(
        period_from="2026-01..2026-03",
        period_to="2025-01..2025-03",
    )
    cfg = parse_flux_period_config(entity)
    assert cfg.current_start_date == date(2026, 1, 1)
    assert cfg.current_end_date == date(2026, 3, 31)
    assert cfg.comparison_start_date == date(2025, 1, 1)
    assert cfg.comparison_end_date == date(2025, 3, 31)
    assert cfg.current_label == "Jan 2026 - Mar 2026"
    assert cfg.comparison_label == "Jan 2025 - Mar 2025"


def test_jan_2026_vs_jan_2025_single_month_each() -> None:
    entity = _FakeEntity(period_from="Jan 2026", period_to="Jan 2025")
    cfg = parse_flux_period_config(entity)
    assert cfg.current_start_date == date(2026, 1, 1)
    assert cfg.current_end_date == date(2026, 1, 31)
    assert cfg.comparison_start_date == date(2025, 1, 1)
    assert cfg.comparison_end_date == date(2025, 1, 31)
    assert cfg.is_single_month_each is True


def test_invalid_both_sides_raises() -> None:
    entity = _FakeEntity(period_from="garbage", period_to="also garbage")
    with pytest.raises(ValueError):
        parse_flux_period_config(entity)


def test_half_empty_falls_back_to_other_side() -> None:
    entity = _FakeEntity(period_from="Mar 2026", period_to="")
    cfg = parse_flux_period_config(entity)
    # Comparison mirrored from current — caller still gets a valid window.
    assert cfg.comparison_start_date == cfg.current_start_date


def test_sanitize_strips_legacy_labels_from_csv() -> None:
    assert sanitize_period_storage_string("2025-01, Mar 2026") == "2025-01..2026-03"
    assert sanitize_period_storage_string("2026-01, Feb 2026") == "2026-01..2026-02"
    assert sanitize_period_storage_string("Mar 2026") == "2026-03"
