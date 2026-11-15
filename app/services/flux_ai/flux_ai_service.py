# @file    services/flux_ai/flux_ai_service.py
#
# Orchestrates the synchronous MVP path for generating Flux AI commentary:
#
#     route ──▶ generate_flux_ai_commentary()
#                    │
#                    ├── load Entity / FluxData / OntologyMapping rows
#                    ├── enrich with ERP GL + dimension labels (best-effort)
#                    ├── build prompt context
#                    ├── call SLM gateway via slm_client.slm_chat_request
#                    ├── parse + validate JSON
#                    └── upsert AiCommentary (preserving edited_text)
#
# The whole flow is intentionally synchronous (no queue, no streaming) so it
# can be invoked directly from a FastAPI route handler. Any failure produces
# a `FluxAiResult` with `ok=False` and a `SafeAiError`; the route translates
# that into a frontend-safe response.

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.flux_ai.flux_ai_evidence import FluxAiEvidence

from sqlalchemy.orm import Session

from app.core.audit import log_change
from app.core.deps import can_access_entity
from app.models.models import (
    AiCommentary,
    Comment,
    Entity,
    ErpDimensionValue,
    ErpGeneralLedgerEntry,
    ErpGlAccount,
    FluxData,
    OntologyMapping,
    Threshold,
    User,
)
from app.services.ai_text_clean import (
    clean_ai_text,
    clean_ai_text_iter,
    format_currency,
    format_percent,
)
from app.services.flux_ai.caption_contracts import (
    CAPTION_CONTRACT_VERSION,
    contract_prompt_block,
    resolve_caption_contract,
)
from app.services.flux_ai.flux_ai_prompt_service import (
    PROMPT_VERSION,
    FluxPromptContext,
    build_flux_ai_system_prompt,
    build_flux_ai_user_prompt,
    resolved_finance_bridge_word,
    resolved_finance_direction,
)
from app.services.flux_finance_variance import (
    build_movement_summary_clause,
    build_value_sign_labels,
    compute_finance_variance,
    normal_balance_explanation,
    variance_bridge_signed_narrative,
)
from app.services.flux_ai.party_context import is_stale_ledger_unavailable_line
from app.services.slm.slm_client import (
    SlmChatMessage,
    SlmChatResult,
    slm_chat_request,
)
from app.services.slm.slm_config import SlmConfig, load_slm_config
from app.services.slm.slm_errors import (
    ENTITY_ACCESS_DENIED,
    ENTITY_NOT_FOUND,
    FLUX_DATA_NOT_FOUND,
    SLM_INVALID_JSON,
    SLM_SCHEMA_INVALID,
    SafeAiError,
    make_safe_ai_error,
)
from app.services.slm.slm_json_utils import parse_loose_json_from_llm_text


_log = logging.getLogger(__name__)


# ── Public result type ──────────────────────────────────────────────────────
@dataclass
class FluxAiResult:
    """Return value from `generate_flux_ai_commentary`.

    Successful runs populate `concise_text` / `detailed_text` along with the
    AI-derived ``drivers``, ``mapped_accounts``, ``transaction_notes``,
    ``review_notes``, ``recommended_actions``, and ``confidence_score``.
    Failures populate ``error`` and leave the text fields empty.

    The legacy ``variance_drivers`` (list of structured driver dicts) and
    ``risk_flags`` are still surfaced for backwards-compatibility with
    existing callers; they are populated only when the SLM emits the older
    schema. New code should prefer the simple ``drivers`` string list.
    """
    ok: bool
    entity_id: str
    statement: str
    caption: str
    concise_text: str = ""
    detailed_text: str = ""
    drivers: list[str] = field(default_factory=list)
    mapped_accounts: list[str] = field(default_factory=list)
    customer_vendor_notes: list[str] = field(default_factory=list)
    transaction_notes: list[str] = field(default_factory=list)
    review_notes: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    confidence_score: float = 0.0
    party_diagnostics: dict[str, Any] = field(default_factory=dict)
    # Legacy fields kept for back-compat — empty for new responses.
    variance_drivers: list[dict[str, Any]] = field(default_factory=list)
    risk_flags: list[dict[str, str]] = field(default_factory=list)
    error: SafeAiError | None = None


# ── Validation helpers ──────────────────────────────────────────────────────
_ALLOWED_SEVERITY = {"low", "medium", "high"}
# Closed risk-flag vocabulary (caption-contract v1). The first block is the
# canonical set required by the contract; the trailing entries are retained so
# previously-stored / legacy SLM outputs keep validating.
_ALLOWED_RISK_TYPES = frozenset({
    "materiality",
    "large_transaction",
    "mapping_review",
    "cutoff_risk",
    "sign_inconsistency",
    "missing_evidence",
    "attribution_limitation",
    "decomposition_gap",
    "manual_journal_risk",
    "recurring_pattern",
    # Closed anomaly vocabulary from the caption-contract spec (Section 2.6 /
    # 5.5): trend break, concentration, and timing distortion.
    "trend_break",
    "concentration",
    "timing_distortion",
    # Turnover / margin trend breaks for the caption-specific evidence builders.
    "dso_trend_break",
    "dpo_trend_break",
    "aging_deterioration",
    "margin_trend_break",
    # Legacy aliases (kept for back-compat with already-stored commentary).
    "missing_customer_vendor_link",
    "insufficient_narration",
})
# Canonical caption-contract detailed sections (v2 layout).
_DETAIL_SECTION_KEYS = (
    "headline",
    "variance_bridge",
    "contract_lens",
    "primary_drivers",
    "decomposition",
    "key_transaction_evidence",
    "drill_down_attribution",
    "pattern_or_anomaly",
    "required_evidence_status",
    "reviewer_actions",
    "escalation_triggers",
)
# Sections stored/rendered as a single narrative string (vs a bullet list).
_NARRATIVE_SECTION_KEYS = frozenset({
    "headline",
    "variance_bridge",
    "contract_lens",
    "pattern_or_anomaly",
})
# Legacy SLM/section keys mapped to the canonical v2 sections during parse so
# older payloads (and already-stored detailed_text) still slot in correctly.
_LEGACY_DETAIL_KEY_MAP: dict[str, str] = {
    "summary": "headline",
    "executive_summary": "headline",
    "major_movement": "primary_drivers",
    "largest_transactions": "key_transaction_evidence",
    "transaction_pattern": "pattern_or_anomaly",
    "timing_and_posting_signals": "pattern_or_anomaly",
    "customer_vendor_context": "drill_down_attribution",
    "attribution_coverage": "drill_down_attribution",
    "recommended_actions": "reviewer_actions",
}


def _coerce_str(value: Any, *, max_len: int = 2000) -> str | None:
    """Coerce ``value`` to a stripped, mojibake-cleaned string. Returns
    ``None`` for non-strings, empty inputs, or strings that became empty
    after cleanup. The cap is applied AFTER cleanup so we don't drop a
    safe character we were about to delete anyway."""
    if not isinstance(value, str):
        return None
    cleaned = clean_ai_text(value)
    if not cleaned:
        return None
    return cleaned[:max_len]


def _coerce_severity(value: Any) -> str:
    if isinstance(value, str) and value.lower() in _ALLOWED_SEVERITY:
        return value.lower()
    return "low"


