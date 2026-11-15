# @file    services/erp/safe_errors.py
#
# Maps connector exceptions to safe (code, message) pairs that are safe to
# surface to the frontend or persist in audit / sync history. NEVER returns
# stack traces, response bodies, tokens, or auth headers.

from __future__ import annotations
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class SafeError:
    code: str
    message: str


# Sentinel error codes — keep names stable; the frontend may key on them.
AUTH_FAILED         = "AUTH_FAILED"
PERMISSION_DENIED   = "PERMISSION_DENIED"
BC_APP_NOT_ENABLED  = "BC_APP_NOT_ENABLED"
ENVIRONMENT_NOT_FOUND = "ENVIRONMENT_NOT_FOUND"
COMPANY_NOT_FOUND   = "COMPANY_NOT_FOUND"
ENTITY_ERP_MAPPING_REQUIRED = "ENTITY_ERP_MAPPING_REQUIRED"
ENDPOINT_NOT_FOUND  = "ENDPOINT_NOT_FOUND"
BC_CUSTOMER_LEDGER_ENDPOINT_NOT_FOUND = "BC_CUSTOMER_LEDGER_ENDPOINT_NOT_FOUND"
BC_VENDOR_LEDGER_ENDPOINT_NOT_FOUND   = "BC_VENDOR_LEDGER_ENDPOINT_NOT_FOUND"
CUSTOMER_VENDOR_DATA_NOT_AVAILABLE    = "CUSTOMER_VENDOR_DATA_NOT_AVAILABLE"
REQUIRED_BC_LAYERS_NOT_READY          = "REQUIRED_BC_LAYERS_NOT_READY"
GL_ACCOUNTS_NOT_SYNCED                = "GL_ACCOUNTS_NOT_SYNCED"
GL_ENTRIES_NOT_SYNCED                 = "GL_ENTRIES_NOT_SYNCED"
RATE_LIMITED        = "RATE_LIMITED"
NETWORK_TIMEOUT     = "NETWORK_TIMEOUT"
BC_SERVICE_ERROR    = "BC_SERVICE_ERROR"
INVALID_CONFIG      = "INVALID_CONFIG"
DIMENSIONS_NOT_AVAILABLE             = "DIMENSIONS_NOT_AVAILABLE"
GENERAL_LEDGER_ENTRIES_NOT_AVAILABLE = "GENERAL_LEDGER_ENTRIES_NOT_AVAILABLE"
DB_SCHEMA_MISMATCH                   = "DB_SCHEMA_MISMATCH"
DATABASE_ERROR                       = "DATABASE_ERROR"

# Auto-sync orchestrator codes — surfaced through ErpSyncRun.safe_error_code
# and the new admin auto-sync endpoints. Stable strings so the frontend can
# key on them.
BC_AUTO_SYNC_DISABLED      = "BC_AUTO_SYNC_DISABLED"
ENTITY_NOT_ELIGIBLE        = "ENTITY_NOT_ELIGIBLE"
CREDENTIAL_NOT_VERIFIED    = "CREDENTIAL_NOT_VERIFIED"
COMPANY_NOT_SELECTED       = "COMPANY_NOT_SELECTED"
SYNC_ALREADY_RUNNING       = "SYNC_ALREADY_RUNNING"
ONTOLOGY_MAPPING_MISSING   = "ONTOLOGY_MAPPING_MISSING"
FLUX_GENERATION_FAILED     = "FLUX_GENERATION_FAILED"

# Period configuration / BC range validation — surfaced by the supervisor
# Save & Generate flow and the flux-generation-diagnostics endpoint.
PERIOD_CONFIG_INVALID                = "PERIOD_CONFIG_INVALID"
PERIOD_SAVE_FAILED                   = "PERIOD_SAVE_FAILED"
NO_FLUX_ROWS_GENERATED               = "NO_FLUX_ROWS_GENERATED"
SELECTED_PERIOD_OUTSIDE_BC_RANGE     = "SELECTED_PERIOD_OUTSIDE_BC_RANGE"
BC_TRANSACTIONS_NOT_SYNCED           = "BC_TRANSACTIONS_NOT_SYNCED"
NO_MATCHING_TRANSACTIONS_FOR_MAPPING = "NO_MATCHING_TRANSACTIONS_FOR_MAPPING"

