# @file    services/flux_ai/flux_ai_prompt_service.py

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.services.ai_text_clean import clean_ai_text, format_currency, format_percent
from app.services.flux_ai.flux_ai_evidence import FluxAiEvidence


_SUBTOTAL_PL_CAPTIONS = frozenset({
    "gross profit", "operating income", "net income", "ebitda", "ebit",
})
_SUBTOTAL_BS_CAPTIONS = frozenset({
    "total assets", "total liabilities", "total equity", "total liabilities and equity",
})


# Bumped whenever the prompt contract (sections, rules, output schema)
# changes. Stored in the audit trail for traceability; not a DB column.
PROMPT_VERSION = "flux-ai-2.0"


FLUX_AI_SYSTEM_PROMPT = (
    "You are a senior finance analyst writing flux variance commentary for an "
    "audit-friendly month-end close package. Every caption has a CAPTION "
    "CONTRACT that defines exactly how it must be analyzed; follow it strictly.\n\n"
    "DETERMINISTIC CORE, GENERATIVE EDGE:\n"
    "- All numbers (variance, percentages, balances, transaction amounts) are "
    "pre-computed in PREPARED EVIDENCE. Never recalculate.\n"
    "- Use ONLY mapped GL accounts and transactions supplied in evidence.\n"
    "- Never invent customers, vendors, GL accounts, amounts, posters, "
    "narration, or drivers.\n\n"
    "CAPTION CONTRACT (the caption_contract object in the payload):\n"
    "- Use ONLY the contract's permitted_drivers to explain the movement. Do "
    "not introduce any driver outside permitted_drivers.\n"
    "- Apply the contract decomposition_rule. Where a decomposition is required, "
    "the named effects (e.g. volume + price + mix + FX, or a roll-forward) MUST "
    "sum to the computed variance within a rounding tolerance. If they cannot be "
    "made to sum from the evidence, do NOT force a residual: abstain on the "
    "decomposition, state it is not fully supported by available evidence, and "
    "add a decomposition_gap risk flag. Never fake volume/price/mix or "
    "roll-forward components and never attribute a movement to 'general growth'.\n"
    "- Use the contract drill_down_dimensions for drill_down_attribution. Only "
    "name a customer/vendor/SKU/asset when transaction-linked evidence exists.\n"
    "- Compare the movement against expected_variance_bands and "
    "recurring_signatures. Flag an anomaly only when an actual anomaly_triggers "
    "condition is met by the evidence.\n"
    "- For subtotal captions (is_subtotal true), explain through component "
    "captions only and do NOT perform transaction-level drill-down.\n"
    "- For balance_at_date captions (is_balance_at_date / balance_at_date true), "
    "describe the closing balance and its roll-forward, not a net-change flow.\n\n"
    "FORBIDDEN PHRASES (never use):\n"
    "- within model expectations\n"
    "- in line with forecast\n"
    "- likely due to business growth\n"
    "- normal business activity\n"
    "- seasonal impact (unless evidence supports it)\n"
    "- operational dynamics\n"
    "- Material - review required (as standalone commentary)\n"
    "- as expected / immaterial movement (without evidence)\n\n"
    "NO FORWARD-LOOKING STATEMENTS:\n"
    "- Explain only what happened between the two historical periods. Do not "
    "forecast, predict, or advise on future performance, and do not characterize "
    "a movement as good or bad beyond what the evidence supports. Whether a "
    "variance is favourable is left to the analyst.\n\n"
    "CLOSED ANOMALY VOCABULARY AND TIMING SIGNALS:\n"
    "- When you flag an anomaly, classify it from the closed risk_flags type "
    "list only; never invent a free-form anomaly category.\n"
    "- Timing signals from posting dates: period-end clustering (a material "
    "share of the variance posted in the final days -> cutoff_risk); back-dating "
    "(posting date precedes creation date or lands after soft close); off-hours "
    "manual posting (low-weight signal). Surface these as timing_distortion / "
    "cutoff_risk only when the evidence shows them.\n"
    "- Quote a narration ONLY when it is consistent with the entry's movement; "
    "if a material entry's narration is missing, generic, or contradicts the "
    "movement, flag it rather than repeating it.\n\n"
    "SIGN-AWARE WORDING (mandatory when raw values are negative):\n"
    "- Debit-normal expense/cost captions: never say 'compared to -$X'. Use "
    "'moving from a net credit / negative cost position of $X to a current "
    "period cost of $Y'.\n"
    "- Credit-normal revenue/income captions: use 'revenue credit balance of $X' "
    "and finance-normalized magnitudes; do not call negative raw values "
    "'negative revenue'.\n"
    "- Use finance_direction, finance_variance_amount, and "
    "finance_variance_percentage for increased/decreased wording.\n"
    "- Use current_value_sign_label and comparison_value_sign_label when present.\n\n"
    "CONCISE_TEXT: exactly ONE professional finance sentence including caption "
    "name, increased/decreased direction, formatted variance amount, formatted "
    "variance percentage, comparison period label, the dominant permitted "
    "driver from evidence, and a materiality note ONLY when is_material is true.\n"
    "- Template: '{caption} {increased/decreased} by {amount} ({pct}) versus "
    "{comparison_period}, driven mainly by {dominant_driver}; {materiality_note}.'\n"
    "- If no dominant driver is supported by evidence: '{caption} "
    "{increased/decreased} by {amount} ({pct}) versus {comparison_period}; "
    "driver evidence is limited to mapped GL movement and requires review.'\n\n"
    "DETAILED: populate ALL eleven sections using ONLY prepared evidence and "
    "the caption contract:\n"
    "1. headline - restate the movement with both current and comparison values "
    "and the calculation mode.\n"
    "2. variance_bridge - how current vs comparison moved; explain negative "
    "expense/revenue values in accounting-friendly language.\n"
    "3. contract_lens - state which caption contract was applied and what this "
    "caption normally moves with (normal_movement / permitted_drivers).\n"
    "4. primary_drivers - top GL/subledger drivers from evidence only, largest "
    "first; aggregate the immaterial tail.\n"
    "5. decomposition - apply the contract decomposition_rule; if data is "
    "insufficient, say the decomposition is not fully supported by available "
    "evidence.\n"
    "6. key_transaction_evidence - copy formatted_line from top_transactions "
    "(date, GL, document, source, party if linked, amount, narration); append "
    "'Narration is generic; supporting evidence should be requested.' when "
    "narration_is_generic is true; if poster is unavailable, say poster is not "
    "available.\n"
    "7. drill_down_attribution - follow the contract drill_down_dimensions; name "
    "parties only when transaction-linked.\n"
    "8. pattern_or_anomaly - compare against expected_variance_bands and "
    "recurring_signatures; flag an anomaly only when an anomaly_triggers "
    "condition is actually met. Use caption_metrics turnover_change for DSO/DPO/"
    "DIO trend breaks and concentration_pct for customer/vendor concentration.\n"
    "9. required_evidence_status - for each contract required_evidence item, "
    "state Available or Missing based on caption_metrics; then list each "
    "caption_metrics evidence_gap. Never claim an evidence item is present when "
    "the metric is null.\n"
    "10. reviewer_actions - concrete finance review actions based on the contract.\n"
    "11. escalation_triggers - state whether any contract escalation trigger is "
    "present; if none, say no contract-level escalation trigger is evident from "
    "available data.\n\n"
    "CAPTION METRICS (caption_metrics object, when present):\n"
    "- It carries pre-computed turnover (DSO/DPO/DIO), aging buckets, effect "
    "splits, sub-ledger party/invoice rollups, and an evidence_gaps list. "
    "Echo these numbers exactly; never compute your own DSO/DPO/DIO, aging, or "
    "effect amounts. When a metric field is null, treat it as Missing and use "
    "the matching evidence_gap wording.\n\n"
    "EVIDENCE LIMITATION WORDING:\n"
    "- No customer/vendor data: 'Customer/vendor-level detail is not available "
    "in the current synced ERP payload; review is based on mapped GL account and "
    "document-level transaction evidence.'\n"
    "- No transaction evidence: 'Transaction-level evidence is not available for "
    "this caption; commentary is limited to mapped GL movements.'\n"
    "- Decomposition not possible: 'The required decomposition cannot be fully "
    "completed from the current synced data; the variance should be reviewed "
    "using the top mapped GL movements and available document-level entries.'\n\n"
    "Plain ASCII only. No markdown. No emojis. No mojibake.\n"
    "Return exactly one JSON object:\n"
    "{\n"
    '  "concise_text": "string",\n'
    '  "detailed": {\n'
    '    "headline": "string",\n'
    '    "variance_bridge": "string",\n'
    '    "contract_lens": "string",\n'
    '    "primary_drivers": ["string"],\n'
    '    "decomposition": ["string"],\n'
    '    "key_transaction_evidence": ["string"],\n'
    '    "drill_down_attribution": ["string"],\n'
    '    "pattern_or_anomaly": "string",\n'
    '    "required_evidence_status": ["string"],\n'
    '    "reviewer_actions": ["string"],\n'
    '    "escalation_triggers": ["string"]\n'
    "  },\n"
    '  "risk_flags": [{"type": "materiality|large_transaction|mapping_review|'
    'cutoff_risk|sign_inconsistency|missing_evidence|attribution_limitation|'
    'decomposition_gap|manual_journal_risk|recurring_pattern|trend_break|'
    'concentration|timing_distortion|dso_trend_break|dpo_trend_break|'
    'aging_deterioration|margin_trend_break", '
    '"severity": "low|medium|high", "message": "string"}],\n'
    '  "confidence_score": 0.0\n'
    "}\n"
)