def _coerce_optional_amount(value: Any) -> float | None:
    """Permissive number parsing for driver amounts. Accepts numeric values
    and decimal-string variants ($, comma, leading sign). Returns ``None``
    when the value is missing or non-numeric, never raises."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _coerce_drivers(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        driver = _coerce_str(item.get("driver"), max_len=200)
        if not driver:
            continue
        out.append({
            "driver":   driver,
            "evidence": _coerce_str(item.get("evidence"), max_len=500) or "",
            "amount":   _coerce_optional_amount(item.get("amount")),
            "impact":   _coerce_severity(item.get("impact")),
        })
    return out


def _normalize_risk_flag(item: dict[str, Any]) -> dict[str, str]:
    """Map internal type/message flags to FluxAiRiskFlag response schema."""
    rtype = _coerce_str(item.get("risk") or item.get("type"), max_len=80)
    if not rtype or rtype not in _ALLOWED_RISK_TYPES:
        rtype = "mapping_review"
    reason = _coerce_str(item.get("reason") or item.get("message"), max_len=500) or ""
    return {
        "risk": rtype,
        "severity": _coerce_severity(item.get("severity")),
        "reason": reason,
    }


def _coerce_risk_flags(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rtype = _coerce_str(item.get("type") or item.get("risk"), max_len=80)
        if rtype and rtype not in _ALLOWED_RISK_TYPES:
            rtype = "mapping_review"
        message = _coerce_str(item.get("message") or item.get("reason"), max_len=500)
        if not message:
            continue
        out.append(_normalize_risk_flag({
            "type": rtype or "mapping_review",
            "severity": item.get("severity"),
            "message": message,
        }))
    return out


def _coerce_actions(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return clean_ai_text_iter([item for item in raw if isinstance(item, str)])


def _coerce_review_notes(raw: Any) -> list[str]:
    """Same shape as ``_coerce_actions``; kept separate for clarity at
    call sites and so future validation can diverge without churn."""
    if not isinstance(raw, list):
        return []
    return clean_ai_text_iter([item for item in raw if isinstance(item, str)])


def _coerce_string_array(raw: Any) -> list[str]:
    """Generic string-array coercer used by the new schema's plain-text
    arrays (``drivers``, ``mapped_accounts``, ``transaction_notes``).
    Mojibake is repaired and empty entries are dropped."""
    if not isinstance(raw, list):
        return []
    return clean_ai_text_iter([item for item in raw if isinstance(item, str)])


def _coerce_drivers_strings(raw: Any) -> list[str]:
    """Permissive parser: accept either the new ``drivers`` array of plain
    strings or the legacy ``variance_drivers`` array of dicts. Returns a
    flat list of strings so downstream rendering stays simple. Never
    raises on bad input.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            cleaned = clean_ai_text(item, drop_emoji=True)
            if cleaned:
                out.append(cleaned)
            continue
        if isinstance(item, dict):
            driver = clean_ai_text(item.get("driver"), drop_emoji=True)
            if not driver:
                continue
            evidence = clean_ai_text(item.get("evidence"), drop_emoji=True)
            amount = item.get("amount")
            if isinstance(amount, (int, float)) and amount:
                driver = f"{driver} (approximately ${abs(float(amount)):,.0f})"
            if evidence:
                driver = f"{driver} - {evidence}"
            out.append(driver)
    return out


def _coerce_confidence(raw: Any) -> float:
    """Clamp the SLM-supplied confidence into [0.0, 1.0]. Returns 0 when the
    value is non-numeric so a misbehaving model never produces an `ai_score`
    above 100."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _parse_detailed_sections(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        target = _LEGACY_DETAIL_KEY_MAP.get(key, key)
        if target not in _DETAIL_SECTION_KEYS:
            continue
        if target in _NARRATIVE_SECTION_KEYS:
            # Narrative sections are single strings. If a legacy payload sent a
            # list (e.g. timing_and_posting_signals), fold it into one string.
            if isinstance(value, list):
                joined = " ".join(_coerce_string_array(value))
                text = _coerce_str(joined, max_len=2000) or ""
            else:
                text = _coerce_str(value, max_len=2000) or ""
            if text:
                out[target] = text
        else:
            existing = out.get(target, [])
            if not isinstance(existing, list):
                existing = []
            out[target] = existing + _coerce_string_array(value)
    return out


def _validate_payload(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate SLM JSON — concise_text required; detailed object optional."""
    if not isinstance(payload, dict):
        return None, SLM_SCHEMA_INVALID

    concise = _coerce_str(payload.get("concise_text"), max_len=1500)
    if not concise:
        return None, SLM_SCHEMA_INVALID

    detailed_sections: dict[str, Any] | None = None
    if isinstance(payload.get("detailed"), dict):
        detailed_sections = _parse_detailed_sections(payload.get("detailed"))

    return {
        "concise_text":        concise,
        "detailed_sections":   detailed_sections,
        "risk_flags":          _coerce_risk_flags(payload.get("risk_flags")),
        "confidence_score":    _coerce_confidence(payload.get("confidence_score")),
    }, None


# ── Loaders ─────────────────────────────────────────────────────────────────
def _load_flux_row(
    db: Session, entity: Entity, statement: str, caption: str,
) -> FluxData | None:
    """Find the active-period flux row for the (entity, statement, caption)
    triple. Active period is taken from the entity itself so we always use
    the supervisor-configured window, not whichever row happens to exist."""
    return (
        db.query(FluxData)
        .filter(
            FluxData.entity_id   == entity.id,
            FluxData.statement   == statement,
            FluxData.caption     == caption,
            FluxData.period_from == entity.period_from,
            FluxData.period_to   == entity.period_to,
        )
        .first()
    )


def _load_threshold_rule(
    db: Session, entity_id: str, statement: str,
) -> str | None:
    """Render the per-statement threshold row as a single-line summary so
    the prompt can reference it without exposing the raw model."""
    row = (
        db.query(Threshold)
        .filter(
            Threshold.entity_id == entity_id,
            Threshold.statement == statement,
        )
        .first()
    )
    if not row:
        return None
    return (
        f"mode={row.mode}, pct>={row.pct_value}%, abs>={row.abs_value}, "
        f"logic={row.logic}"
    )


def _load_ontology_mapping(
    db: Session, entity_id: str, statement: str, caption: str,
) -> OntologyMapping | None:
    return (
        db.query(OntologyMapping)
        .filter(
            OntologyMapping.entity_id     == entity_id,
            OntologyMapping.statement     == statement,
            OntologyMapping.tflux_caption == caption,
        )
        .first()
    )


def _label_gl_accounts(
    db: Session, entity_id: str, account_nos: list[str],
) -> list[str]:
    """Return human-readable labels for mapped GL account numbers. Falls
    back to bare account numbers when the ERP catalog hasn't been synced
    yet so the prompt still has *something* to anchor on."""
    if not account_nos:
        return []
    rows = (
        db.query(ErpGlAccount)
        .filter(
            ErpGlAccount.entity_id == entity_id,
            ErpGlAccount.account_no.in_(account_nos),
        )
        .all()
    )
    by_no = {row.account_no: row for row in rows}
    labels: list[str] = []
    for no in account_nos:
        row = by_no.get(no)
        if row and (row.account_name or row.display_name):
            labels.append(f"{no} — {row.display_name or row.account_name}")
        else:
            labels.append(no)
    return labels


def _label_dimensions(
    db: Session, entity_id: str, dimension_codes: list[str],
) -> list[str]:
    """Return labels for mapped Sub-GL / dimension codes. Same fallback
    strategy as `_label_gl_accounts`."""
    if not dimension_codes:
        return []
    rows = (
        db.query(ErpDimensionValue)
        .filter(
            ErpDimensionValue.entity_id      == entity_id,
            ErpDimensionValue.dimension_code.in_(dimension_codes),
        )
        .all()
    )
    by_code = {row.dimension_code: row for row in rows}
    labels: list[str] = []
    for code in dimension_codes:
        row = by_code.get(code)
        if row and (row.dimension_name or row.display_name):
            labels.append(f"{code} — {row.dimension_name or row.display_name}")
        else:
            labels.append(code)
    return labels


# Keys to probe for customer/vendor/party context in BC's
# `raw_source_json`. Listed by descending specificity so the first match
# wins. Never raises if the payload is missing.
_PARTY_NAME_KEYS: tuple[str, ...] = (
    "customerName", "customer_name", "vendorName", "vendor_name",
    "sourceName", "source_name", "balancingAccountName",
    "balancing_account_name", "partyName", "party_name",
)
_PARTY_NUMBER_KEYS: tuple[str, ...] = (
    "customerNumber", "customer_number", "customerNo", "customer_no",
    "vendorNumber", "vendor_number", "vendorNo", "vendor_no",
    "sourceNo", "source_no", "partyNumber", "party_number",
)