# PL-specific narrow codes for the supervisor "PL is empty" diagnosis. The
# generic codes above keep working for BS / both — these only fire when
# we know exactly which statement failed.
PL_ONTOLOGY_MAPPING_MISSING          = "PL_ONTOLOGY_MAPPING_MISSING"
PL_NO_MAPPED_GL_ACCOUNTS             = "PL_NO_MAPPED_GL_ACCOUNTS"
PL_NO_MATCHING_TRANSACTIONS          = "PL_NO_MATCHING_TRANSACTIONS"
STATEMENT_KEY_MISMATCH               = "STATEMENT_KEY_MISMATCH"
PERIOD_KEY_MISMATCH                  = "PERIOD_KEY_MISMATCH"

# QuickBooks Online connector — stable codes for the Admin UI.
QUICKBOOKS_OAUTH_FAILED              = "QUICKBOOKS_OAUTH_FAILED"
QUICKBOOKS_TOKEN_EXPIRED             = "QUICKBOOKS_TOKEN_EXPIRED"
QUICKBOOKS_REFRESH_FAILED            = "QUICKBOOKS_REFRESH_FAILED"
QUICKBOOKS_COMPANY_INFO_FAILED       = "QUICKBOOKS_COMPANY_INFO_FAILED"
QUICKBOOKS_ACCOUNTS_SYNC_FAILED      = "QUICKBOOKS_ACCOUNTS_SYNC_FAILED"
QUICKBOOKS_GL_SYNC_FAILED            = "QUICKBOOKS_GL_SYNC_FAILED"
QUICKBOOKS_GL_NO_TRANSACTIONS        = "QUICKBOOKS_GL_NO_TRANSACTIONS"
QUICKBOOKS_GL_PARSE_EMPTY            = "QUICKBOOKS_GL_PARSE_EMPTY"
QUICKBOOKS_CUSTOMERS_SYNC_FAILED     = "QUICKBOOKS_CUSTOMERS_SYNC_FAILED"
QUICKBOOKS_VENDORS_SYNC_FAILED       = "QUICKBOOKS_VENDORS_SYNC_FAILED"
QUICKBOOKS_REALM_ID_MISSING          = "QUICKBOOKS_REALM_ID_MISSING"
QUICKBOOKS_NOT_CONFIGURED            = "QUICKBOOKS_NOT_CONFIGURED"
QUICKBOOKS_SERVICE_ERROR             = "QUICKBOOKS_SERVICE_ERROR"