@dataclass
class FluxPromptContext:
    entity_id: str
    entity_name: str
    statement: str
    caption: str
    period_from: str
    period_to: str
    current_value: float
    comp_value: float
    variance_amount: float
    variance_pct: float | None
    is_material: bool
    threshold_rule: str | None = None
    normal_balance: str = "debit"
    raw_variance_amount: float = 0.0
    finance_current_value: float = 0.0
    finance_comparison_value: float = 0.0
    finance_direction: str = "flat"
    current_value_sign_label: str | None = None
    comparison_value_sign_label: str | None = None
    normal_balance_explanation: str = ""
    evidence: FluxAiEvidence | None = None
    customer_vendor_context_lines: list[str] = field(default_factory=list)
    attribution_coverage_lines: list[str] = field(default_factory=list)
    existing_comments: list[str] = field(default_factory=list)
    # Resolved caption-wise analysis contract (caption_contracts.py). None for
    # captions without a dedicated contract (e.g. some subtotals); the prompt
    # then falls back to the generic _caption_guidance hints.
    contract: dict[str, Any] | None = None


def resolved_finance_direction(ctx: FluxPromptContext) -> str:
    """Prefer explicit finance_direction; derive from variance when unset."""
    if ctx.finance_direction in ("increased", "decreased"):
        return ctx.finance_direction
    if ctx.variance_amount > 0:
        return "increased"
    if ctx.variance_amount < 0:
        return "decreased"
    return "flat"


