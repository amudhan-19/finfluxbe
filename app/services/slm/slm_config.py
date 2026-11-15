# @file    services/slm/slm_config.py
#
# Thin, immutable config wrapper around the SLM_* environment block from
# `app.core.config.settings`. Reading the env via this helper makes the SLM
# client + Flux AI service trivially testable: tests can construct an
# `SlmConfig` directly without touching the global settings singleton.

from __future__ import annotations
from dataclasses import dataclass

from app.core.config import settings


@dataclass(frozen=True)
class SlmConfig:
    """All SLM gateway parameters resolved at call time.

    Sensitive fields (`api_key`) are stored only in this dataclass and are
    never serialized into responses, audit logs, or exception messages.
    """
    chat_url: str
    api_key: str
    project_id: str
    template_id: str
    timeout_seconds: float
    max_output_tokens: int
    concurrency: int

    @property
    def is_configured(self) -> bool:
        """True when the minimum required pair (URL + API key) is present."""
        return bool(self.chat_url) and bool(self.api_key)


def load_slm_config() -> SlmConfig:
    """Load the live SLM configuration from the application settings.

    Returns a fully-formed `SlmConfig` even when values are missing; callers
    should branch on `cfg.is_configured` to render a safe error response
    rather than raising at import time. This keeps the FastAPI app boot
    process resilient when SLM credentials are not yet provisioned.
    """
    return SlmConfig(
        chat_url=settings.SLM_CHAT_URL or "",
        api_key=settings.SLM_CHAT_API_KEY or "",
        project_id=settings.SLM_CHAT_PROJECT_ID or "",
        template_id=settings.SLM_CHAT_TEMPLATE_ID or "",
        timeout_seconds=float(settings.SLM_CHAT_TIMEOUT_SECONDS or 30.0),
        max_output_tokens=int(settings.SLM_CHAT_MAX_OUTPUT_TOKENS or 0),
        concurrency=int(settings.SLM_CHAT_CONCURRENCY or 4),
    )