_SAFE_MESSAGES = {
    AUTH_FAILED:           "Authentication with Business Central failed.",
    PERMISSION_DENIED:     "Business Central denied permission for this operation.",
    BC_APP_NOT_ENABLED:    "Business Central app registration is not enabled for this tenant.",
    ENVIRONMENT_NOT_FOUND: "Business Central environment was not found.",
    COMPANY_NOT_FOUND:     "Business Central company was not found.",
    ENTITY_ERP_MAPPING_REQUIRED: (
        "This entity is not mapped to Business Central. "
        "Connect the entity to Business Central from Integrations first."
    ),
    ENDPOINT_NOT_FOUND:    "Business Central endpoint was not found.",
    BC_CUSTOMER_LEDGER_ENDPOINT_NOT_FOUND: (
        "Customer/Vendor ledger entries are not exposed in this Business Central API. "
        "Custom API pages are required for ledger-level details."
    ),
    BC_VENDOR_LEDGER_ENDPOINT_NOT_FOUND: (
        "Customer/Vendor ledger entries are not exposed in this Business Central API. "
        "Custom API pages are required for ledger-level details."
    ),
    CUSTOMER_VENDOR_DATA_NOT_AVAILABLE: (
        "Customer/Vendor ledger endpoints are not available in this Business Central API."
    ),
    REQUIRED_BC_LAYERS_NOT_READY: (
        "Business Central GL accounts and general ledger entries must be synced first."
    ),
    GL_ACCOUNTS_NOT_SYNCED: "Business Central GL accounts have not been synced yet.",
    GL_ENTRIES_NOT_SYNCED:  "Business Central general ledger entries have not been synced yet.",
    RATE_LIMITED:          "Business Central rate limit reached. Try again shortly.",
    NETWORK_TIMEOUT:       "Request to Business Central timed out.",
    BC_SERVICE_ERROR:      "Business Central service error.",
    INVALID_CONFIG:        "Stored Business Central configuration is invalid.",
    DIMENSIONS_NOT_AVAILABLE: (
        "Business Central did not return any dimensions for this company. "
        "Confirm dimensions are configured in the BC environment."
    ),
    GENERAL_LEDGER_ENTRIES_NOT_AVAILABLE: (
        "Business Central did not return any general ledger entries for this "
        "company in the requested period."
    ),
    DB_SCHEMA_MISMATCH: (
        "Business Central sync failed because the database schema is not "
        "aligned with the application. Please run database migrations."
    ),
    DATABASE_ERROR: (
        "A database error prevented this operation from completing. "
        "Please retry; if the problem persists, contact an administrator."
    ),

    BC_AUTO_SYNC_DISABLED: (
        "Business Central auto-sync is disabled on this server. "
        "Set BC_AUTO_SYNC_ENABLED=true to enable."
    ),
    ENTITY_NOT_ELIGIBLE: (
        "Entity is not eligible for Business Central auto-sync."
    ),
    CREDENTIAL_NOT_VERIFIED: (
        "Business Central credentials are not verified for this entity."
    ),
    COMPANY_NOT_SELECTED: (
        "No active Business Central company is selected for this entity."
    ),
    SYNC_ALREADY_RUNNING: (
        "A Business Central sync is already running for this entity."
    ),
    ONTOLOGY_MAPPING_MISSING: (
        "Ontology mapping is empty for this entity. "
        "Add a mapping before generating Flux from BC."
    ),
    FLUX_GENERATION_FAILED: (
        "Flux data generation from Business Central could not complete."
    ),

    PERIOD_CONFIG_INVALID: (
        "Period configuration is invalid. Provide a valid current and "
        "comparison period (e.g. 'Mar 2026' or '2026-01..2026-03')."
    ),
    PERIOD_SAVE_FAILED: (
        "Period configuration could not be saved. Use a compact month "
        "range (e.g. '2025-01..2025-03') and try again."
    ),
    NO_FLUX_ROWS_GENERATED: (
        "Period saved but no Flux rows were generated from Business "
        "Central for the selected window."
    ),
    SELECTED_PERIOD_OUTSIDE_BC_RANGE: (
        "Selected period is outside the synced Business Central data "
        "range. Sync more transactions or pick a period that overlaps "
        "the available data."
    ),
    BC_TRANSACTIONS_NOT_SYNCED: (
        "No Business Central transactions are synced for this entity. "
        "Run a GL entry sync before generating Flux."
    ),
    NO_MATCHING_TRANSACTIONS_FOR_MAPPING: (
        "Mapped GL accounts have no matching transactions in the "
        "selected period."
    ),

    PL_ONTOLOGY_MAPPING_MISSING: (
        "Profit & Loss ontology has no mappings yet. Add P&L captions "
        "in Admin → Ontology Mapping before generating PL Flux."
    ),
    PL_NO_MAPPED_GL_ACCOUNTS: (
        "Profit & Loss captions are present but none have GL accounts "
        "assigned. Map at least one GL account per P&L caption."
    ),
    PL_NO_MATCHING_TRANSACTIONS: (
        "Mapped P&L GL accounts have no transactions in the selected "
        "period. Verify the P&L mapping or pick a period with synced "
        "transactions."
    ),
    STATEMENT_KEY_MISMATCH: (
        "Statement key was not recognised. Use 'pl', 'bs', or 'both'."
    ),
    PERIOD_KEY_MISMATCH: (
        "Generated rows used a period key that the Flux Review query "
        "does not match. Re-save the entity period and regenerate."
    ),

    QUICKBOOKS_OAUTH_FAILED: "QuickBooks authorization failed. Try connecting again.",
    QUICKBOOKS_TOKEN_EXPIRED: "QuickBooks access token expired. Reconnect or retry sync.",
    QUICKBOOKS_REFRESH_FAILED: "QuickBooks token refresh failed. Reconnect QuickBooks.",
    QUICKBOOKS_COMPANY_INFO_FAILED: "Could not load QuickBooks company information.",
    QUICKBOOKS_ACCOUNTS_SYNC_FAILED: "QuickBooks chart of accounts sync failed.",
    QUICKBOOKS_GL_SYNC_FAILED: "QuickBooks general ledger sync failed.",
    QUICKBOOKS_GL_NO_TRANSACTIONS: (
        "No General Ledger transactions found for the selected QuickBooks period."
    ),
    QUICKBOOKS_GL_PARSE_EMPTY: (
        "QuickBooks report was received, but no transaction rows were parsed. "
        "Please check parser mapping."
    ),
    QUICKBOOKS_CUSTOMERS_SYNC_FAILED: "QuickBooks customers sync failed.",
    QUICKBOOKS_VENDORS_SYNC_FAILED: "QuickBooks vendors sync failed.",
    QUICKBOOKS_REALM_ID_MISSING: "QuickBooks company (realmId) is not connected.",
    QUICKBOOKS_NOT_CONFIGURED: "QuickBooks is not configured for this entity.",
    QUICKBOOKS_SERVICE_ERROR: "QuickBooks service error.",
}