def _extract_party_label(entry: ErpGeneralLedgerEntry) -> str | None:
    """Return ``"<name> (<number>)"`` when BC supplied a customer/vendor
    on the entry, else just one of them, else ``None``. Lives next to
    the prompt builder because it's a presentation concern, not GL math.
    """
    raw = entry.raw_source_json
    if not isinstance(raw, dict):
        return None
    name: str | None = None
    number: str | None = None
    for key in _PARTY_NAME_KEYS:
        val = raw.get(key)
        if val:
            name = str(val).strip()
            break
    for key in _PARTY_NUMBER_KEYS:
        val = raw.get(key)
        if val:
            number = str(val).strip()
            break
    if name and number:
        return f"{name} ({number})"
    return name or number


def _summarize_transactions(
    db: Session, entity_id: str, account_nos: list[str], limit: int = 10,
) -> str | None:
    """Build a short textual summary of recent GL entries for the mapped
    accounts. Kept brief so the prompt stays well within token limits.
    Returns ``None`` when nothing relevant is on file.

    Each line includes — when available — the customer/vendor party so the
    AI can mention real BC-backed names instead of inventing them.
    """
    if not account_nos:
        return None
    rows = (
        db.query(ErpGeneralLedgerEntry)
        .filter(
            ErpGeneralLedgerEntry.entity_id == entity_id,
            ErpGeneralLedgerEntry.account_no.in_(account_nos),
        )
        .order_by(ErpGeneralLedgerEntry.posting_date.desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return None
    bullet_lines: list[str] = []
    party_seen = False
    for row in rows:
        date_part = row.posting_date.isoformat() if row.posting_date else "?"
        amount_part = (
            f"{float(row.amount):,.2f}" if row.amount is not None else "?"
        )
        desc = (row.description or "").strip() or "(no description)"
        doc_part = (row.document_no or "").strip() or "-"
        party = _extract_party_label(row)
        if party:
            party_seen = True
            line = (
                f"- {date_part} | doc={doc_part} | acct={row.account_no} "
                f"| amount={amount_part} | party={party} | {desc}"
            )
        else:
            line = (
                f"- {date_part} | doc={doc_part} | acct={row.account_no} "
                f"| amount={amount_part} | {desc}"
            )
        bullet_lines.append(line)
    if not party_seen:
        from app.services.flux_ai.party_context import build_party_ai_context
        party = build_party_ai_context(db, entity_id, None, account_nos)
        if party.customer_vendor_master_available:
            bullet_lines.append(
                "(GL entries do not include customer/vendor references; "
                "use ontology-selected master context when mapped.)"
            )
        else:
            bullet_lines.append(
                "(Customer/vendor names are not present in the synced BC "
                "payload; review document numbers and descriptions instead.)"
            )
    return "\n".join(bullet_lines)


def _existing_comment_snippets(
    db: Session, entity_id: str, caption: str, limit: int = 3,
) -> list[str]:
    """Return up to ``limit`` open-comment text blobs for the caption so
    the model is aware of unresolved reviewer questions."""
    rows = (
        db.query(Comment)
        .filter(
            Comment.entity_id == entity_id,
            Comment.caption == caption,
            Comment.is_resolved == False,  # noqa: E712 (SQLAlchemy expr)
        )
        .order_by(Comment.created_at.desc())
        .limit(limit)
        .all()
    )
    return [c.text.strip() for c in rows if c.text]


# ── Persistence ─────────────────────────────────────────────────────────────
def _upsert_ai_commentary(
    db: Session,
    *,
    entity_id: str,
    caption: str,
    concise_text: str,
    detailed_text: str,
    confidence_score: float,
) -> AiCommentary:
    """Idempotent write into ``ai_commentary``. Critically, ``edited_text`` /
    ``edited_by`` / ``edited_at`` are NEVER touched here — supervisors keep
    their manual edits even after a regen."""
    row = (
        db.query(AiCommentary)
        .filter(
            AiCommentary.entity_id == entity_id,
            AiCommentary.caption == caption,
        )
        .first()
    )
    score = max(0, min(100, int(round(confidence_score * 100))))
    if row is None:
        row = AiCommentary(
            entity_id=entity_id,
            caption=caption,
            concise_text=concise_text,
            detailed_text=detailed_text,
            ai_score=score,
        )
        db.add(row)
    else:
        row.concise_text = concise_text
        row.detailed_text = detailed_text
        row.ai_score = score
        row.updated_at = datetime.utcnow()
    db.flush()
    return row


# ── Public entry point ──────────────────────────────────────────────────────
async def generate_flux_ai_commentary(
    db: Session,
    *,
    current_user: User,
    entity_id: str,
    statement: str,
    caption: str,
    request=None,
    cfg: SlmConfig | None = None,
) -> FluxAiResult:
    """Generate one (entity, statement, caption) commentary synchronously.

    The route handler is responsible for:
      • Resolving `current_user` from the bearer token.
      • Calling `db.commit()` after a successful run.

    This service:
      • Validates entity access (analysts only see assigned entities).
      • Loads the flux row and ontology context.
      • Calls the SLM gateway exactly once.
      • Parses + validates the JSON.
      • Upserts the `ai_commentary` row (preserving any reviewer edits).
      • Writes a single audit row on success.
    """
    # 1. Entity + access
    entity = db.query(Entity).filter(Entity.id == entity_id).first()
    if entity is None:
        return FluxAiResult(
            ok=False, entity_id=entity_id, statement=statement, caption=caption,
            error=make_safe_ai_error(ENTITY_NOT_FOUND),
        )
    if not can_access_entity(db, current_user, entity_id):
        return FluxAiResult(
            ok=False, entity_id=entity_id, statement=statement, caption=caption,
            error=make_safe_ai_error(ENTITY_ACCESS_DENIED),
        )

    # 2. Flux row for the active period
    flux_row = _load_flux_row(db, entity, statement, caption)
    if flux_row is None:
        return FluxAiResult(
            ok=False, entity_id=entity_id, statement=statement, caption=caption,
            error=make_safe_ai_error(FLUX_DATA_NOT_FOUND),
        )

    # 3. Prepared evidence (backend-computed; AI must not recalculate).
    from app.services.flux_ai.flux_ai_evidence import build_flux_ai_evidence
    from app.services.flux_ai.party_context import (
        build_attribution_coverage_lines,
        build_customer_vendor_context_lines_from_evidence,
        build_party_ai_context,
    )

    threshold_rule = _load_threshold_rule(db, entity_id, statement)
    mapping = _load_ontology_mapping(db, entity_id, statement, caption)

    gl_account_nos: list[str] = []
    if mapping is not None:
        gl_account_nos = [str(x) for x in (mapping.erp_gl_json or []) if x]

    evidence = build_flux_ai_evidence(db, entity, statement, mapping)
    party_ctx = build_party_ai_context(db, entity_id, mapping, gl_account_nos)
    attr_lines = build_attribution_coverage_lines(party_ctx, evidence)
    cv_lines = build_customer_vendor_context_lines_from_evidence(
        evidence, party_ctx,
    )
    _log.info(
        "Flux AI evidence entity=%s caption=%s party=%s",
        entity_id, caption, party_ctx.diagnostics,
    )

    # 4. Compute finance-normalized variance for AI direction/percentages.
    current_value = float(flux_row.current_value or 0)
    comp_value    = float(flux_row.comp_value    or 0)
    fin = compute_finance_variance(current_value, comp_value, caption, statement)
    cur_sign, comp_sign = build_value_sign_labels(
        current_value, comp_value, fin.normal_balance,
    )

    # Resolve the caption-wise analysis contract (None for captions without a
    # dedicated contract; the prompt falls back to generic guidance).
    caption_contract = resolve_caption_contract(caption, statement)
    contract_block = contract_prompt_block(caption_contract)

    ctx = FluxPromptContext(
        entity_id=entity.id,
        entity_name=entity.name,
        statement=statement,
        caption=caption,
        period_from=entity.period_from,
        period_to=entity.period_to,
        current_value=current_value,
        comp_value=comp_value,
        raw_variance_amount=fin.raw_variance_amount,
        variance_amount=fin.finance_variance_amount,
        variance_pct=fin.finance_variance_percentage,
        normal_balance=fin.normal_balance,
        finance_current_value=fin.finance_current_value,
        finance_comparison_value=fin.finance_comparison_value,
        finance_direction=fin.finance_direction,
        current_value_sign_label=cur_sign,
        comparison_value_sign_label=comp_sign,
        normal_balance_explanation=normal_balance_explanation(
            current_value, comp_value, fin.normal_balance,
        ),
        is_material=bool(flux_row.is_material),
        threshold_rule=threshold_rule,
        evidence=evidence,
        customer_vendor_context_lines=cv_lines,
        attribution_coverage_lines=attr_lines,
        existing_comments=_existing_comment_snippets(db, entity_id, caption),
        contract=contract_block,
    )

    # 5. SLM call
    config = cfg or load_slm_config()
    messages = [
        SlmChatMessage(role="system", content=build_flux_ai_system_prompt()),
        SlmChatMessage(role="user",   content=build_flux_ai_user_prompt(ctx)),
    ]
    chat_result: SlmChatResult = await slm_chat_request(messages, config)
    _log.info(
        "SLM raw response [entity=%s caption=%s ok=%s]:\n%s",
        entity_id, caption, chat_result.ok, chat_result.text or "(empty)",
    )
    if not chat_result.ok or not chat_result.text:
        return FluxAiResult(
            ok=False, entity_id=entity_id, statement=statement, caption=caption,
            error=chat_result.error or make_safe_ai_error("SLM_HTTP_ERROR"),
        )

    # 6. Parse + validate
    raw = parse_loose_json_from_llm_text(chat_result.text)
    if raw is None:
        final = synthesize_fallback_commentary(ctx)
        schema_err = SLM_INVALID_JSON
    else:
        clean, schema_err = _validate_payload(raw)
        if clean is None:
            final = synthesize_fallback_commentary(ctx)
        else:
            final = _merge_ai_with_evidence(clean, ctx)

    if not final.get("concise_text") or not final.get("detailed_text"):
        return FluxAiResult(
            ok=False, entity_id=entity_id, statement=statement, caption=caption,
            error=make_safe_ai_error(schema_err or SLM_SCHEMA_INVALID),
        )

    # 7. Persist (preserving any reviewer-edited text on the existing row)
    row = _upsert_ai_commentary(
        db,
        entity_id=entity_id,
        caption=caption,
        concise_text=final["concise_text"],
        detailed_text=final["detailed_text"],
        confidence_score=final["confidence_score"],
    )

    # 8. Audit (best-effort; never blocks the response on audit failure).
    try:
        log_change(
            db,
            actor=current_user,
            action="ai_commentary.generate",
            table_name="ai_commentary",
            row_pk=f"{entity_id}/{caption}",
            after={
                "statement":            statement,
                "ai_score":             row.ai_score,
                "confidence_score":     final["confidence_score"],
                "prompt_version":       PROMPT_VERSION,
                "contract_version":     CAPTION_CONTRACT_VERSION,
                "caption_contract_id":  (
                    caption_contract.caption_id if caption_contract else None
                ),
                "model":                getattr(config, "model", None),
                "generated_at":         datetime.utcnow().isoformat(),
            },
            request=request,
        )
    except Exception:  # pragma: no cover - audit must never break the flow
        _log.exception("Audit log failed for ai_commentary.generate")

    return FluxAiResult(
        ok=True,
        entity_id=entity_id,
        statement=statement,
        caption=caption,
        concise_text=final["concise_text"],
        detailed_text=final["detailed_text"],
        customer_vendor_notes=final.get("attribution_coverage", []),
        recommended_actions=final.get("reviewer_actions", []),
        confidence_score=final["confidence_score"],
        party_diagnostics=party_ctx.diagnostics,
        risk_flags=final.get("risk_flags", []),
        error=None,
    )


# ── Evidence-backed commentary assembly ───────────────────────────────────

_GENERIC_CONCISE_PHRASES = (
    "within model expectations",
    "in line with forecast",
    "as expected",
    "no significant",
    "immaterial movement",
)

_MSG_NO_GL_MOVEMENT = (
    "Primary GL driver detail is not available from the current generated "
    "Flux context."
)
_MSG_NO_TXN_DETAIL = (
    "Key transaction detail is not available in the current synced ERP payload."
)
_MSG_TIMING_UNKNOWN = (
    "Timing risk could not be assessed from the current payload."
)
_MSG_GENERIC_NARRATION = (
    "Narration is generic; supporting evidence should be requested."
)


def _comparison_period_label(ctx: FluxPromptContext) -> str:
    if ctx.evidence and ctx.evidence.comparison_period_label:
        return ctx.evidence.comparison_period_label
    return clean_ai_text(ctx.period_from) or "the comparison period"


def _is_generic_concise(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in _GENERIC_CONCISE_PHRASES)


def _main_driver_phrase(ctx: FluxPromptContext) -> str:
    evidence = ctx.evidence
    if not evidence:
        return "mapped GL transactions"
    if evidence.main_movement_line:
        line = evidence.main_movement_line
        if line.startswith("GL "):
            head = line.split(" contributed")[0].split(" drove")[0]
            return f"large movements under {head}"
        return line.rstrip(".")
    if evidence.top_gl_movements:
        parts = []
        for m in evidence.top_gl_movements[:3]:
            name = m.account_name or m.account_no
            parts.append(f"GL {m.account_no} — {name}")
        return f"activity under {', '.join(parts)}"
    if evidence.top_transactions:
        t = evidence.top_transactions[0]
        return (
            f"{t.account_name or t.account_no} transactions in the "
            f"{t.period_bucket} period"
        )
    return "mapped GL account activity"


def _turnover_concise(ctx: FluxPromptContext) -> str | None:
    """Caption-contract concise line for turnover captions (AR/AP/Inventory).
    Follows the spec format including DSO/DPO/DIO, with an explicit fallback
    when the turnover ratio is unavailable."""
    metrics = _caption_metrics(ctx)
    if metrics is None:
        return None
    caption = clean_ai_text(ctx.caption) or "Caption"
    direction = resolved_finance_direction(ctx)
    amount_str = format_currency(ctx.variance_amount)
    pct_str = format_percent(ctx.variance_pct)
    top_party = ""
    if metrics.top_parties:
        p = metrics.top_parties[0]
        top_party = (p.party_name or p.party_no).strip()
    if metrics.turnover_current is not None and metrics.turnover_comparison is not None:
        tail = (
            f"; {metrics.turnover_label} {_fmt_days(metrics.turnover_comparison)} "
            f"to {_fmt_days(metrics.turnover_current)}"
        )
        if top_party:
            tail += f", concentrated in {top_party}"
        return f"{caption} {direction} by {amount_str} ({pct_str}){tail}."
    concentrate = top_party or "the mapped GL accounts"
    return (
        f"{caption} {direction} by {amount_str} ({pct_str}); "
        f"{metrics.turnover_label} and aging evidence are unavailable, with "
        f"movement concentrated in {concentrate}."
    )


def _build_concise_from_evidence(ctx: FluxPromptContext) -> str:
    turnover = _turnover_concise(ctx)
    if turnover:
        return turnover
    caption = clean_ai_text(ctx.caption) or "Caption"
    direction = resolved_finance_direction(ctx)
    amount_str = format_currency(ctx.variance_amount)
    pct_str = format_percent(ctx.variance_pct)
    comp = _comparison_period_label(ctx)
    movement = build_movement_summary_clause(
        ctx.current_value,
        ctx.comp_value,
        ctx.normal_balance,  # type: ignore[arg-type]
        include_period_labels=False,
    )
    material = (
        "; this is material and requires review."
        if ctx.is_material
        else "."
    )
    if ctx.normal_balance in ("credit", "debit") and (
        ctx.current_value < 0 or ctx.comp_value < 0
    ):
        return (
            f"{caption} {direction} by {amount_str} ({pct_str}) versus {comp}, "
            f"{movement}{material}"
        )
    driver = _main_driver_phrase(ctx)
    return (
        f"{caption} {direction} by {amount_str} ({pct_str}) versus {comp}, "
        f"mainly driven by {driver}{material}"
    )


def _build_variance_bridge(ctx: FluxPromptContext) -> str:
    direction = resolved_finance_bridge_word(ctx)
    evidence = ctx.evidence
    signed = variance_bridge_signed_narrative(
        ctx.current_value, ctx.comp_value, ctx.normal_balance,  # type: ignore[arg-type]
    )
    if signed:
        return f"Variance bridge: {signed}"
    if ctx.normal_balance == "credit":
        cur_label = evidence.current_period_label if evidence else ctx.period_from
        comp_label = evidence.comparison_period_label if evidence else ctx.period_to
        return (
            f"Variance bridge: Revenue/income magnitude was "
            f"{format_currency(ctx.finance_current_value)} in {cur_label} compared "
            f"with {format_currency(ctx.finance_comparison_value)} in {comp_label}, "
            f"resulting in a {format_currency(abs(ctx.variance_amount))} {direction}."
        )
    if evidence and evidence.calculation_mode == "balance_at_date":
        return (
            f"Variance bridge: Balance at {evidence.current_period_label} was "
            f"{format_currency(ctx.current_value)} compared with "
            f"{format_currency(ctx.comp_value)} at "
            f"{evidence.comparison_period_label}, resulting in a "
            f"{format_currency(abs(ctx.variance_amount))} {direction}."
        )
    if ctx.normal_balance == "debit" and ctx.comp_value < 0:
        return (
            "Variance bridge: The variance is mainly driven by inventory adjustment "
            "and mapped cost GL movements. The comparison period included significant "
            "negative cost adjustments, while the current period shows positive "
            "cost activity."
        )
    return (
        f"Variance bridge: Current period {format_currency(ctx.current_value)} less "
        f"comparison period {format_currency(ctx.comp_value)} resulted in a "
        f"{format_currency(abs(ctx.variance_amount))} {direction}."
    )


def _build_executive_summary(ctx: FluxPromptContext) -> str:
    caption = clean_ai_text(ctx.caption) or "Caption"
    direction = resolved_finance_direction(ctx)
    evidence = ctx.evidence
    calc_note = (
        "calculated using Balance at Date as of the selected period end date"
        if evidence and evidence.calculation_mode == "balance_at_date"
        else "calculated using Net Change for the selected period"
    )
    cur_label = evidence.current_period_label if evidence else ctx.period_from
    comp_label = evidence.comparison_period_label if evidence else ctx.period_to
    pct_part = f" ({format_percent(ctx.variance_pct)})" if ctx.variance_pct is not None else ""
    comp = _comparison_period_label(ctx)

    if ctx.normal_balance in ("credit", "debit") and (
        ctx.current_value < 0 or ctx.comp_value < 0
    ):
        movement = build_movement_summary_clause(
            ctx.current_value,
            ctx.comp_value,
            ctx.normal_balance,  # type: ignore[arg-type]
            comparison_period_label=comp_label,
            current_period_label=cur_label,
            include_period_labels=True,
        )
        base = (
            f"{caption} {direction} by {format_currency(ctx.variance_amount)}"
            f"{pct_part} versus {comp}, {movement}. "
            f"This movement is {calc_note}."
        )
    else:
        if ctx.normal_balance == "credit":
            cur_display = ctx.finance_current_value
            comp_display = ctx.finance_comparison_value
        else:
            cur_display = ctx.current_value
            comp_display = ctx.comp_value
        base = (
            f"{caption} {direction} by {format_currency(ctx.variance_amount)}{pct_part}, "
            f"moving from {format_currency(comp_display)} in {comp_label} "
            f"compared with {format_currency(cur_display)} in {cur_label}, "
            f"{calc_note}."
        )
    if ctx.comp_value == 0:
        base += (
            " Percentage variance is not meaningful because the comparison "
            "value is zero."
        )
    metrics = _caption_metrics(ctx)
    if metrics is not None and metrics.turnover_current is not None:
        base += (
            f" {metrics.turnover_label} moved from "
            f"{_fmt_days(metrics.turnover_comparison)} to "
            f"{_fmt_days(metrics.turnover_current)} "
            f"(change {_fmt_days(metrics.turnover_change)})."
        )
    return base


def _format_txn_evidence_lines(evidence: FluxAiEvidence | None) -> list[str]:
    if not evidence or not evidence.top_transactions:
        return [_MSG_NO_TXN_DETAIL]
    lines: list[str] = []
    for t in evidence.top_transactions:
        line = t.formatted_line
        if t.narration_is_generic:
            line = f"{line} — {_MSG_GENERIC_NARRATION}"
        lines.append(line)
    return lines


def _build_key_transaction_evidence(ctx: FluxPromptContext) -> list[str]:
    """Key entries: prefer open sub-ledger invoices (with invoice date,
    party, amount, age days) for AR/AP; otherwise the de-duplicated GL
    transaction lines. Limited to the top material entries."""
    metrics = _caption_metrics(ctx)
    if metrics is not None and metrics.top_invoices:
        lines: list[str] = []
        for inv in metrics.top_invoices:
            name = (inv.party_name or inv.party_no).strip()
            label = (
                f"{metrics.party_label.capitalize()} {inv.party_no} — {name}"
                if name and name != inv.party_no
                else f"{metrics.party_label.capitalize()} {inv.party_no}"
            )
            age = f"aged {inv.age_days} days" if inv.age_days is not None else "age n/a"
            doc = f"Doc {inv.document_no}" if inv.document_no and inv.document_no != "-" else "Doc -"
            narr = f" | {inv.description}" if inv.description else ""
            lines.append(
                f"{inv.posting_date} | {doc} | {label} | "
                f"{format_currency(inv.amount)} open | {age}{narr}",
            )
        lines.append(
            "Poster/source user is not exposed in the current synced ERP payload.",
        )
        return lines
    return _format_txn_evidence_lines(ctx.evidence)


def _build_evidence_backed_sections(ctx: FluxPromptContext) -> dict[str, Any]:
    evidence = ctx.evidence

    executive = _build_executive_summary(ctx)
    bridge = _build_variance_bridge(ctx)

    primary: list[str] = []
    if evidence and evidence.top_gl_movements:
        from app.services.flux_ai.flux_ai_evidence import _gl_movement_sentence
        primary = [
            _gl_movement_sentence(m, normal_balance=ctx.normal_balance)
            for m in evidence.top_gl_movements
        ]
    elif evidence and evidence.mapped_gl_accounts:
        nos = ", ".join(a.account_no for a in evidence.mapped_gl_accounts[:5])
        primary = [f"Mapped sales GL accounts {nos} had activity in the selected periods."]
    else:
        primary = [_MSG_NO_GL_MOVEMENT]

    txn_lines = _build_key_transaction_evidence(ctx)

    timing: list[str] = []
    if evidence and evidence.posting_signal.timing_lines:
        timing = list(evidence.posting_signal.timing_lines)
    elif evidence and evidence.transaction_pattern.pattern_lines:
        timing = list(evidence.transaction_pattern.pattern_lines)
    else:
        timing = [_MSG_TIMING_UNKNOWN]

    attribution = _build_drill_down_attribution(ctx)

    actions = [
        "Review the largest GL entries and confirm whether the mapped "
        "accounts are appropriate.",
    ]
    if evidence:
        if evidence.posting_signal.majority_period == "comparison":
            actions.insert(
                0,
                "Validate whether the comparison-period transactions are "
                "one-time adjustments or recurring entries.",
            )
        if ctx.normal_balance == "debit" and ctx.comp_value < 0:
            actions.insert(
                0,
                "Review the largest inventory adjustment and retail purchase "
                "entries, and confirm whether prior-period negative cost "
                "adjustments were one-time reversals or normal recurring entries.",
            )
        if evidence.posting_signal.generic_narration_count >= 2:
            actions.append(
                "Request support for material entries with generic narration.",
            )
        if evidence.posting_signal.period_end_concentration:
            actions.append("Confirm period cut-off before sign-off.")
    if ctx.is_material and len(actions) < 2:
        actions.append("Confirm account mapping before sign-off.")

    return {
        "headline": executive,
        "variance_bridge": bridge,
        "contract_lens": _build_contract_lens(ctx),
        "primary_drivers": primary,
        "decomposition": _build_decomposition(ctx),
        "key_transaction_evidence": txn_lines,
        "drill_down_attribution": attribution,
        "pattern_or_anomaly": _build_pattern_or_anomaly(ctx, timing),
        "required_evidence_status": _build_required_evidence_status(ctx),
        "reviewer_actions": actions[:2],
        "escalation_triggers": _build_escalation_triggers(ctx),
    }


# ── Caption-contract-aware section builders ───────────────────────────────

# Caption families whose contracts require a balance roll-forward (opening +
# movement = closing). The component split (additions/disposals/etc.) is not
# separable from current synced GL data, so we present the closing-balance
# identity and localise to the mapped accounts and entries below.
_ROLLFORWARD_CAPTION_IDS = frozenset({"ppe", "debt", "equity", "intangibles"})
# Caption families whose contracts express the movement as a turnover ratio
# (DSO/DIO/DPO) plus an aging profile. Those metrics need the paired flow
# (revenue/COGS) and an aging report that the current GL-only payload does not
# expose, so we state the limitation per the spec's graceful-degradation rule.
_TURNOVER_CAPTION_LABELS: dict[str, str] = {
    "accounts_receivable": "DSO (days sales outstanding)",
    "inventory": "DIO (days inventory outstanding)",
    "accounts_payable": "DPO (days payable outstanding)",
}


def _contract_of(ctx: FluxPromptContext) -> dict[str, Any] | None:
    c = getattr(ctx, "contract", None)
    return c if isinstance(c, dict) else None


def _caption_metrics(ctx: FluxPromptContext):
    """Return the computed CaptionMetricEvidence for this caption, or None."""
    evidence = ctx.evidence
    return getattr(evidence, "caption_metrics", None) if evidence else None


def _fmt_days(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} days"


# DSO/DPO change (in days) beyond which the contract flags a turnover trend
# break; aligned with the AR contract's "+/-5 days" anomaly trigger.
_TURNOVER_TREND_BREAK_DAYS = 5.0
# Single-party share of the open sub-ledger balance above which we flag a
# concentration risk.
_CONCENTRATION_PCT_THRESHOLD = 40.0


def _build_required_evidence_status(ctx: FluxPromptContext) -> list[str]:
    """Map the contract's required_evidence to Available / Missing using the
    computed metrics, then append the explicit evidence gaps. Backend-
    authoritative so the reviewer always sees what is and is not supported."""
    from app.services.flux_ai.caption_metrics import metric_evidence_status_lines

    contract = _contract_of(ctx)
    required = list(contract.get("required_evidence") or []) if contract else []
    metrics = _caption_metrics(ctx)
    lines = metric_evidence_status_lines(metrics, required)
    if metrics is not None:
        for gap in metrics.evidence_gaps:
            gap_line = f"Evidence gap: {gap}"
            if gap_line not in lines:
                lines.append(gap_line)
    if not lines:
        lines.append(
            "Required-evidence status is not tracked for this caption; review "
            "is based on mapped GL accounts and document-level entries.",
        )
    return lines[:10]


def _build_contract_lens(ctx: FluxPromptContext) -> str:
    contract = _contract_of(ctx)
    if not contract:
        return (
            "No dedicated caption contract is defined for this caption; the "
            "movement is reviewed using mapped GL account evidence and "
            "available document-level entries."
        )
    name = clean_ai_text(contract.get("display_name") or ctx.caption) or ctx.caption
    movement = clean_ai_text(contract.get("normal_movement") or "")
    drivers = [
        clean_ai_text(d) for d in (contract.get("permitted_drivers") or []) if d
    ]
    parts = [f"Analyzed using the {name} caption contract."]
    if movement:
        parts.append(movement)
    if drivers:
        parts.append(
            "Permitted drivers for this caption are "
            + ", ".join(drivers[:8]) + "."
        )
    if contract.get("is_subtotal"):
        parts.append(
            "This is a subtotal caption, so it is explained through its "
            "component captions rather than direct transaction drill-down."
        )
    elif contract.get("balance_at_date"):
        parts.append(
            "This is a Balance at Date caption, so the closing balance and its "
            "roll-forward are described rather than a net-change flow."
        )
    return " ".join(parts).strip()


_CUSTOMER_DRILL_CAPTION_IDS = frozenset({"accounts_receivable", "revenue"})
_VENDOR_DRILL_CAPTION_IDS = frozenset({
    "accounts_payable", "cost_of_revenue", "software_technology",
    "professional_services",
})


def _drill_party_label(ctx: FluxPromptContext) -> str:
    contract = _contract_of(ctx)
    caption_id = str(contract.get("caption_id") or "") if contract else ""
    if caption_id in _CUSTOMER_DRILL_CAPTION_IDS:
        return "customer"
    if caption_id in _VENDOR_DRILL_CAPTION_IDS:
        return "vendor"
    return "customer/vendor"


def _build_drill_down_attribution(ctx: FluxPromptContext) -> list[str]:
    """Real customer/vendor drill-down rollup (when transaction-linked) plus
    the ERP-capability coverage messaging. Backend-authoritative: party data
    comes from synced GL source fields, never invented."""
    from app.services.flux_ai.flux_ai_evidence import party_breakdown_lines

    lines: list[str] = []
    # Open sub-ledger party rollups (customer/vendor open balances) take
    # precedence when the caption has computed metrics, since they are the
    # contract drill-down dimension for AR/AP.
    metrics = _caption_metrics(ctx)
    if metrics is not None and metrics.top_parties:
        label = metrics.party_label
        for p in metrics.top_parties:
            name = (p.party_name or p.party_no).strip()
            tag = f"{label.capitalize()} {p.party_no} — {name}" if name and name != p.party_no else f"{label.capitalize()} {p.party_no}"
            ent = "entry" if p.entry_count == 1 else "entries"
            lines.append(
                f"By {label}: {tag} — {format_currency(p.open_amount)} open "
                f"across {p.entry_count} {ent}.",
            )
        if metrics.concentration_pct is not None:
            lines.append(
                f"Top {label} represents {metrics.concentration_pct:.0f}% of the "
                "open sub-ledger balance.",
            )
    breakdown = party_breakdown_lines(
        ctx.evidence, party_label=_drill_party_label(ctx),
    )
    for ln in breakdown:
        if ln not in lines:
            lines.append(ln)
    for ln in ctx.attribution_coverage_lines:
        if ln and ln not in lines:
            lines.append(ln)
    if not lines:
        lines.append(
            "Customer/vendor and poster-level attribution are not available "
            "from the current synced payload.",
        )
    return lines[:6]


def _build_decomposition(ctx: FluxPromptContext) -> list[str]:
    contract = _contract_of(ctx)
    if not contract:
        return [
            "A caption-specific decomposition is not defined; review the top "
            "mapped GL movements and available document-level entries.",
        ]
    rule = clean_ai_text(contract.get("decomposition_rule") or "")
    if contract.get("is_subtotal"):
        return [
            "This is a subtotal caption; the movement is explained through its "
            "component captions and is not decomposed at the transaction level.",
        ]

    caption_id = str(contract.get("caption_id") or "")
    evidence = ctx.evidence
    has_evidence = bool(
        evidence and (evidence.top_gl_movements or evidence.top_transactions)
    )
    lines: list[str] = []
    if rule:
        lines.append(f"Contract decomposition rule: {rule}")

    # Balance roll-forward families: present the closing-balance identity from
    # the computed opening (comparison) and closing (current) balances. The
    # additions/disposals/depreciation split is not separable from GL-only data.
    if caption_id in _ROLLFORWARD_CAPTION_IDS:
        lines.append(
            f"Roll-forward (balance at date): opening balance "
            f"{format_currency(ctx.comp_value)} plus net movement "
            f"{format_currency(ctx.current_value - ctx.comp_value)} equals closing "
            f"balance {format_currency(ctx.current_value)}.",
        )
        lines.append(
            "The additions / disposals / depreciation / reclassification split "
            "is not separable from the current synced GL data; the net movement "
            "is localised to the mapped GL accounts and entries below.",
        )
        return lines

    # Turnover families (AR/AP/Inventory): use the computed metrics when the
    # paired flow was available; otherwise state the limitation honestly.
    turnover_label = _TURNOVER_CAPTION_LABELS.get(caption_id)
    if turnover_label:
        metrics = _caption_metrics(ctx)
        if metrics is not None and metrics.turnover_current is not None:
            lines.append(
                f"{metrics.turnover_label}: {_fmt_days(metrics.turnover_comparison)} "
                f"to {_fmt_days(metrics.turnover_current)} "
                f"(change {_fmt_days(metrics.turnover_change)}), using "
                f"{metrics.flow_label} of "
                f"{format_currency(metrics.flow_comparison)} then "
                f"{format_currency(metrics.flow_current)}.",
            )
            if metrics.flow_effect is not None and metrics.turnover_effect is not None:
                flow_name = (
                    "Revenue effect" if caption_id == "accounts_receivable"
                    else "Purchasing effect" if caption_id == "accounts_payable"
                    else "Flow effect"
                )
                turn_name = (
                    "Collection effect" if caption_id == "accounts_receivable"
                    else "Payment-timing effect" if caption_id == "accounts_payable"
                    else "Turnover effect"
                )
                lines.append(
                    f"Estimated decomposition: {flow_name} "
                    f"{format_currency(metrics.flow_effect)} + {turn_name} "
                    f"{format_currency(metrics.turnover_effect)} = total movement "
                    f"{format_currency(metrics.balance_current - metrics.balance_comparison)} "
                    "(two-factor balance bridge).",
                )
            lines.append(
                "Allowance / write-off and FX effects are not separately "
                "identified in the current synced data and are therefore "
                "excluded from the split.",
            )
        else:
            lines.append(
                f"The {turnover_label} ratio could not be computed because the "
                "paired flow value was not found in generated Flux data; the "
                "movement is localised to the mapped GL accounts, the "
                "customer/vendor breakdown, and the dated entries below.",
            )
        return lines

    if has_evidence:
        lines.append(
            "Applied to the available evidence, the movement concentrates in the "
            "top mapped GL movements and document-level entries listed below; a "
            "fuller driver split is limited to what those entries support.",
        )
    else:
        lines.append(
            "The required decomposition cannot be fully completed from the "
            "current synced data; the variance should be reviewed using the top "
            "mapped GL movements and available document-level entries.",
        )
    return lines


def _build_pattern_or_anomaly(
    ctx: FluxPromptContext, timing_lines: list[str],
) -> str:
    contract = _contract_of(ctx)
    bands = clean_ai_text(contract.get("expected_variance_bands") or "") if contract else ""
    timing_clean = [clean_ai_text(t) for t in (timing_lines or []) if t and t != _MSG_TIMING_UNKNOWN]
    parts: list[str] = []
    if bands:
        parts.append(f"Expected pattern for this caption: {bands}")
    if timing_clean:
        parts.append(" ".join(timing_clean))

    anomalies = _metric_anomaly_sentences(ctx)
    if anomalies:
        parts.extend(anomalies)
    else:
        parts.append(
            "No contract-level anomaly trigger is evident from the available "
            "data; any anomaly should be confirmed against the supporting entries."
        )
    return " ".join(parts).strip()


def _metric_anomaly_sentences(ctx: FluxPromptContext) -> list[str]:
    """Turnover trend break / aging deterioration / concentration sentences
    derived strictly from computed metrics (never invented)."""
    metrics = _caption_metrics(ctx)
    if metrics is None:
        return []
    out: list[str] = []
    if (
        metrics.turnover_change is not None
        and abs(metrics.turnover_change) > _TURNOVER_TREND_BREAK_DAYS
    ):
        direction = "deterioration" if metrics.turnover_change > 0 else "improvement"
        out.append(
            f"{metrics.turnover_label} trend break: moved "
            f"{_fmt_days(metrics.turnover_change)} "
            f"({direction}), beyond the +/-{_TURNOVER_TREND_BREAK_DAYS:.0f}-day band.",
        )
    if (
        metrics.concentration_pct is not None
        and metrics.concentration_pct >= _CONCENTRATION_PCT_THRESHOLD
    ):
        out.append(
            f"{metrics.party_label.capitalize()} concentration: a single "
            f"{metrics.party_label} represents "
            f"{metrics.concentration_pct:.0f}% of the open balance.",
        )
    return out


def _build_escalation_triggers(ctx: FluxPromptContext) -> list[str]:
    triggers: list[str] = []
    metrics = _caption_metrics(ctx)
    if metrics is not None:
        if (
            metrics.turnover_change is not None
            and abs(metrics.turnover_change) > _TURNOVER_TREND_BREAK_DAYS
        ):
            triggers.append(
                f"{metrics.turnover_label} trend break "
                f"({_fmt_days(metrics.turnover_change)} change) exceeds the "
                f"+/-{_TURNOVER_TREND_BREAK_DAYS:.0f}-day band; escalate for review.",
            )
        if (
            metrics.concentration_pct is not None
            and metrics.concentration_pct >= _CONCENTRATION_PCT_THRESHOLD
        ):
            triggers.append(
                f"{metrics.party_label.capitalize()} concentration "
                f"({metrics.concentration_pct:.0f}% of open balance) warrants "
                "credit/collection review.",
            )
    if triggers:
        return triggers
    return [
        "No contract-level escalation trigger is evident from the available "
        "data; confirm against supporting entries before sign-off.",
    ]


def _sections_to_detailed_text(sections: dict[str, Any]) -> str:
    layout: tuple[tuple[str, str], ...] = (
        ("headline", "Headline"),
        ("variance_bridge", "Variance bridge"),
        ("contract_lens", "Contract lens"),
        ("primary_drivers", "Primary drivers"),
        ("decomposition", "Decomposition"),
        ("key_transaction_evidence", "Key transaction evidence"),
        ("drill_down_attribution", "Drill-down attribution"),
        ("pattern_or_anomaly", "Pattern or anomaly"),
        ("required_evidence_status", "Required evidence status"),
        ("reviewer_actions", "Reviewer actions"),
        ("escalation_triggers", "Escalation triggers"),
    )
    parts: list[str] = []
    for key, title in layout:
        parts.append(f"{title}:")
        value = sections.get(key)
        if key in _NARRATIVE_SECTION_KEYS:
            text = value if isinstance(value, str) else ""
            parts.append(text or f"({title.lower()} unavailable)")
        elif isinstance(value, list):
            if value:
                for line in value:
                    parts.append(f"- {line}")
            else:
                parts.append("- (none)")
        else:
            parts.append("- (none)")
        parts.append("")
    return "\n".join(parts).strip()


def _merge_cv_context(
    ai_lines: list[str] | None,
    authoritative: list[str],
) -> list[str]:
    ai_clean = [
        ln for ln in (ai_lines or [])
        if ln and not is_stale_ledger_unavailable_line(ln)
    ]
    if authoritative:
        return authoritative
    return ai_clean or authoritative


def _default_risk_flags(ctx: FluxPromptContext) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    if ctx.is_material:
        flags.append({
            "type": "materiality",
            "severity": "high",
            "message": "Variance is material under the configured threshold.",
        })
    metrics = _caption_metrics(ctx)
    if metrics is not None:
        if (
            metrics.turnover_change is not None
            and abs(metrics.turnover_change) > _TURNOVER_TREND_BREAK_DAYS
        ):
            ttype = (
                "dso_trend_break" if metrics.caption_id == "accounts_receivable"
                else "dpo_trend_break" if metrics.caption_id == "accounts_payable"
                else "trend_break"
            )
            flags.append({
                "type": ttype,
                "severity": "high",
                "message": (
                    f"{metrics.turnover_label} moved "
                    f"{_fmt_days(metrics.turnover_change)} beyond the "
                    f"+/-{_TURNOVER_TREND_BREAK_DAYS:.0f}-day band."
                ),
            })
        if (
            metrics.concentration_pct is not None
            and metrics.concentration_pct >= _CONCENTRATION_PCT_THRESHOLD
        ):
            flags.append({
                "type": "concentration",
                "severity": "medium",
                "message": (
                    f"A single {metrics.party_label} represents "
                    f"{metrics.concentration_pct:.0f}% of the open balance."
                ),
            })
    evidence = ctx.evidence
    if evidence and evidence.top_transactions:
        top = evidence.top_transactions[0]
        if abs(top.amount) >= abs(ctx.variance_amount) * 0.5:
            flags.append({
                "type": "large_transaction",
                "severity": "medium",
                "message": (
                    "A single large transaction may explain much of the movement."
                ),
            })
    if (
        ctx.evidence
        and not ctx.evidence.has_gl_party_links
        and ctx.customer_vendor_context_lines
        and any(
            "not available for the matched GL entries" in ln.lower()
            for ln in ctx.customer_vendor_context_lines
        )
    ):
        flags.append({
            "type": "missing_customer_vendor_link",
            "severity": "low",
            "message": (
                "Customer/vendor master is synced without ledger-level linkage."
            ),
        })
    if evidence and not evidence.mapped_gl_accounts:
        flags.append({
            "type": "mapping_review",
            "severity": "medium",
            "message": "No GL accounts are mapped for this caption.",
        })
    if evidence and evidence.posting_signal.period_end_concentration:
        flags.append({
            "type": "cutoff_risk",
            "severity": "medium",
            "message": "Material share of entries posted near period-end.",
        })
    if evidence and evidence.posting_signal.generic_narration_count >= 2:
        flags.append({
            "type": "insufficient_narration",
            "severity": "medium",
            "message": "Multiple matched entries have generic narration.",
        })
    if (
        evidence
        and not evidence.attribution_coverage.transaction_party_link_available
        and evidence.attribution_coverage.customer_vendor_master_synced
    ):
        flags.append({
            "type": "attribution_limitation",
            "severity": "low",
            "message": (
                "Customer/vendor master synced without transaction-level linkage."
            ),
        })
    return [_normalize_risk_flag(f) for f in flags]


def _merge_ai_with_evidence(
    clean: dict[str, Any], ctx: FluxPromptContext,
) -> dict[str, Any]:
    """Merge SLM output with backend evidence; never persist generic-only text."""
    evidence_sections = _build_evidence_backed_sections(ctx)
    ai_sections = clean.get("detailed_sections") or {}

    merged: dict[str, Any] = {}
    for key in _DETAIL_SECTION_KEYS:
        if key in _NARRATIVE_SECTION_KEYS:
            ai_text = ai_sections.get(key) if isinstance(ai_sections, dict) else ""
            merged[key] = (
                ai_text if isinstance(ai_text, str) and ai_text.strip()
                else evidence_sections.get(key, "")
            )
        else:
            ai_list = _coerce_string_array(
                ai_sections.get(key) if isinstance(ai_sections, dict) else None,
            )
            ev_list = evidence_sections.get(key, [])
            merged[key] = ai_list if ai_list else ev_list

    # Drill-down attribution is authoritative from backend party context: the
    # real customer/vendor rollup plus ERP-capability coverage messaging. We
    # never let the SLM's free-form drill-down override synced party evidence.
    backend_drill = _build_drill_down_attribution(ctx)
    if backend_drill:
        merged["drill_down_attribution"] = backend_drill

    # Decomposition and key transaction evidence are deterministic, evidence-
    # derived facts: the caption-contract decomposition rule plus the
    # roll-forward / turnover identity, and the dated GL entries with invoice
    # aging in days. These are backend-authoritative so the document-spec
    # decomposition and entry aging always surface, regardless of SLM output.
    ev_decomp = evidence_sections.get("decomposition")
    if ev_decomp:
        merged["decomposition"] = ev_decomp
    ev_txns = evidence_sections.get("key_transaction_evidence")
    if ev_txns:
        merged["key_transaction_evidence"] = ev_txns

    # Required-evidence status is always backend-authoritative (it is the
    # honest Available/Missing map plus computed evidence gaps).
    ev_status = evidence_sections.get("required_evidence_status")
    if ev_status:
        merged["required_evidence_status"] = ev_status

    # When computed metrics exist, the pattern/anomaly narrative is backend-
    # authoritative so DSO/DPO trend breaks and concentration always surface.
    if _caption_metrics(ctx) is not None:
        ev_pattern = evidence_sections.get("pattern_or_anomaly")
        if ev_pattern:
            merged["pattern_or_anomaly"] = ev_pattern

    concise = clean["concise_text"]
    if _caption_metrics(ctx) is not None:
        # Turnover captions must follow the exact contract concise format
        # (caption, direction, pct, amount, DSO/DPO/DIO, concentration).
        concise = _build_concise_from_evidence(ctx)
    elif _is_generic_concise(concise):
        concise = _build_concise_from_evidence(ctx)
    elif _comparison_period_label(ctx).lower() not in concise.lower():
        concise = _build_concise_from_evidence(ctx)

    risk_flags = clean.get("risk_flags") or []
    if not risk_flags:
        risk_flags = _default_risk_flags(ctx)

    return {
        "concise_text": concise,
        "detailed_text": _sections_to_detailed_text(merged),
        "attribution_coverage": merged.get("drill_down_attribution", []),
        "reviewer_actions": merged.get("reviewer_actions", []),
        "confidence_score": clean["confidence_score"],
        "risk_flags": risk_flags,
    }


# ── Fallback synthesis ─────────────────────────────────────────────────────

def synthesize_fallback_commentary(ctx: FluxPromptContext) -> dict[str, Any]:
    """Deterministic commentary when SLM output is missing or invalid."""
    caption = clean_ai_text(ctx.caption) or "Caption"
    comp = _comparison_period_label(ctx)
    direction = resolved_finance_direction(ctx)
    nb_expl = ctx.normal_balance_explanation or "mapped GL account movement"
    concise = (
        f"{caption} {direction} by {format_currency(ctx.variance_amount)} "
        f"({format_percent(ctx.variance_pct)}) versus {comp}. "
        f"The movement reflects {nb_expl} and should be reviewed against "
        f"the largest mapped GL movements."
    )
    if ctx.normal_balance in ("credit", "debit") and (
        ctx.current_value < 0 or ctx.comp_value < 0
    ):
        movement = build_movement_summary_clause(
            ctx.current_value,
            ctx.comp_value,
            ctx.normal_balance,  # type: ignore[arg-type]
            include_period_labels=False,
        )
        material = (
            "; this is material and requires review."
            if ctx.is_material
            else "."
        )
        concise = (
            f"{caption} {direction} by {format_currency(ctx.variance_amount)} "
            f"({format_percent(ctx.variance_pct)}) versus {comp}, "
            f"{movement}{material}"
        )

    # Turnover captions must follow the exact contract concise format even on
    # the deterministic fallback path.
    if _caption_metrics(ctx) is not None:
        concise = _build_concise_from_evidence(ctx)

    sections = _build_evidence_backed_sections(ctx)
    if ctx.normal_balance == "debit" and ctx.comp_value < 0:
        note = (
            "Comparison period includes a net credit / negative cost balance, so "
            "the variance should be reviewed as a movement from prior-period "
            "credit adjustment activity to current-period cost activity."
        )
        bridge = sections.get("variance_bridge", "")
        if note not in bridge:
            sections["variance_bridge"] = f"{bridge} {note}".strip()
    if not ctx.evidence:
        sections["headline"] = (
            f"{caption} {direction} by {format_currency(ctx.variance_amount)} "
            f"versus {comp}."
        )
        sections["variance_bridge"] = (
            "Current value and comparison value were computed by the backend; "
            "detailed bridge should be reviewed in Flux data."
        )
        sections["primary_drivers"] = [
            "Review the top mapped GL accounts by movement.",
        ]
        sections["decomposition"] = [
            "The required decomposition cannot be fully completed from the "
            "current synced data; review the top mapped GL movements and "
            "available document-level entries.",
        ]
        sections["key_transaction_evidence"] = [
            "Transaction-level evidence is not available for this caption; "
            "commentary is limited to mapped GL movements.",
        ]
        sections["drill_down_attribution"] = [
            "Customer/vendor and poster-level attribution are not available "
            "from the current synced payload.",
        ]
        sections["pattern_or_anomaly"] = (
            "Pattern could not be assessed from the current payload; no "
            "contract-level anomaly trigger is evident from available data."
        )
        sections["reviewer_actions"] = [
            "Validate mapped GL accounts and largest GL entries before sign-off.",
        ]

    return {
        "concise_text": concise,
        "detailed_text": _sections_to_detailed_text(sections),
        "attribution_coverage": sections.get("drill_down_attribution", []),
        "reviewer_actions": sections.get("reviewer_actions", []),
        "confidence_score": 0.35,
        "risk_flags": _default_risk_flags(ctx),
    }
