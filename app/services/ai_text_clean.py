# @file    services/ai_text_clean.py
#
# Text-hygiene helpers used everywhere we save or surface SLM-generated
# commentary. Single source of truth so the UI, the prompt builder, and the
# persistence layer cannot drift.
#
# Goals:
#   • Repair UTF-8-as-cp1252 mojibake (â€™, â€", Â, Ã©, …) without breaking
#     legitimate currency / percentage / bullet glyphs.
#   • Strip stray control characters and zero-width artefacts that LLM
#     responses occasionally include.
#   • Drop pictographic emojis from finance commentary while keeping safe
#     punctuation (•, —, –, …, ', ", $, %, ±).
#   • Provide stable currency / percent formatters used by both the prompt
#     builder and any fallback strings the service synthesises.

from __future__ import annotations

from typing import Iterable

# ── Mojibake repair ─────────────────────────────────────────────────────────
# Keys are the cp1252-decoded artefacts most often seen when UTF-8 bytes are
# read as cp1252 / Windows-1252; values are the intended Unicode glyphs.
# Listed by descending key length so we replace the multi-byte tokens before
# any of their lone-byte fallbacks (Â / Ã…) ever match.
_MOJIBAKE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("\u00e2\u20ac\u2122", "\u2019"),      # â€™ -> ’ (right single quote / apostrophe)
    ("\u00e2\u20ac\u0153", "\u201c"),      # â€œ -> “ (left double quote)
    ("\u00e2\u20ac\u009d", "\u201d"),      # â€\x9d -> ” (right double quote)
    ("\u00e2\u20ac\ufffd", "\u201d"),      # â€\ufffd -> ” (replacement-char fallback)
    ("\u00e2\u20ac\u201d", "\u2014"),      # â€" -> — (em dash)
    ("\u00e2\u20ac\u201c", "\u2013"),      # â€" -> – (en dash)
    ("\u00e2\u20ac\u00a2", "\u2022"),      # â€¢ -> • (bullet)
    ("\u00e2\u20ac\u00a6", "\u2026"),      # â€¦ -> … (ellipsis)
    ("\u00e2\u20ac\u02dc", "\u2018"),      # â€˜ -> ‘ (left single quote)
    ("\u00c3\u00a9", "\u00e9"),            # Ã© -> é
    ("\u00c3\u00a8", "\u00e8"),            # Ã¨ -> è
    ("\u00c3\u00aa", "\u00ea"),            # Ãª -> ê
    ("\u00c3\u00b6", "\u00f6"),            # Ã¶ -> ö
    ("\u00c3\u00bc", "\u00fc"),            # Ã¼ -> ü
    ("\u00c3\u00b1", "\u00f1"),            # Ã± -> ñ
    ("\u00c3\u0083", "\u00c0"),            # ÃƒÀ — rare bridge artefact
)

# Single Â characters that aren't followed by a paired byte are usually a
# stray non-breaking-space artefact ("Â ") and should just be removed so the
# rendered string keeps the surrounding plain space.
_BARE_NBSP_ARTEFACT = "\u00c2\u00a0"     # Â + NBSP
_BARE_A_HAT = "\u00c2"                   # lone Â (only stripped after roundtrip)

# Mojibake markers that justify attempting a cp1252 -> utf-8 round-trip
# before reaching for the literal lookup table. Keeping this list short
# avoids touching strings that legitimately contain Â / Ã in finance text.
_MOJIBAKE_MARKERS: tuple[str, ...] = (
    "\u00e2\u20ac",   # â€ — most common UTF-8 punctuation mojibake
    "\u00c3\u00a9",   # Ã©
    "\u00c3\u00a8",   # Ã¨
    "\u00c3\u00aa",   # Ãª
    "\u00c3\u00b6",   # Ã¶
    "\u00c3\u00bc",   # Ã¼
)


# ── Emoji ranges to strip from finance text ────────────────────────────────
# Tuples are inclusive (start, end). General-Punctuation (0x2000-0x206F) is
# deliberately excluded so bullet (•), em/en dash (—, –) and ellipsis (…)
# survive. Currency-Symbols (0x20A0-0x20CF) is also kept intact.
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F300, 0x1F6FF),   # Misc Symbols & Pictographs, Transport & Map
    (0x1F900, 0x1F9FF),   # Supplemental Symbols & Pictographs
    (0x1FA00, 0x1FAFF),   # Symbols & Pictographs Extended-A
    (0x1F000, 0x1F02F),   # Mahjong tiles
    (0x1F0A0, 0x1F0FF),   # Playing cards
    (0x2600,  0x27BF),    # Misc Symbols + Dingbats (drops ⚠ ✔ ✖ ✨ ↩ etc.)
)


