# @file    services/slm/slm_errors.py
#
# Stable, frontend-safe error codes for the SLM chat gateway and the Flux AI
# commentary pipeline. Codes are intentionally short and stable so the React
# layer can key on them; messages are user-friendly strings that never leak
# stack traces, prompts, headers, or API keys.

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class SafeAiError:
    code: str
    message: str


SLM_CONFIG_MISSING        = "SLM_CONFIG_MISSING"
SLM_AUTH_FAILED           = "SLM_AUTH_FAILED"
SLM_TIMEOUT               = "SLM_TIMEOUT"
SLM_RATE_LIMITED          = "SLM_RATE_LIMITED"
SLM_HTTP_ERROR            = "SLM_HTTP_ERROR"
SLM_EMPTY_RESPONSE        = "SLM_EMPTY_RESPONSE"
SLM_INVALID_JSON          = "SLM_INVALID_JSON"
SLM_SCHEMA_INVALID        = "SLM_SCHEMA_INVALID"
FLUX_DATA_NOT_FOUND       = "FLUX_DATA_NOT_FOUND"
ENTITY_NOT_FOUND          = "ENTITY_NOT_FOUND"
ENTITY_ACCESS_DENIED      = "ENTITY_ACCESS_DENIED"

# Frontend-facing aliases used by the public Flux AI / ontology / generate
# endpoints so the React layer keys can stay stable and self-explanatory.
AI_CONFIG_MISSING         = "AI_CONFIG_MISSING"
AI_EMPTY_RESPONSE         = "AI_EMPTY_RESPONSE"
AI_INVALID_JSON           = "AI_INVALID_JSON"

# Ontology / generate-from-BC pipeline.
ONTOLOGY_MAPPING_MISSING  = "ONTOLOGY_MAPPING_MISSING"
BC_DATA_NOT_SYNCED        = "BC_DATA_NOT_SYNCED"
FLUX_GENERATION_FAILED    = "FLUX_GENERATION_FAILED"
DEMO_AUTOMAP_NO_MATCHES   = "DEMO_AUTOMAP_NO_MATCHES"


_DEFAULT_MESSAGES: dict[str, str] = {
    SLM_CONFIG_MISSING: (
        "AI service is not configured on the server. "
        "Ask an administrator to set the SLM credentials."
    ),
    SLM_AUTH_FAILED: (
        "AI service rejected the server credentials. "
        "Ask an administrator to verify the SLM API key."
    ),
    SLM_TIMEOUT:        "AI commentary generation timed out. Please retry.",
    SLM_RATE_LIMITED: (
        "AI service rate limit reached. Wait a minute and retry, or ask an "
        "administrator to raise the SLM gateway quota."
    ),
    SLM_HTTP_ERROR:     "AI service returned an error. Please retry shortly.",
    SLM_EMPTY_RESPONSE: "AI service returned an empty response.",
    SLM_INVALID_JSON: (
        "AI service returned an unparseable response. Please retry."
    ),
    SLM_SCHEMA_INVALID: (
        "AI service returned a response that did not match the expected "
        "schema. Please retry."
    ),
    FLUX_DATA_NOT_FOUND: (
        "No flux data is available for this entity, statement and caption "
        "for the active period."
    ),
    ENTITY_NOT_FOUND:     "Entity was not found.",
    ENTITY_ACCESS_DENIED: "You do not have access to this entity.",

    AI_CONFIG_MISSING:    "AI service is not configured on the server.",
    AI_EMPTY_RESPONSE:    "AI service returned an empty response.",
    AI_INVALID_JSON:      "AI service returned an unparseable response.",

    ONTOLOGY_MAPPING_MISSING: (
        "Ontology mapping is empty for this entity. "
        "Add or import a mapping before generating Flux data."
    ),
    BC_DATA_NOT_SYNCED: (
        "Business Central data has not been fully synced for this entity. "
        "Run GL accounts, dimensions, and GL entries syncs before retrying."
    ),
    FLUX_GENERATION_FAILED: (
        "Flux data generation could not complete. Please retry shortly."
    ),
    DEMO_AUTOMAP_NO_MATCHES: (
        "No GL accounts matched the demo keyword rules for this entity. "
        "Verify that BC GL accounts have been synced."
    ),
}


def make_safe_ai_error(code: str, message: str | None = None) -> SafeAiError:
    """Return a `SafeAiError` for `code`. Falls back to a default message
    when the caller does not supply one. Never includes raw exception text
    or HTTP bodies."""
    return SafeAiError(
        code=code,
        message=(message or _DEFAULT_MESSAGES.get(code, "Unexpected AI error.")),
    )
