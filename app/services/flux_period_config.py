# @file    services/flux_period_config.py
#
# Single source of truth for parsing the entity period strings into the
# date ranges the BC generator consumes and the labels the UI renders.
#
# Why this lives in its own module
# --------------------------------
# Period strings reach the backend in several shapes (originally one label
# like "Mar 2026", later multi-month CSVs like "2025-01, 2025-02, 2025-03"
# emitted by the supervisor MonthPicker, occasionally a Q1/Q4 token, and
# rarely an ISO date). Centralising the parser:
#
#   • lets the generator, the diagnostics endpoint, and the save-and-
#     generate route share *exactly* the same date math (no drift),
#   • keeps `flux_generation.py` focused on aggregation,
#   • makes the parser unit-testable in isolation.

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional


# ── Public dataclass ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class FluxPeriodConfig:
    """Resolved period config for one entity.

    The application convention used by the seed loader, the FluxReview UI,
    and (now) the BC generator is:

        entity.period_from = CURRENT period
        entity.period_to   = COMPARISON period

    ``current_label`` and ``comparison_label`` are sanitized display
    strings — they intentionally drop trailing junk so the UI never
    renders mixed-format CSVs like "2025-01, Mar 2026"."""
    current_start_date:    date
    current_end_date:      date
    comparison_start_date: date
    comparison_end_date:   date
    current_label:         str
    comparison_label:      str
    current_period_raw:    str
    comparison_period_raw: str

    @property
    def is_single_month_each(self) -> bool:
        """True when both sides resolve to a single calendar month."""
        return (
            self.current_start_date.month == self.current_end_date.month
            and self.comparison_start_date.month == self.comparison_end_date.month
        )


# ── Parse primitives ───────────────────────────────────────────────────────
_MONTH_ABBR: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_QUARTER_MONTHS: dict[int, tuple[int, int]] = {
    1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12),
}
_RANGE_SEPARATOR_RE = re.compile(r"\s+(?:to|-|\u2013|\u2014)\s+", re.IGNORECASE)
_SPLIT_LIST_RE = re.compile(r",\s*")
# Compact storage key: ``2025-01..2025-03`` (fits ``VARCHAR(20)`` on entities).
_PERIOD_RANGE_KEY_RE = re.compile(r"^(\d{4}-\d{2})\.\.(\d{4}-\d{2})$")
_MONTH_KEY_RE = re.compile(r"^\d{4}-\d{2}$")


# Calendar helpers — kept tiny so the parser stays linear and readable.
def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    start_month, end_month = _QUARTER_MONTHS[quarter]
    last_day = calendar.monthrange(year, end_month)[1]
    return date(year, start_month, 1), date(year, end_month, last_day)