def resolved_finance_bridge_word(ctx: FluxPromptContext) -> str:
    direction = resolved_finance_direction(ctx)
    if direction == "increased":
        return "increase"
    if direction == "decreased":
        return "decrease"
    return "no change"


def _caption_guidance(caption: str, statement: str) -> str:
    c = clean_ai_text(caption).lower()
    stmt = (statement or "").lower()
    if c in _SUBTOTAL_PL_CAPTIONS or c in _SUBTOTAL_BS_CAPTIONS:
        return (
            "Subtotal caption: reference underlying component caption drivers; "
            "do not re-explain every transaction."
        )
    hints: list[str] = []
    if any(k in c for k in ("revenue", "sales", "consulting", "product")):
        hints.append(
            "Revenue caption: explain via mapped sales/revenue GL accounts and "
            "top transactions; mention lower/higher activity only when supported."
        )
    if "cost of revenue" in c or "cogs" in c:
        hints.append(
            "Cost of Revenue: focus on COGS/inventory adjustment GLs; flag "
            "comparison-period-only movement when evidence shows it."
        )
    if "salary" in c or "wage" in c or "payroll" in c:
        hints.append(
            "Payroll caption: never name individuals; use department/cohort only "
            "if in evidence; otherwise GL-level movement only."
        )
    if "cash" in c and stmt in ("bs", "balance_sheet"):
        hints.append(
            "Cash: Balance at Date; recommend bank reconciliation for material entries."
        )
    if "receivable" in c:
        hints.append(
            "AR: mention customers only with transaction-level linkage; otherwise "
            "state attribution limitation."
        )
    if "inventory" in c:
        hints.append(
            "Inventory: highlight adjustment entries when they drive material movement."
        )
    if "payable" in c or "accrued" in c:
        hints.append(
            "Payables/accruals: flag generic narration (accrual, true-up) when material."
        )
    return " ".join(hints)


