# @file    tests/test_flux_ai_service.py

from __future__ import annotations

from app.services.flux_ai.flux_ai_evidence import (
    AttributionCoverageEvidence,
    FluxAiEvidence,
    GlMovementEvidence,
    MappedGlAccount,
    PostingSignalEvidence,
    TransactionEvidence,
    TransactionPatternEvidence,
)
from app.services.flux_ai.gl_entry_party import PartySummaryEvidence
from app.services.flux_ai.flux_ai_prompt_service import FluxPromptContext
from app.services.flux_ai.flux_ai_service import (
    _build_executive_summary,
    _merge_ai_with_evidence,
    _validate_payload,
    synthesize_fallback_commentary,
)


def _evidence() -> FluxAiEvidence:
    return FluxAiEvidence(
        calculation_mode="net_change",
        calculation_mode_label="Net Change",
        current_period_label="Jan-Dec 2025",
        comparison_period_label="Jan-Dec 2024",
        mapped_gl_accounts=[
            MappedGlAccount(
                account_no="42010",
                account_name="Inventory Adjustment Materials",
                label="42010 — Inventory Adjustment Materials",
            ),
        ],
        top_gl_movements=[
            GlMovementEvidence(
                account_no="42010",
                account_name="Inventory Adjustment Materials",
                current_amount=0.0,
                comparison_amount=-120000.0,
                variance_amount=120000.0,
                majority_period="comparison",
            ),
        ],
        top_transactions=[
            TransactionEvidence(
                posting_date="2024-06-15",
                account_no="42010",
                account_name="Inventory Adjustment Materials",
                document_no="103001",
                description="Inventory adjustment",
                amount=-120000.0,
                period_bucket="comparison",
                party_type="customer",
                party_no="C10010",
                party_name="ABC Traders",
                formatted_line=(
                    "2024-06-15 | GL 42010 — Inventory Adjustment Materials "
                    "| Customer C10010 — ABC Traders | Doc 103001 | -$120,000 "
                    "| Inventory adjustment"
                ),
            ),
        ],
        has_gl_party_links=True,
        largest_party_line=(
            "The largest matched GL entries are linked to "
            "Customer C10010 — ABC Traders."
        ),
        transaction_pattern=TransactionPatternEvidence(
            current_transaction_count=0,
            comparison_transaction_count=10,
            majority_period="comparison",
            largest_transaction_amount=120000.0,
            pattern_lines=[
                "Most of the movement occurred in the comparison period; the "
                "current period has no matching transactions for this mapped caption.",
            ],
        ),
        posting_signal=PostingSignalEvidence(
            current_transaction_count=0,
            comparison_transaction_count=10,
            majority_period="comparison",
            largest_transaction_amount=120000.0,
            timing_lines=[
                "Most of the movement occurred in the comparison period, while the "
                "current period has no matching transactions for this mapped caption.",
            ],
        ),
        attribution_coverage=AttributionCoverageEvidence(
            customer_vendor_master_synced=True,
            transaction_party_link_available=True,
            poster_source_available=False,
        ),
        main_movement_line=(
            "GL 42010 — Inventory Adjustment Materials contributed the largest "
            "account-level movement"
        ),
        has_transaction_detail=True,
        has_gl_movement_detail=True,
    )


def _ctx(**overrides) -> FluxPromptContext:
    base = dict(
        entity_id="ACM-IN",
        entity_name="Acme India",
        statement="pl",
        caption="Cost of Revenue",
        period_from="Jan-Dec 2024",
        period_to="Jan-Dec 2025",
        current_value=0.0,
        comp_value=-356820.0,
        variance_amount=356820.0,
        variance_pct=100.0,
        is_material=True,
        threshold_rule=None,
        evidence=_evidence(),
        customer_vendor_context_lines=[
            "Customer/vendor context is derived from Business Central GL entry "
            "balancing account fields where available.",
        ],
        attribution_coverage_lines=[
            "The largest matched entries are linked to Customer C10010 — ABC Traders.",
        ],
        existing_comments=[],
    )
    base.update(overrides)
    return FluxPromptContext(**base)


def test_validate_payload_accepts_detailed_object_schema() -> None:
    payload = {
        "concise_text": (
            "Cost of Revenue increased by $356,820 (+100%) versus Jan-Dec 2024, "
            "mainly driven by GL 42010 activity; this is material and requires review."
        ),
        "detailed": {
            "executive_summary": "Cost of Revenue increased by $356,820.",
            "variance_bridge": "Variance bridge: Current period $0 less comparison period -$356,820 resulted in a $356,820 increase.",
            "primary_drivers": ["GL 42010 drove comparison-period movement."],
            "key_transaction_evidence": ["2024-06-15 | GL 42010 | Doc 103001"],
            "timing_and_posting_signals": ["Movement concentrated in comparison period."],
            "attribution_coverage": ["Master synced without ledger link."],
            "reviewer_actions": ["Review largest GL entries before sign-off."],
        },
        "risk_flags": [
            {"type": "materiality", "severity": "high", "message": "Material variance."},
        ],
        "confidence_score": 0.7,
    }
    clean, code = _validate_payload(payload)
    assert code is None
    assert clean is not None
    assert clean["concise_text"].startswith("Cost of Revenue")
    assert clean["detailed_sections"]["primary_drivers"][0].startswith("GL 42010")