def _year_bounds(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


# ── Single-segment parser ──────────────────────────────────────────────────
def _parse_single_segment(segment: str) -> Optional[tuple[date, date]]:
    """Parse one period token. Returns ``(start, end)`` or ``None`` when
    the token is unrecognisable. Recognised shapes:

      ``"Mar 2026"``    → 2026-03-01 .. 2026-03-31
      ``"March 2026"``  → same
      ``"2026-03"``     → 2026-03-01 .. 2026-03-31
      ``"2026-03-15"``  → ISO date → its containing month
      ``"Q1 2026"``     → 2026-01-01 .. 2026-03-31
      ``"FY 2026"`` /
      ``"2026"``        → 2026-01-01 .. 2026-12-31
    """
    s = (segment or "").strip()
    if not s:
        return None

    upper = s.upper()
    if upper.startswith("Q") and " " in upper:
        try:
            quarter = int(upper.split()[0][1])
            year = int(upper.split()[1])
            if quarter in _QUARTER_MONTHS:
                return _quarter_bounds(year, quarter)
        except (IndexError, ValueError):
            return None

    parts = s.split()
    if len(parts) == 2:
        month_token = parts[0][:3].lower()
        month = _MONTH_ABBR.get(month_token)
        if month and parts[1].isdigit():
            return _month_bounds(int(parts[1]), month)

    if upper.startswith("FY") and len(parts) == 2 and parts[1].isdigit():
        return _year_bounds(int(parts[1]))

    if s.isdigit() and len(s) == 4:
        return _year_bounds(int(s))

    iso_match = re.match(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$", s)
    if iso_match:
        try:
            year = int(iso_match.group(1))
            month = int(iso_match.group(2))
            return _month_bounds(year, month)
        except ValueError:
            return None
    return None


def _parse_range_segment(segment: str) -> Optional[tuple[date, date]]:
    """A range like ``"Jan 2026 to Mar 2026"`` / ``"Jan 2026 - Mar 2026"``.
    Returns the union ``(min_start, max_end)`` or ``None`` when either
    side is unparseable."""
    parts = _RANGE_SEPARATOR_RE.split(segment.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    left = _parse_single_segment(parts[0])
    right = _parse_single_segment(parts[1])
    if left is None or right is None:
        return None
    return min(left[0], right[0]), max(left[1], right[1])


# ── Public period parser ───────────────────────────────────────────────────
def parse_period_string(value: str | None) -> Optional[tuple[date, date]]:
    """Parse a period string in any supported shape and return the inclusive
    date range that bounds it. Returns ``None`` when the input is empty or
    unparseable. Never raises.

    Supported shapes:
      • single token (see :func:`_parse_single_segment`),
      • range (``"Jan 2026 to Mar 2026"``),
      • CSV of tokens (``"2025-01, 2025-02, 2025-03"``) — the bounds span
        the whole list and we tolerate a few unparseable items as long as
        at least one segment is recognised.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None

    range_key_match = _PERIOD_RANGE_KEY_RE.match(s)
    if range_key_match:
        start = _parse_single_segment(range_key_match.group(1))
        end = _parse_single_segment(range_key_match.group(2))
        if start is not None and end is not None:
            return start[0], end[1]

    direct = _parse_single_segment(s)
    if direct is not None:
        return direct

    range_ = _parse_range_segment(s)
    if range_ is not None:
        return range_

    bounds: list[tuple[date, date]] = []
    for token in _SPLIT_LIST_RE.split(s):
        token = token.strip()
        if not token:
            continue
        parsed = _parse_single_segment(token) or _parse_range_segment(token)
        if parsed is not None:
            bounds.append(parsed)
    if not bounds:
        return None
    return min(b[0] for b in bounds), max(b[1] for b in bounds)


# ── Label sanitiser ────────────────────────────────────────────────────────
_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _format_month_label(d: date) -> str:
    return f"{_MONTH_NAMES[d.month - 1]} {d.year}"


def _expand_date_span_to_month_keys(start: date, end: date) -> list[str]:
    """Expand an inclusive date span into sorted ``YYYY-MM`` month keys."""
    keys: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year:04d}-{month:02d}")
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return keys


def _expand_segment_to_month_keys(segment: str) -> list[str]:
    """Expand one period token into canonical ``YYYY-MM`` month keys.

    Single months become one key; explicit ranges expand to every month in
    the span. Unparseable segments are skipped.
    """
    parsed = _parse_single_segment(segment) or _parse_range_segment(segment)
    if parsed is None:
        return []
    return _expand_date_span_to_month_keys(parsed[0], parsed[1])


def normalize_month_keys(months: list[str]) -> list[str]:
    """Validate, dedupe, and sort ``YYYY-MM`` month keys."""
    valid = [m for m in months if _MONTH_KEY_RE.match(m or "")]
    return sorted(set(valid))


def collect_month_keys_from_period_input(value: str | None) -> list[str]:
    """Expand any supported period input into sorted unique month keys."""
    if not value:
        return []
    s = str(value).strip()
    if not s:
        return []

    range_key_match = _PERIOD_RANGE_KEY_RE.match(s)
    if range_key_match:
        parsed = parse_period_string(s)
        if parsed is not None:
            return _expand_date_span_to_month_keys(*parsed)
        return []

    if _MONTH_KEY_RE.match(s):
        return [s]

    months: set[str] = set()
    for token in _SPLIT_LIST_RE.split(s):
        token = token.strip()
        if not token:
            continue
        if _PERIOD_RANGE_KEY_RE.match(token):
            parsed = parse_period_string(token)
            if parsed is not None:
                months.update(_expand_date_span_to_month_keys(*parsed))
            continue
        if _MONTH_KEY_RE.match(token):
            months.add(token)
            continue
        months.update(_expand_segment_to_month_keys(token))
    return sorted(months)


def build_period_range_key(value: str | None) -> str:
    """Convert any period input to a compact storage key for ``VARCHAR(20)``.

    Single month → ``"2025-01"`` (7 chars).
    Multi-month span → ``"2025-01..2025-03"`` (15 chars).
    """
    months = collect_month_keys_from_period_input(value)
    if not months:
        return ""
    if len(months) == 1:
        return months[0]
    return f"{months[0]}..{months[-1]}"


def expand_period_range_key(value: str | None) -> list[str]:
    """Expand a stored period key back into ``YYYY-MM`` keys for the UI."""
    return collect_month_keys_from_period_input(value)


def sanitize_period_storage_string(value: str | None) -> str:
    """Alias for :func:`build_period_range_key` — kept for callers that
    already import the old name."""
    return build_period_range_key(value)


def period_date_ranges_equal(
    left: Optional[tuple[date, date]],
    right: Optional[tuple[date, date]],
) -> bool:
    """True when two parsed period windows are identical."""
    return left is not None and right is not None and left == right


def resolve_flux_period_keys(
    db,
    entity,
    statement: str,
) -> tuple[str, str]:
    """Pick the ``(period_from, period_to)`` pair stored on ``flux_data``.

    Prefer an exact string match on the entity config. When that yields no
    rows (common after period labels were normalised), fall back to any
    stored pair whose parsed date windows match the entity's active period.
    """
    from app.models.models import FluxData

    entity_from = getattr(entity, "period_from", "") or ""
    entity_to = getattr(entity, "period_to", "") or ""

    exact_q = (
        db.query(FluxData)
        .filter(
            FluxData.entity_id == entity.id,
            FluxData.statement == statement,
            FluxData.period_from == entity_from,
            FluxData.period_to == entity_to,
        )
    )
    if exact_q.count() > 0:
        return entity_from, entity_to

    try:
        target = parse_flux_period_config(entity)
    except ValueError:
        return entity_from, entity_to

    target_current = (target.current_start_date, target.current_end_date)
    target_comp = (target.comparison_start_date, target.comparison_end_date)

    pairs = (
        db.query(FluxData.period_from, FluxData.period_to)
        .filter(
            FluxData.entity_id == entity.id,
            FluxData.statement == statement,
        )
        .distinct()
        .all()
    )
    for stored_from, stored_to in pairs:
        if period_date_ranges_equal(
            parse_period_string(stored_from), target_current,
        ) and period_date_ranges_equal(
            parse_period_string(stored_to), target_comp,
        ):
            return stored_from, stored_to

    return entity_from, entity_to


def build_period_label(start: date, end: date) -> str:
    """Render a finance-friendly label.

    Single month → ``"Mar 2026"``.
    Same calendar year, multi-month → ``"Jan 2026 - Mar 2026"``.
    Cross-year range → same form, both years shown.
    """
    if start.year == end.year and start.month == end.month:
        return _format_month_label(start)
    return f"{_format_month_label(start)} - {_format_month_label(end)}"


# ── Public entrypoint used by routers + service ────────────────────────────
def parse_flux_period_config(entity) -> FluxPeriodConfig:
    """Resolve an :class:`Entity` into a :class:`FluxPeriodConfig`.

    The application convention is ``period_from = current`` and
    ``period_to = comparison`` — matching the seed loader, the FluxReview
    UI, and the supervisor MonthPicker.

    Raises :class:`ValueError` only when *both* sides fail to parse;
    callers can map that to the safe error code ``PERIOD_CONFIG_INVALID``
    without exposing internals.
    """
    raw_current = getattr(entity, "period_from", "") or ""
    raw_comparison = getattr(entity, "period_to", "") or ""

    current = parse_period_string(raw_current)
    comparison = parse_period_string(raw_comparison)
    if current is None and comparison is None:
        raise ValueError(
            "Period configuration is invalid: neither period_from nor "
            "period_to is parseable.",
        )
    # Tolerate a half-empty config: mirror whichever side parsed so the
    # caller still has a valid window. The comparison side is duplicated
    # from the current side when missing — and vice versa — so callers
    # never have to special-case None.
    if current is None and comparison is not None:
        current = comparison
    if comparison is None and current is not None:
        comparison = current
    assert current is not None and comparison is not None  # for type checkers

    return FluxPeriodConfig(
        current_start_date=current[0],
        current_end_date=current[1],
        comparison_start_date=comparison[0],
        comparison_end_date=comparison[1],
        current_label=build_period_label(*current),
        comparison_label=build_period_label(*comparison),
        current_period_raw=raw_current.strip(),
        comparison_period_raw=raw_comparison.strip(),
    )