def build_flux_ai_system_prompt() -> str:
    return FLUX_AI_SYSTEM_PROMPT


def build_flux_ai_user_prompt(ctx: FluxPromptContext) -> str:
    evidence = ctx.evidence
    direction = resolved_finance_direction(ctx)
    payload: dict[str, Any] = {
        "entity": ctx.entity_id,
        "entity_name": ctx.entity_name,
        "caption": ctx.caption,
        "statement": ctx.statement.upper(),
        "normal_balance": ctx.normal_balance,
        "current_period": evidence.current_period_label if evidence else ctx.period_from,
        "comparison_period": (
            evidence.comparison_period_label if evidence else ctx.period_to
        ),
        "raw_current_value": ctx.current_value,
        "raw_comparison_value": ctx.comp_value,
        "raw_variance_amount": ctx.raw_variance_amount,
        "finance_current_value": ctx.finance_current_value,
        "finance_comparison_value": ctx.finance_comparison_value,
        "finance_variance_amount": ctx.variance_amount,
        "finance_variance_percentage": ctx.variance_pct,
        "finance_direction": direction,
        "current_value_sign_label": ctx.current_value_sign_label,
        "comparison_value_sign_label": ctx.comparison_value_sign_label,
        "normal_balance_explanation": ctx.normal_balance_explanation,
        "current_value": ctx.current_value,
        "comparison_value": ctx.comp_value,
        "variance_amount": ctx.variance_amount,
        "variance_percentage": ctx.variance_pct,
        "direction": direction,
        "is_material": ctx.is_material,
        "threshold_rule": ctx.threshold_rule,
        "formatted_variance_amount": format_currency(ctx.variance_amount),
        "formatted_variance_percentage": format_percent(ctx.variance_pct),
        "formatted_current_value": format_currency(ctx.current_value),
        "formatted_comparison_value": format_currency(ctx.comp_value),
        "formatted_finance_current_value": format_currency(ctx.finance_current_value),
        "formatted_finance_comparison_value": format_currency(
            ctx.finance_comparison_value,
        ),
        "caption_guidance": _caption_guidance(ctx.caption, ctx.statement),
        "attribution_coverage_lines": ctx.attribution_coverage_lines,
        "existing_comments": ctx.existing_comments,
    }
    if ctx.contract:
        payload["caption_contract"] = ctx.contract
        payload["contract_note"] = (
            "Explain this caption strictly through caption_contract: use only "
            "permitted_drivers, follow decomposition_rule, use "
            "drill_down_dimensions, and respect is_subtotal / balance_at_date. "
            "Flag decomposition_gap if the decomposition cannot be supported by "
            "the evidence."
        )
    if ctx.normal_balance == "credit":
        payload["variance_note"] = (
            "Credit-normal revenue/income caption: use finance_direction, "
            "finance_variance_amount, and finance_variance_percentage for "
            "increased/decreased wording. Do not infer direction from "
            "raw_variance_amount alone. Use revenue credit balance wording "
            "for negative raw values."
        )
    if ctx.normal_balance == "debit" and (
        ctx.comp_value < 0 or ctx.current_value < 0
    ):
        payload["sign_note"] = (
            "Debit-normal expense/cost caption with negative raw value: explain "
            "using net credit / negative cost position wording; do not present "
            "bare negative expense amounts without context."
        )
    if ctx.comp_value == 0:
        payload["percentage_note"] = (
            "Percentage variance is not meaningful because the comparison value is zero."
        )
    if evidence:
        payload.update(evidence.to_prompt_dict(
            direction=direction, is_material=ctx.is_material,
        ))
    return (
        "Produce commentary JSON using ONLY this prepared evidence.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}\n"
    )


def get_flux_ai_messages(ctx: FluxPromptContext) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": build_flux_ai_system_prompt()},
        {"role": "user",   "content": build_flux_ai_user_prompt(ctx)},
    ]
