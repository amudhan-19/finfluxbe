# @file    tests/test_caption_metrics.py
#
# Unit tests for the caption-specific computed evidence (DSO/DPO/DIO, aging,
# effect split, sub-ledger rollups) and the caption-contract-aware rendering
# that consumes it. These tests are DB-free: they construct the metric and
# evidence objects directly and exercise the deterministic builders.

from __future__ import annotations

from datetime import date

from app.services.flux_ai.caption_contracts import (
    contract_prompt_block,
    resolve_caption_contract,
)
from app.services.flux_ai.caption_metrics import (
    AgingBucketEvidence,
    CaptionInvoiceEvidence,
    CaptionMetricEvidence,
    CaptionPartyRollup,
    _aging_buckets,
    _effect_split,
    _turnover_days,
    format_turnover_days,
    metric_evidence_status_lines,
)
from app.services.flux_ai.flux_ai_evidence import (
    FluxAiEvidence,
    TransactionEvidence,
    _dedupe_transactions,
)
from app.services.flux_ai.flux_ai_prompt_service import FluxPromptContext
from app.services.flux_ai.flux_ai_service import (
    _build_concise_from_evidence,
    _build_decomposition,
    _build_drill_down_attribution,
    _build_escalation_triggers,
    _build_key_transaction_evidence,
    _build_pattern_or_anomaly,
    _build_required_evidence_status,
    _default_risk_flags,
)


# ── Pure metric math ───────────────────────────────────────────────────────

def test_turnover_days_basic():
    # 100 balance / 1000 flow over 365 days => 36.5 days
    assert _turnover_days(100.0, 1000.0, 365) == 36.5


def test_turnover_days_missing_flow_returns_none():
    assert _turnover_days(100.0, None, 365) is None
    assert _turnover_days(100.0, 0.0, 365) is None
    assert _turnover_days(100.0, 1000.0, 0) is None


def test_effect_split_sums_to_balance_movement():
    flow_effect, turnover_effect = _effect_split(
        balance_current=71623.0,
        balance_comparison=44370.0,
        flow_current=500000.0,
        turnover_comparison=40.0,
        days_current=365,
    )
    assert flow_effect is not None and turnover_effect is not None
    total = flow_effect + turnover_effect
    assert abs(total - (71623.0 - 44370.0)) < 0.01


def test_effect_split_missing_inputs_returns_none():
    assert _effect_split(
        balance_current=1.0, balance_comparison=1.0,
        flow_current=None, turnover_comparison=10.0, days_current=365,
    ) == (None, None)


def test_aging_buckets_partition_by_age():
    as_of = date(2025, 12, 31)

    class _Row:
        def __init__(self, remaining, posting):
            self.remaining_amount = remaining
            self.posting_date = posting

    rows = [
        _Row(1000.0, date(2025, 12, 20)),   # 11 days -> 0-30
        _Row(2000.0, date(2025, 11, 15)),   # 46 days -> 31-60
        _Row(3000.0, date(2025, 6, 1)),     # ~213 days -> 90+
        _Row(0.0, date(2025, 12, 1)),       # fully paid -> ignored
    ]
    buckets = _aging_buckets(rows, as_of)
    by_label = {b.label: b for b in buckets}
    assert by_label["0-30 days"].amount == 1000.0
    assert by_label["31-60 days"].amount == 2000.0
    assert by_label["90+ days"].amount == 3000.0
    assert by_label["61-90 days"].amount == 0.0


def test_format_turnover_days():
    assert format_turnover_days(None) == "n/a"
    assert format_turnover_days(52.275) == "52.3 days"


def test_evidence_status_lines_available_vs_missing():
    metrics = _ar_metrics_available()
    lines = metric_evidence_status_lines(
        metrics,
        ["current DSO", "FX effect", "top customers", "aging buckets"],
    )
    joined = " ".join(lines)
    assert "DSO" in joined and "Available" in joined
    assert "FX effect: Missing" in joined
    assert "top customers: Available" in joined
    assert "aging buckets: Available (current only)" in joined


# ── Transaction de-duplication ──────────────────────────────────────────────