def test_validate_payload_repairs_mojibake() -> None:
    payload = {
        "concise_text": "Prepaid Expenses \u00e2\u20ac\u201d drivers",
        "detailed": {"summary": "Driver: rate\u00e2\u20ac\u2122s effect."},
        "confidence_score": 0.5,
    }
    clean, code = _validate_payload(payload)
    assert code is None
    assert clean is not None
    assert "\u00e2\u20ac" not in clean["concise_text"]


def test_validate_payload_rejects_missing_concise_text() -> None:
    payload = {"detailed": {"summary": "ok"}}
    clean, code = _validate_payload(payload)
    assert clean is None
    assert code == "SLM_SCHEMA_INVALID"


def test_merge_replaces_generic_concise_with_evidence() -> None:
    clean, _ = _validate_payload({
        "concise_text": "Revenue variance is within model expectations.",
        "detailed": {"summary": "ignored"},
        "confidence_score": 0.6,
    })
    assert clean is not None
    merged = _merge_ai_with_evidence(clean, _ctx())
    assert "within model expectations" not in merged["concise_text"].lower()
    assert "Jan-Dec 2024" in merged["concise_text"]
    assert (
        "net credit / negative cost position" in merged["concise_text"]
        or "42010" in merged["concise_text"]
    )


def test_merge_detailed_has_contract_sections() -> None:
    clean, _ = _validate_payload({
        "concise_text": "Cost of Revenue increased by $356,820 (+100%) versus Jan-Dec 2024.",
        "confidence_score": 0.5,
    })
    assert clean is not None
    merged = _merge_ai_with_evidence(clean, _ctx())
    text = merged["detailed_text"]
    for heading in (
        "Headline:",
        "Variance bridge:",
        "Contract lens:",
        "Primary drivers:",
        "Decomposition:",
        "Key transaction evidence:",
        "Drill-down attribution:",
        "Pattern or anomaly:",
        "Reviewer actions:",
        "Escalation triggers:",
    ):
        assert heading in text
    assert "42010" in text
    assert "103001" in text


def test_fallback_concise_matches_spec_template() -> None:
    out = synthesize_fallback_commentary(_ctx())
    assert "Cost of Revenue" in out["concise_text"]
    assert "increased by" in out["concise_text"]
    assert "$356,820" in out["concise_text"]
    assert "Jan-Dec 2024" in out["concise_text"]
    assert "net credit / negative cost position" in out["concise_text"]


def test_fallback_detailed_sections_in_order() -> None:
    out = synthesize_fallback_commentary(_ctx())
    text = out["detailed_text"]
    sections = [
        "Headline:",
        "Variance bridge:",
        "Contract lens:",
        "Primary drivers:",
        "Decomposition:",
        "Key transaction evidence:",
        "Drill-down attribution:",
        "Pattern or anomaly:",
        "Reviewer actions:",
        "Escalation triggers:",
    ]
    indexes = [text.index(s) for s in sections]
    assert indexes == sorted(indexes)


def test_fallback_without_evidence_uses_placeholder_messages() -> None:
    out = synthesize_fallback_commentary(_ctx(evidence=None))
    assert "Transaction-level evidence is not available" in out["detailed_text"]
    assert "Pattern could not be assessed" in out["detailed_text"]


def test_output_is_ascii_safe() -> None:
    out = synthesize_fallback_commentary(_ctx())
    for ch in ("\u00e2\u20ac", "\u00c2"):
        assert ch not in out["detailed_text"]
        assert ch not in out["concise_text"]


def test_executive_summary_negative_comparison_cost() -> None:
    ctx = FluxPromptContext(
        entity_id="ACM-IN",
        entity_name="Acme India",
        statement="pl",
        caption="Cost of Revenue",
        period_from="Jan 2025 - Dec 2025",
        period_to="Jan 2024 - Dec 2024",
        current_value=18191.20,
        comp_value=-280681.00,
        variance_amount=298872.20,
        variance_pct=106.5,
        is_material=True,
        normal_balance="debit",
        finance_current_value=18191.20,
        finance_comparison_value=-280681.00,
        finance_direction="increased",
        comparison_value_sign_label="net credit / negative cost position",
    )
    summary = _build_executive_summary(ctx)
    assert "net credit / negative cost position" in summary
    assert "current period cost" in summary
    assert "increased" in summary
    assert "-$280,681" not in summary
