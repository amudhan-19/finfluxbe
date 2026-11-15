# @file    services/slm/slm_json_utils.py
#
# Tiny tolerant JSON parser used to extract the AI commentary payload from a
# raw SLM response. Models routinely wrap their JSON in ```json fences or
# emit a stray trailing comma, so we keep the cleanup minimal and explicit
# rather than reaching for a full LLM-output parsing library.

from __future__ import annotations
import json
import re
from typing import Any

# Matches ```json … ``` or ``` … ``` fences at the start/end of a payload.
# DOTALL so newlines are captured by the inner group.
_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*(?P<body>.*?)\s*```\s*$",
    re.DOTALL,
)

# Conservative trailing-comma cleanup: only strips a comma that immediately
# precedes a closing brace or bracket. Avoids touching commas inside strings.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def strip_json_code_fences(text: str) -> str:
    """Remove a single outer ```json fenced block if present. Returns the
    input unchanged when no fence is detected."""
    if not text:
        return ""
    m = _FENCE_RE.match(text.strip())
    if m:
        return m.group("body").strip()
    return text.strip()


def _strip_simple_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def parse_loose_json_from_llm_text(text: str) -> Any | None:
    """Attempt to parse the SLM output as JSON.

    Strategy (kept intentionally minimal):
      1. Strip a ```json … ``` fence if the model added one.
      2. Try `json.loads` on the cleaned text.
      3. If that fails, strip trailing commas before `}` / `]` and retry.
      4. As a last resort, try to slice from the first `{` to the last `}`
         (handy when the model prefaces JSON with a sentence or two).

    Returns the parsed object on success, or `None` when every attempt fails.
    Never raises — callers convert `None` into a safe `SLM_INVALID_JSON`.
    """
    if not text or not text.strip():
        return None

    cleaned = strip_json_code_fences(text)

    try:
        return json.loads(cleaned)
    except (ValueError, TypeError):
        pass

    try:
        return json.loads(_strip_simple_trailing_commas(cleaned))
    except (ValueError, TypeError):
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            try:
                return json.loads(_strip_simple_trailing_commas(candidate))
            except (ValueError, TypeError):
                return None
    return None
