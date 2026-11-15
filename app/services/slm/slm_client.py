# @file    services/slm/slm_client.py
#
# Async HTTP client for the FinFlux SLM (PHI-4) chat gateway.
#
# Responsibilities
# ----------------
# • Build the JSON body shape the gateway expects (messages + temperature
#   plus the optional max_tokens / project_id / template_id fields).
# • Send a single POST with `X-API-Key` auth.
# • Extract the assistant text from any of the gateway's known shapes.
# • Convert transport / parsing failures to a stable `SafeAiError` so route
#   handlers can render frontend-safe responses without leaking internals.
#
# Explicitly NOT doing for MVP: retries, streaming, multi-call chunking,
# concurrency throttling. The `concurrency` field on `SlmConfig` is reserved
# for a future batch path.

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from app.services.slm.slm_config import SlmConfig
from app.services.slm.slm_errors import (
    SLM_AUTH_FAILED,
    SLM_CONFIG_MISSING,
    SLM_EMPTY_RESPONSE,
    SLM_HTTP_ERROR,
    SLM_RATE_LIMITED,
    SLM_TIMEOUT,
    SafeAiError,
    make_safe_ai_error,
)


_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlmChatMessage:
    """A single chat-completion message. Mirrors the gateway's wire format."""
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class SlmChatResult:
    """Outcome of a single SLM POST call. Always populated; `text` is empty
    on failure and `error` is `None` on success."""
    ok: bool
    text: str
    http_status: int
    finish_reason: str | None
    error: SafeAiError | None


def _build_body(
    messages: Sequence[SlmChatMessage], cfg: SlmConfig
) -> dict[str, Any]:
    """Assemble the request body. Optional gateway fields are included only
    when populated so we don't ship empty strings or zero-token caps."""
    body: dict[str, Any] = {
        "messages": [m.to_dict() for m in messages],
        "temperature": 0,
    }
    if cfg.max_output_tokens and cfg.max_output_tokens > 0:
        body["max_tokens"] = int(cfg.max_output_tokens)
    if cfg.project_id:
        body["project_id"] = cfg.project_id
    if cfg.template_id:
        body["template_id"] = cfg.template_id
    return body


def _extract_text(payload: Any) -> tuple[str, str | None]:
    """Pull the assistant's text out of a gateway response.

    Tries each known wrapper shape in order:
      1. choices[0].message.content   (OpenAI-style)
      2. choices[0].content           (some gateways flatten message)
      3. top-level content / text / output / reply (plain wrapper)

    Returns ``(text, finish_reason)`` — empty string when nothing matches.
    `finish_reason` is best-effort; callers do not rely on it for control
    flow, only surface it for debugging in logs.
    """
    if not isinstance(payload, dict):
        return "", None

    finish_reason: str | None = None

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            finish_reason = first.get("finish_reason") or finish_reason
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content, finish_reason
            content = first.get("content")
            if isinstance(content, str) and content.strip():
                return content, finish_reason

    for key in ("content", "text", "output", "reply"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val, finish_reason

    return "", finish_reason


def _http_status_to_safe_error(status_code: int, *, detail: str | None = None) -> SafeAiError:
    if status_code in (401, 403):
        return make_safe_ai_error(SLM_AUTH_FAILED)
    if status_code == 429:
        if detail:
            return make_safe_ai_error(
                SLM_RATE_LIMITED,
                f"AI service rate limit reached ({detail}). "
                "Wait for the quota window to reset, or ask an administrator "
                "to raise the SLM gateway limit.",
            )
        return make_safe_ai_error(SLM_RATE_LIMITED)
    return make_safe_ai_error(SLM_HTTP_ERROR)


def _gateway_error_detail(response: httpx.Response) -> str | None:
    """Best-effort parse of a safe, user-facing gateway error string."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("error", "message", "detail"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:200]
    return None


async def slm_chat_request(
    messages: Sequence[SlmChatMessage], cfg: SlmConfig
) -> SlmChatResult:
    """Send a single chat-completion request to the SLM gateway.

    The function never raises: every failure path produces a populated
    `SlmChatResult` with `ok=False` and a `SafeAiError`. This makes route
    handlers very small — they only check `result.ok` and translate.
    """
    if not cfg.is_configured:
        return SlmChatResult(
            ok=False, text="", http_status=0, finish_reason=None,
            error=make_safe_ai_error(SLM_CONFIG_MISSING),
        )

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": cfg.api_key,
    }
    body = _build_body(messages, cfg)
    timeout = httpx.Timeout(cfg.timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(cfg.chat_url, json=body, headers=headers)
    except httpx.TimeoutException:
        # Never log the API key or full body — only mention the URL host.
        _log.warning("SLM chat timeout after %.1fs", cfg.timeout_seconds)
        return SlmChatResult(
            ok=False, text="", http_status=0, finish_reason=None,
            error=make_safe_ai_error(SLM_TIMEOUT),
        )
    except httpx.HTTPError as exc:
        _log.warning("SLM chat transport error: %s", type(exc).__name__)
        return SlmChatResult(
            ok=False, text="", http_status=0, finish_reason=None,
            error=make_safe_ai_error(SLM_HTTP_ERROR),
        )

    if response.status_code >= 400:
        detail = _gateway_error_detail(response) if response.status_code == 429 else None
        safe_err = _http_status_to_safe_error(response.status_code, detail=detail)
        _log.warning("SLM chat HTTP %s%s", response.status_code, f" ({detail})" if detail else "")
        return SlmChatResult(
            ok=False, text="", http_status=response.status_code,
            finish_reason=None,
            error=safe_err,
        )

    try:
        payload: Any = response.json()
    except ValueError:
        return SlmChatResult(
            ok=False, text="", http_status=response.status_code,
            finish_reason=None,
            error=make_safe_ai_error(SLM_EMPTY_RESPONSE),
        )

    text, finish_reason = _extract_text(payload)
    if not text:
        return SlmChatResult(
            ok=False, text="", http_status=response.status_code,
            finish_reason=finish_reason,
            error=make_safe_ai_error(SLM_EMPTY_RESPONSE),
        )

    return SlmChatResult(
        ok=True, text=text, http_status=response.status_code,
        finish_reason=finish_reason, error=None,
    )