def _txn(doc, amount, party="C1", date_str="2025-03-01") -> TransactionEvidence:
    return TransactionEvidence(
        posting_date=date_str,
        account_no="1200",
        account_name="AR",
        document_no=doc,
        description="Invoice",
        amount=amount,
        period_bucket="current",
        formatted_line=f"{date_str} | Doc {doc} | {amount} | Invoice",
        party_no=party,
    )


def test_dedupe_collapses_identical_entries():
    txns = [
        _txn("INV1", 1000.0),
        _txn("INV1", 1000.0),    # identical -> collapsed
        _txn("INV1", 1000.0),    # identical -> collapsed
        _txn("INV2", 500.0),
    ]
    out = _dedupe_transactions(txns)
    assert len(out) == 2
    rep = next(t for t in out if t.document_no == "INV1")
    assert "x3 identical entries" in rep.formatted_line
    other = next(t for t in out if t.document_no == "INV2")
    assert "identical entries" not in other.formatted_line


# ── Metric-aware rendering (DSO available / unavailable) ─────────────────────

def _ar_metrics_available() -> CaptionMetricEvidence:
    return CaptionMetricEvidence(
        caption_id="accounts_receivable",
        party_label="customer",
        turnover_label="DSO",
        flow_label="revenue",
        balance_current=71623.0,
        balance_comparison=44370.0,
        flow_current=500000.0,
        flow_comparison=400000.0,
        days_current=365,
        days_comparison=365,
        turnover_current=52.3,
        turnover_comparison=40.5,
        turnover_change=11.8,
        flow_effect=15000.0,
        turnover_effect=12253.0,
        aging_buckets_current=[
            AgingBucketEvidence("0-30 days", 30000.0, 4),
            AgingBucketEvidence("31-60 days", 20000.0, 2),
            AgingBucketEvidence("61-90 days", 0.0, 0),
            AgingBucketEvidence("90+ days", 21623.0, 3),
        ],
        top_parties=[
            CaptionPartyRollup("10000", "Adatum Corporation", 50000.0, 6),
            CaptionPartyRollup("10010", "Trey Research", 21623.0, 3),
        ],
        top_invoices=[
            CaptionInvoiceEvidence(
                "INV-100", "10000", "Adatum Corporation",
                "2025-02-12", 16514.0, 322, "Consulting",
            ),
        ],
        concentration_pct=69.8,
        evidence_gaps=[
            "Comparison-period aging snapshot is not retained by the ERP sync.",
            "Allowance / write-off movements and FX revaluation are not "
            "separately identified in the current synced payload.",
        ],
    )


def _ar_metrics_no_dso() -> CaptionMetricEvidence:
    m = _ar_metrics_available()
    m.flow_current = None
    m.flow_comparison = None
    m.turnover_current = None
    m.turnover_comparison = None
    m.turnover_change = None
    m.flow_effect = None
    m.turnover_effect = None
    m.evidence_gaps.insert(
        0, "DSO could not be computed because the paired revenue value was "
        "not found in generated Flux data for both periods.",
    )
    return m


def _ar_ctx(metrics: CaptionMetricEvidence) -> FluxPromptContext:
    evidence = FluxAiEvidence(
        calculation_mode="balance_at_date",
        calculation_mode_label="Balance at Date",
        current_period_label="Jan-Dec 2025",
        comparison_period_label="Jan-Dec 2024",
        caption_metrics=metrics,
    )
    contract = resolve_caption_contract("Accounts Receivable", "bs")
    return FluxPromptContext(
        entity_id="E1",
        entity_name="Acme",
        statement="bs",
        caption="Accounts Receivable",
        period_from="Jan-Dec 2024",
        period_to="Jan-Dec 2025",
        current_value=71623.0,
        comp_value=44370.0,
        variance_amount=27253.0,
        variance_pct=61.4,
        is_material=True,
        normal_balance="debit",
        finance_current_value=71623.0,
        finance_comparison_value=44370.0,
        finance_direction="increased",
        evidence=evidence,
        contract=contract_prompt_block(contract),
    )


def test_ar_concise_includes_dso_and_customer():
    concise = _build_concise_from_evidence(_ar_ctx(_ar_metrics_available()))
    assert "DSO" in concise
    assert "40.5 days" in concise and "52.3 days" in concise
    assert "Adatum Corporation" in concise