def safe_error(code: str, message: str | None = None) -> SafeError:
    return SafeError(code=code, message=message or _SAFE_MESSAGES.get(code, "Unexpected error."))


def _classify_status(status_code: int, body_text: str) -> str:
    """Map an HTTP status (and a tiny snippet of body) to a safe error code.

    Body is only inspected for well-known short markers; never returned.
    """
    if status_code == 401:
        return AUTH_FAILED
    if status_code == 403:
        if "AppNotEnabled" in body_text or "App not enabled" in body_text:
            return BC_APP_NOT_ENABLED
        return PERMISSION_DENIED
    if status_code == 404:
        if "environment" in body_text.lower():
            return ENVIRONMENT_NOT_FOUND
        if "company" in body_text.lower():
            return COMPANY_NOT_FOUND
        return ENDPOINT_NOT_FOUND
    if status_code == 429:
        return RATE_LIMITED
    if 500 <= status_code < 600:
        return BC_SERVICE_ERROR
    return BC_SERVICE_ERROR


def from_exception(exc: BaseException) -> SafeError:
    """Convert any connector exception to a SafeError. Stack traces never leak."""
    if isinstance(exc, httpx.TimeoutException):
        return safe_error(NETWORK_TIMEOUT)
    if isinstance(exc, httpx.HTTPStatusError):
        body_snippet = (exc.response.text or "")[:200] if exc.response is not None else ""
        return safe_error(_classify_status(exc.response.status_code, body_snippet))
    if isinstance(exc, httpx.HTTPError):
        return safe_error(BC_SERVICE_ERROR, "Could not reach Business Central.")
    if isinstance(exc, ValueError):
        return safe_error(INVALID_CONFIG, str(exc) if str(exc) else None)
    return safe_error(BC_SERVICE_ERROR)