def _is_emoji_codepoint(ch: str) -> bool:
    cp = ord(ch)
    for start, end in _EMOJI_RANGES:
        if start <= cp <= end:
            return True
    # Variation selectors and zero-width joiners glue emoji sequences
    # together; remove them too so we don't leave dangling combiners.
    return cp in (0xFE0E, 0xFE0F, 0x200D)


def _try_cp1252_roundtrip(text: str) -> str:
    """Re-decode text as if its underlying bytes were UTF-8 that has been
    incorrectly read as cp1252. Returns the original string if the roundtrip
    fails for any reason — never raises."""
    try:
        return text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _strip_unsafe_chars(text: str, *, drop_emoji: bool) -> str:
    """Remove control characters except newline / tab. Optionally also drop
    emoji codepoints. Implemented as a single pass for clarity."""
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
            continue
        if cp < 0x20 or cp == 0x7F:
            continue
        if cp in (0xFEFF, 0x200B):     # BOM, zero-width-space
            continue
        if drop_emoji and _is_emoji_codepoint(ch):
            continue
        out.append(ch)
    return "".join(out)


def clean_ai_text(text: str | None, *, drop_emoji: bool = True) -> str:
    """Return a display-safe version of ``text``.

    Rules (deliberately conservative):
      1. ``None`` / empty / whitespace-only inputs become ``""``.
      2. If the string carries a known mojibake marker, attempt a single
         cp1252 -> utf-8 roundtrip.
      3. Apply the literal mojibake replacement table as a safety net.
      4. Strip stray "Â " (cp1252 NBSP artefact).
      5. Drop control chars + (optionally) emoji codepoints.
      6. Trim outer whitespace.

    The ``drop_emoji`` flag is opt-out so callers that *do* want to keep
    pictographs (e.g. macro factor icons supplied by users) can pass
    ``drop_emoji=False``."""
    if text is None:
        return ""
    s = str(text)
    if not s.strip():
        return ""

    if any(marker in s for marker in _MOJIBAKE_MARKERS):
        s = _try_cp1252_roundtrip(s)

    for bad, good in _MOJIBAKE_REPLACEMENTS:
        if bad in s:
            s = s.replace(bad, good)

    s = s.replace(_BARE_NBSP_ARTEFACT, "\u00a0")
    s = s.replace(_BARE_A_HAT, "")

    s = _strip_unsafe_chars(s, drop_emoji=drop_emoji)
    return s.strip()


def clean_ai_text_iter(values: Iterable[str | None], *, drop_emoji: bool = True) -> list[str]:
    """Apply :func:`clean_ai_text` to each item, dropping empty results."""
    out: list[str] = []
    for v in values:
        cleaned = clean_ai_text(v, drop_emoji=drop_emoji)
        if cleaned:
            out.append(cleaned)
    return out


# ── Currency / percent formatting ──────────────────────────────────────────
def format_currency(value: float | int | None, *, symbol: str = "$") -> str:
    """Render a numeric variance amount in finance style: ``$308,864`` or
    ``-$12,345``. ``None`` becomes ``"n/a"``. Decimals beyond the cent are
    truncated to integer dollars when the value is large; small magnitudes
    (< 100) keep two decimal places so percentages-as-amounts still read
    sensibly. Never raises — non-numeric inputs return ``"n/a"``.
    """
    if value is None:
        return "n/a"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "n/a"

    sign = "-" if amount < 0 else ""
    magnitude = abs(amount)
    if magnitude >= 100:
        body = f"{magnitude:,.0f}"
    else:
        body = f"{magnitude:,.2f}"
    return f"{sign}{symbol}{body}"


def format_percent(value: float | int | None, *, decimals: int = 0) -> str:
    """Render a percentage with an explicit sign: ``+67%``, ``-12%``.
    ``None`` becomes ``"n/a"``. ``decimals`` controls the precision —
    keep at 0 for headline commentary, bump to 1 when called from rich
    drill-down views."""
    if value is None:
        return "n/a"
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return "n/a"

    sign = "+" if pct >= 0 else "-"
    body = f"{abs(pct):,.{decimals}f}"
    return f"{sign}{body}%"