def test_ar_concise_dso_unavailable_fallback():
    concise = _build_concise_from_evidence(_ar_ctx(_ar_metrics_no_dso()))
    assert "unavailable" in concise.lower()
    # Still concentrates on the top customer when parties are linked.
    assert "Adatum Corporation" in concise


def test_ar_decomposition_uses_effect_split_when_available():
    lines = _build_decomposition(_ar_ctx(_ar_metrics_available()))
    joined = " ".join(lines)
    assert "Revenue effect" in joined
    assert "Collection effect" in joined
    assert "DSO" in joined


def test_ar_decomposition_states_limitation_when_dso_missing():
    lines = _build_decomposition(_ar_ctx(_ar_metrics_no_dso()))
    joined = " ".join(lines).lower()
    assert "could not be computed" in joined


def test_ar_required_evidence_status_lists_gaps():
    lines = _build_required_evidence_status(_ar_ctx(_ar_metrics_available()))
    joined = " ".join(lines)
    assert any("Evidence gap" in ln for ln in lines)
    assert "FX" in joined or "Allowance" in joined


def test_ar_pattern_flags_dso_trend_break_and_concentration():
    pattern = _build_pattern_or_anomaly(_ar_ctx(_ar_metrics_available()), [])
    assert "trend break" in pattern.lower()
    assert "concentration" in pattern.lower()


def test_ar_escalation_triggers_from_metrics():
    triggers = _build_escalation_triggers(_ar_ctx(_ar_metrics_available()))
    joined = " ".join(triggers).lower()
    assert "trend break" in joined or "concentration" in joined


def test_ar_drill_down_names_customers():
    lines = _build_drill_down_attribution(_ar_ctx(_ar_metrics_available()))
    joined = " ".join(lines)
    assert "Adatum Corporation" in joined
    assert "By customer" in joined


def test_ar_key_transaction_evidence_shows_invoice_age():
    lines = _build_key_transaction_evidence(_ar_ctx(_ar_metrics_available()))
    joined = " ".join(lines)
    assert "aged 322 days" in joined
    assert "INV-100" in joined


def test_ar_default_risk_flags_include_dso_trend_break():
    flags = _default_risk_flags(_ar_ctx(_ar_metrics_available()))
    # Normalized risk flags expose the type under the "risk" key.
    types = {f["risk"] for f in flags}
    assert "dso_trend_break" in types
    assert "concentration" in types


# ── Subtotal captions must not drill down ───────────────────────────────────

def test_subtotal_caption_decomposition_no_transaction_drilldown():
    contract = resolve_caption_contract("Gross Profit", "pl")
    assert contract is not None and contract.is_subtotal
    ctx = FluxPromptContext(
        entity_id="E1", entity_name="Acme", statement="pl",
        caption="Gross Profit", period_from="p1", period_to="p2",
        current_value=100.0, comp_value=80.0, variance_amount=20.0,
        variance_pct=25.0, is_material=False, normal_balance="credit",
        contract=contract_prompt_block(contract),
    )
    lines = _build_decomposition(ctx)
    assert any("subtotal" in ln.lower() for ln in lines)
    assert any("component" in ln.lower() for ln in lines)


# ── Caption alias resolver ───────────────────────────────────────────────────

def test_caption_alias_resolution():
    cases = {
        "Product Revenue": "revenue",
        "Consulting Revenue": "revenue",
        "Total Revenue": "revenue",
        "Cost of Revenue": "cost_of_revenue",
        "Depreciation": "depreciation_amortization",
        "Cash": "cash",
        "Accounts Receivable": "accounts_receivable",
        "Long-Term Debt": "debt",
        "Equity": "equity",
    }
    for label, expected in cases.items():
        contract = resolve_caption_contract(label, None)
        assert contract is not None, f"{label} did not resolve"
        assert contract.caption_id == expected, (
            f"{label} -> {contract.caption_id}, expected {expected}"
        )


def test_unknown_caption_resolves_to_none():
    assert resolve_caption_contract("Totally Unknown Caption", "pl") is None
