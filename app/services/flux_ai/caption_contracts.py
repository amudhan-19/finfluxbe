# @file    services/flux_ai/caption_contracts.py
#
# Caption-wise analysis contract registry for Flux AI commentary.
#
# Each fixed T-Flux caption has an accounting "contract" describing what it
# normally moves with, which drivers are permitted, how it should be
# decomposed, which drill-down dimensions apply, what counts as a recurring
# signature vs an anomaly, and what should trigger escalation. The registry
# is pure metadata (no DB, no calculations) and is injected into the AI
# prompt so commentary is caption-specific instead of generic.
#
# Plain ASCII only. No emojis. This module never invents GL accounts,
# customers, vendors, amounts or narration; it only constrains how the model
# is allowed to describe evidence that the backend already prepared.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Bumped whenever a contract's analytical meaning changes. Stored in the
# audit trail (not in a DB column) so a regenerated commentary can be traced
# back to the contract version that produced it.
CAPTION_CONTRACT_VERSION = "1.0"


@dataclass(frozen=True)
class CaptionContract:
    caption_id: str
    display_name: str
    statement: str            # "pl" | "bs"
    category: str
    normal_balance: str       # "debit" | "credit"
    normal_movement: str
    source_ledgers: tuple[str, ...]
    permitted_drivers: tuple[str, ...]
    drill_down_dimensions: tuple[str, ...]
    entry_level_attribution: str
    expected_variance_bands: str
    decomposition_rule: str
    recurring_signatures: tuple[str, ...]
    anomaly_triggers: tuple[str, ...]
    required_evidence: tuple[str, ...]
    materiality_guidance: str
    concise_template: str
    detailed_template: str
    escalation_triggers: tuple[str, ...]
    is_subtotal: bool = False
    balance_at_date: bool = False

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "caption_id": self.caption_id,
            "display_name": self.display_name,
            "statement": self.statement.upper(),
            "category": self.category,
            "normal_balance": self.normal_balance,
            "normal_movement": self.normal_movement,
            "is_subtotal": self.is_subtotal,
            "balance_at_date": self.balance_at_date,
            "source_ledgers": list(self.source_ledgers),
            "permitted_drivers": list(self.permitted_drivers),
            "drill_down_dimensions": list(self.drill_down_dimensions),
            "entry_level_attribution": self.entry_level_attribution,
            "expected_variance_bands": self.expected_variance_bands,
            "decomposition_rule": self.decomposition_rule,
            "recurring_signatures": list(self.recurring_signatures),
            "anomaly_triggers": list(self.anomaly_triggers),
            "required_evidence": list(self.required_evidence),
            "materiality_guidance": self.materiality_guidance,
            "escalation_triggers": list(self.escalation_triggers),
            "contract_version": CAPTION_CONTRACT_VERSION,
        }


# Registry is populated below in _register() calls.
_CONTRACTS: dict[str, CaptionContract] = {}
_ALIASES: dict[str, str] = {}


def _norm(name: str) -> str:
    """Normalize a caption label for alias lookup: lowercase, collapse
    internal whitespace, strip surrounding punctuation/space."""
    if not name:
        return ""
    out = " ".join(str(name).split()).strip().lower()
    return out.strip(" .:-")


def _register(contract: CaptionContract, aliases: tuple[str, ...]) -> None:
    _CONTRACTS[contract.caption_id] = contract
    _ALIASES[_norm(contract.display_name)] = contract.caption_id
    _ALIASES[_norm(contract.caption_id)] = contract.caption_id
    for alias in aliases:
        _ALIASES[_norm(alias)] = contract.caption_id


def resolve_caption_contract(
    caption: str, statement: str | None = None,
) -> CaptionContract | None:
    """Resolve a caption label (T-Flux core or alias) to its contract.

    Returns ``None`` for unmapped captions (e.g. subtotals without a
    dedicated contract) so callers fall back to generic behavior. The
    ``statement`` argument is advisory only; resolution is by name/alias so
    we never mis-handle a caption just because of a P&L/BS hint.
    """
    key = _norm(caption)
    if not key:
        return None
    cid = _ALIASES.get(key)
    if cid is None:
        return None
    contract = _CONTRACTS.get(cid)
    if contract is None:
        return None
    if statement and contract.statement and statement.lower() != contract.statement.lower():
        # Name matched but statement disagrees; still return it because the
        # caption name is authoritative in our fixed taxonomy.
        return contract
    return contract


def contract_prompt_block(contract: CaptionContract | None) -> dict[str, Any] | None:
    if contract is None:
        return None
    return contract.to_prompt_dict()


# ===========================================================================
# P&L caption contracts
# ===========================================================================

_register(
    CaptionContract(
        caption_id="revenue",
        display_name="Revenue",
        statement="pl",
        category="Revenue",
        normal_balance="credit",
        normal_movement="Increases as a credit; more negative raw balance means revenue increased.",
        source_ledgers=("Revenue/Sales GL", "AR sub-ledger", "Billing/Invoices"),
        permitted_drivers=(
            "volume", "price/rate", "product or geography mix", "FX",
            "new vs existing customer split", "one-time recognition items",
            "contract modification / milestone recognition",
        ),
        drill_down_dimensions=(
            "entity", "customer", "product/service line", "contract/invoice",
        ),
        entry_level_attribution="customer and invoice, only when transaction-linked",
        expected_variance_bands="MoM +/-15% absent a known seasonal pattern; QoQ +/-10%; YoY flag if outside +/-20% of the trailing trend. For subscription models also flag net revenue retention falling more than 5 points below the trailing-twelve-month average.",
        decomposition_rule="Decompose into volume + price/rate + mix + FX, each tied to GL or sub-ledger evidence; the components MUST sum to the computed variance within a rounding tolerance. If they cannot, abstain on the decomposition and raise a decomposition_gap flag rather than forcing a residual. Never attribute the movement to 'general growth'; if invoice/line evidence is absent, state decomposition is limited to mapped GL movement.",
        recurring_signatures=("recurring subscription/consulting billing", "steady invoice cadence", "annual price increases in renewal-cohort months", "end-of-quarter deal-closing surges"),
        anomaly_triggers=(
            "large final-days manual revenue journal with blank or generic narration",
            "a single customer, product, or entry driving more than 40% of the variance",
            "revenue movement opposite to volume movement (price/mix masking a volume decline)",
            "revenue growth with flat or declining cash collections",
            "credit memo or reversal materially reducing revenue",
        ),
        required_evidence=("mapped revenue GL movement", "top revenue transactions", "customer/invoice linkage where available"),
        materiality_guidance="Flag if movement breaches the configured P&L threshold or if a single customer/journal drives most of it.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Explain revenue movement via mapped revenue GL and top invoices; decompose into volume/price/mix only with evidence; never invent customers or products.",
        escalation_triggers=(
            "unexplained residual after decomposition exceeds materiality",
            "single-customer / single-entry concentration breach",
            "material revenue recognized via manual journal in the final days with weak narration",
            "revenue reversal/credit memo materially affecting the period",
            "suspected recognition-policy change",
        ),
    ),
    aliases=("Consulting Revenue", "Product Revenue", "Total Revenue", "Sales", "Net Revenue"),
)

_register(
    CaptionContract(
        caption_id="cost_of_revenue",
        display_name="Cost of Revenue / COGS",
        statement="pl",
        category="Cost of Revenue",
        normal_balance="debit",
        normal_movement="Increases as a debit; negative values represent net credit / negative cost adjustments.",
        source_ledgers=("COGS GL", "Inventory sub-ledger", "Purchase/Vendor ledger"),
        permitted_drivers=(
            "input cost (materials/labour/hosting)", "volume (revenue-driven)",
            "product mix", "standard/absorption cost variance",
            "freight/duty", "inventory adjustments", "inventory write-downs",
            "one-time cost adjustments",
        ),
        drill_down_dimensions=("entity", "product/SKU", "cost component", "vendor"),
        entry_level_attribution="vendor and SKU, only when transaction-linked",
        expected_variance_bands="Should track revenue within a few gross-margin points; express the band in gross-margin points, not COGS dollars. Flag if gross margin moves more than 3 points (compression or expansion) without a documented cause.",
        decomposition_rule="Express the movement BOTH in absolute terms AND as a change in gross-margin percentage. Decompose into volume (revenue-driven) + input-cost/rate + mix + one-time (e.g. write-down); the margin movement must be explained, not just the absolute COGS movement, and the components MUST sum to the total within tolerance. If revenue/margin data is unavailable, state the margin bridge is not fully supported and raise a decomposition_gap flag.",
        recurring_signatures=("regular purchase/inventory consumption postings", "standard-cost reset at fiscal-year start", "recurring hosting-cost allocations"),
        anomaly_triggers=(
            "gross margin moves more than 3 points",
            "COGS movement disconnected from revenue volume",
            "large write-down concentrated in one GL or one manual entry",
            "negative cost position reversing into positive cost",
            "any negative COGS balance",
        ),
        required_evidence=("mapped COGS/inventory GL movement", "top purchase/adjustment transactions"),
        materiality_guidance="Flag large inventory adjustments and any prior-period negative cost reversal.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Focus on inventory adjustment and purchase GL movement; explain negative cost positions in accounting-friendly language; do not invent vendors.",
        escalation_triggers=(
            "margin movement beyond band without documented cause",
            "suspected misclassification between COGS and OpEx",
            "material inventory write-down indicating obsolescence",
            "prior-period negative cost adjustment reversing in the current period",
        ),
    ),
    aliases=("Cost of Revenue", "COGS", "Cost of Goods Sold", "Cost of Sales"),
)

_register(
    CaptionContract(
        caption_id="gross_profit",
        display_name="Gross Profit",
        statement="pl",
        category="Subtotal",
        normal_balance="credit",
        normal_movement="Revenue less Cost of Revenue; moves with its components.",
        source_ledgers=("Derived from Revenue and Cost of Revenue",),
        permitted_drivers=("revenue movement", "cost of revenue movement", "margin mix"),
        drill_down_dimensions=("component captions only",),
        entry_level_attribution="none; explain through component captions",
        expected_variance_bands="Driven by component bands; flag if the gross-margin percentage moves outside +/-3 points.",
        decomposition_rule="Do NOT re-explain Revenue and COGS drivers independently (that double-counts). State the net margin story in one or two sentences and point to the Revenue and Cost of Revenue commentary.",
        recurring_signatures=("stable gross margin percentage", "mirrors revenue seasonality net of cost behaviour"),
        anomaly_triggers=("gross-margin trend break", "gross profit moving opposite to revenue"),
        required_evidence=("Revenue movement", "Cost of Revenue movement"),
        materiality_guidance="Assess at the margin level; defer transaction detail to component captions.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven by movement in Revenue and Cost of Revenue; {materiality_note}",
        detailed_template="Subtotal caption: reference Revenue and Cost of Revenue drivers; no direct transaction drill-down.",
        escalation_triggers=("unexplained margin swing after reviewing both components",),
        is_subtotal=True,
    ),
    aliases=("Gross Profit", "Gross Margin"),
)

_register(
    CaptionContract(
        caption_id="salaries_wages",
        display_name="Salaries & Wages / Personnel",
        statement="pl",
        category="Operating Expense",
        normal_balance="debit",
        normal_movement="Increases as a debit; largely recurring with periodic accrual adjustments.",
        source_ledgers=("Payroll GL", "Accrual journals"),
        permitted_drivers=(
            "headcount change", "merit/wage inflation", "bonus-accrual timing",
            "commission", "severance", "stock-based compensation",
            "payroll tax/benefits", "capitalized-labour reclassification",
        ),
        drill_down_dimensions=("entity", "cost centre/department", "employee cohort", "pay component"),
        entry_level_attribution="department or cohort and pay component only; the poster of a journal may be named for audit, but never name individual employees or disclose individual pay",
        expected_variance_bands="MoM +/-5% absent a hiring wave or a bonus month; bonus-accrual months are recurring, not anomalous.",
        decomposition_rule="Decompose into headcount effect (change in heads x average fully-loaded cost) + rate effect (wage inflation) + bonus/timing + one-time (severance); the headcount effect must tie to the HRIS sub-ledger where available and the components must sum to the total. If HRIS/headcount data is missing, state headcount decomposition is unavailable. Never attribute to named individuals.",
        recurring_signatures=("regular monthly payroll run", "annual merit-increase month", "quarterly or annual bonus accrual"),
        anomaly_triggers=(
            "cost rising while headcount is flat or falling",
            "a bonus accrual outside its normal month",
            "a capitalised-labour swing without a project reference",
            "a manual payroll journal outside the normal payroll-interface run",
            "severance not previously communicated",
        ),
        required_evidence=("mapped payroll GL movement", "accrual journal entries where present"),
        materiality_guidance="Flag large bonus/commission accruals and any payroll accrual true-up.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Never name individuals; use department/cohort only if in evidence, otherwise GL-level movement only.",
        escalation_triggers=("material one-time bonus/commission accrual", "payroll accrual true-up of material size"),
    ),
    aliases=("Salaries & Wages", "Salaries and Wages", "Personnel", "Payroll", "Compensation"),
)

_register(
    CaptionContract(
        caption_id="software_technology",
        display_name="Software & Technology / SaaS",
        statement="pl",
        category="Operating Expense",
        normal_balance="debit",
        normal_movement="Increases as a debit; subscription and consumption based.",
        source_ledgers=("Software/IT expense GL", "Vendor/AP ledger"),
        permitted_drivers=(
            "new tool adoption", "seat/user growth", "renewal price increases",
            "vendor-consolidation savings", "prepaid-amortisation timing",
            "cloud consumption", "capitalized internal-use software reclassification",
        ),
        drill_down_dimensions=("entity", "vendor", "contract/tool"),
        entry_level_attribution="vendor and contract, only when transaction-linked; highlight any manual journal among otherwise system-fed AP postings",
        expected_variance_bands="MoM +/-10%; renewal months may step up. Flag a sustained step-up greater than 25% that recurs without a contract or new-tool reference.",
        decomposition_rule="Decompose into new-vendor/new-tool + seat/volume + renewal-price + amortisation-timing, each tied to a vendor or contract in the AP sub-ledger and summing to the total; if vendor/contract data is missing, limit to mapped GL movement.",
        recurring_signatures=("recurring monthly/annual SaaS charges", "annual renewal step-ups for major platforms", "seat growth tracking headcount"),
        anomaly_triggers=(
            "a step-up greater than 25% without a renewal or new-tool reference",
            "double-billing (two invoices, same vendor, same period)",
            "a tool that should be capitalised hitting expense",
        ),
        required_evidence=("mapped software/IT GL movement", "top vendor transactions where available"),
        materiality_guidance="Flag large new subscriptions, annual renewals and capitalization reclasses.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Drill down to vendor/contract if available; otherwise GL-level movement only.",
        escalation_triggers=("material capitalization vs expense reclassification", "large unbudgeted new subscription"),
    ),
    aliases=("Software & Technology", "Software and Technology", "SaaS", "Software", "Technology"),
)

_register(
    CaptionContract(
        caption_id="professional_services",
        display_name="Professional Services / Consulting",
        statement="pl",
        category="Operating Expense",
        normal_balance="debit",
        normal_movement="Increases as a debit; project and engagement driven.",
        source_ledgers=("Professional services expense GL", "Vendor/AP ledger"),
        permitted_drivers=(
            "project starts/stops", "audit and tax fee cycles", "legal matters",
            "one-time advisory", "M&A / restructuring engagements",
            "recurring retainers",
        ),
        drill_down_dimensions=("entity", "vendor", "engagement/project (PO/SOW)"),
        entry_level_attribution="vendor and project, only when transaction-linked; a material entry without a PO/SOW reference is flagged",
        expected_variance_bands="Inherently variable; band the recurring component tightly (flag a recurring-component move greater than 15%) and treat the project component as expected variability.",
        decomposition_rule="Separate recurring professional fees from project/one-time engagements; explain each material project individually with its PO/SOW and aggregate the immaterial tail. Where the split cannot be supported, flag a decomposition_gap.",
        recurring_signatures=("recurring legal/audit retainers", "audit fees around year-end and quarter-ends", "annual tax-compliance fees"),
        anomaly_triggers=(
            "a single engagement driving most of the variance without a PO/SOW reference",
            "recurring fees moving without a rate change",
        ),
        required_evidence=("mapped professional services GL movement", "top vendor transactions where available"),
        materiality_guidance="Flag large one-time advisory, legal, audit or tax engagements.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Drill down to vendor/project if available; otherwise GL-level movement only.",
        escalation_triggers=("material one-time advisory/legal engagement",),
    ),
    aliases=("Professional Services", "Consulting", "Legal & Audit", "Advisory"),
)

_register(
    CaptionContract(
        caption_id="depreciation_amortization",
        display_name="Depreciation & Amortization",
        statement="pl",
        category="Operating Expense",
        normal_balance="debit",
        normal_movement="Increases as a debit; steady depreciation is recurring, not an anomaly.",
        source_ledgers=("Depreciation/Amortization GL", "Fixed asset register"),
        permitted_drivers=(
            "asset additions (pro-rata to in-service date)", "disposals",
            "fully-depreciated assets rolling off", "useful-life changes",
            "impairment", "ROU lease-asset schedule movement",
        ),
        drill_down_dimensions=("entity", "asset class", "individual asset"),
        entry_level_attribution="asset class / fixed asset register where available; highlight manual life-change or impairment entries",
        expected_variance_bands="Should move smoothly; flag any movement greater than 10% as it implies a register event that must be identified.",
        decomposition_rule="Tie the entire movement to register events (additions, disposals, roll-offs, life changes, impairment); it should reconcile to the FAR/intangibles roll-forward to the dollar, and any residual is flagged. Steady depreciation is expected, not an anomaly.",
        recurring_signatures=("steady monthly depreciation charge", "predictable roll-offs as cohorts fully depreciate"),
        anomaly_triggers=(
            "movement not explained by a register event",
            "depreciation continuing on a disposed asset",
            "sudden acceleration suggesting an unrecorded impairment",
            "a manual D&A journal outside the register run",
        ),
        required_evidence=("mapped depreciation GL movement", "fixed asset register where available"),
        materiality_guidance="Treat steady depreciation as recurring; flag impairment or method changes.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Use fixed asset register if available; do not flag steady depreciation as an anomaly.",
        escalation_triggers=("impairment charge", "depreciation method or useful-life change"),
    ),
    aliases=("Depreciation", "Amortization", "Depreciation and Amortization", "D&A"),
)

_register(
    CaptionContract(
        caption_id="operating_income",
        display_name="Operating Income / EBIT",
        statement="pl",
        category="Subtotal",
        normal_balance="credit",
        normal_movement="Gross Profit less operating expenses; moves with its components.",
        source_ledgers=("Derived from Gross Profit and operating expense captions",),
        permitted_drivers=("revenue", "cost of revenue", "operating expense movement"),
        drill_down_dimensions=("component captions only",),
        entry_level_attribution="none; explain through component captions",
        expected_variance_bands="Component-driven; flag if operating margin moves outside +/-3 points.",
        decomposition_rule="Tell the operating-leverage story (did revenue outpace cost growth, and which cost lines moved); reference the component captions and avoid double-counting. Do not invent transaction-level drivers.",
        recurring_signatures=("stable operating margin", "improving margin as the business scales"),
        anomaly_triggers=("operating-margin trend break", "operating income moving opposite to revenue"),
        required_evidence=("component caption movements",),
        materiality_guidance="Assess at the operating-margin level; defer detail to components.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven by component captions; {materiality_note}",
        detailed_template="Subtotal caption: explain through component captions; no transaction drill-down.",
        escalation_triggers=("unexplained operating margin swing",),
        is_subtotal=True,
    ),
    aliases=("Operating Income", "EBIT", "Operating Profit"),
)

_register(
    CaptionContract(
        caption_id="interest_expense",
        display_name="Interest Expense",
        statement="pl",
        category="Non-Operating",
        normal_balance="debit",
        normal_movement="Increases as a debit; tracks debt balance and rate.",
        source_ledgers=("Interest expense GL", "Debt schedule"),
        permitted_drivers=(
            "principal-balance change", "rate change (especially floating-rate)",
            "new borrowings or paydowns", "debt-issuance-cost amortisation",
            "capitalised interest", "FX on foreign-currency debt",
        ),
        drill_down_dimensions=("entity", "debt instrument"),
        entry_level_attribution="debt instrument where a debt schedule is available; a manual interest journal outside the schedule-driven accrual is highlighted",
        expected_variance_bands="Should track the debt schedule; flag any movement not explained by a balance or rate change.",
        decomposition_rule="Decompose into balance effect (change in principal x rate) + rate effect (change in rate x average balance) + new-instrument effect + debt-issuance-cost amortisation; tie each to the debt schedule and sum to the total. Otherwise flag a decomposition_gap.",
        recurring_signatures=("regular periodic interest accrual", "gradual decline as debt amortises", "reset on floating-rate adjustment dates"),
        anomaly_triggers=(
            "interest moving without a balance or rate change",
            "a capitalised-interest swing",
            "a missed accrual",
            "a manual interest journal off-schedule",
        ),
        required_evidence=("mapped interest GL movement", "debt schedule where available"),
        materiality_guidance="Flag refinancing, rate resets and FX revaluation.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Tie to debt schedule if available; explain rate vs balance effects.",
        escalation_triggers=("refinancing event", "material FX revaluation on debt"),
    ),
    aliases=("Interest Expense", "Interest"),
)

_register(
    CaptionContract(
        caption_id="income_tax",
        display_name="Income Tax Expense",
        statement="pl",
        category="Tax",
        normal_balance="debit",
        normal_movement="Increases as a debit; tracks pretax income and effective tax rate.",
        source_ledgers=("Income tax GL", "Deferred tax schedule"),
        permitted_drivers=(
            "pre-tax-income change", "effective-tax-rate change",
            "jurisdiction mix", "permanent differences",
            "valuation-allowance movement", "deferred tax",
            "discrete items", "return-to-provision true-ups",
        ),
        drill_down_dimensions=("entity", "jurisdiction", "tax component (current/deferred/discrete)"),
        entry_level_attribution="jurisdiction and current/deferred/discrete component where available; discrete items are usually manual journals",
        expected_variance_bands="Express as an ETR; flag if the ETR moves outside +/-3 points without a documented discrete item.",
        decomposition_rule="Separate the pre-tax-income effect (change in income x prior ETR) from the rate effect (change in ETR x current income); explain ETR movements with specific drivers, never a bare 'rate change'. Components should sum to the total; otherwise flag a decomposition_gap.",
        recurring_signatures=("tax accrual at a stable effective rate", "predictable year-end return-to-provision true-ups"),
        anomaly_triggers=(
            "an ETR swing without a discrete-item explanation",
            "tax moving opposite to pre-tax income",
            "a negative ETR",
        ),
        required_evidence=("mapped tax GL movement", "ETR context where available"),
        materiality_guidance="Flag discrete items, true-ups and material ETR changes.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Explain ETR movement if available; split current vs deferred and discrete items.",
        escalation_triggers=("material discrete tax item", "prior-year tax true-up"),
    ),
    aliases=("Income Tax", "Income Tax Expense", "Tax Expense", "Provision for Income Taxes"),
)

_register(
    CaptionContract(
        caption_id="net_income",
        display_name="Net Income",
        statement="pl",
        category="Subtotal",
        normal_balance="credit",
        normal_movement="Bottom-line subtotal; moves with all P&L components.",
        source_ledgers=("Derived from all P&L captions",),
        permitted_drivers=("operating income", "interest expense", "income tax", "below-the-line / one-off items"),
        drill_down_dimensions=("component captions only",),
        entry_level_attribution="none; the bridge references entries surfaced under the component captions",
        expected_variance_bands="Component-driven; flag if the net-margin trend breaks.",
        decomposition_rule="Provide a top-to-bottom bridge: revenue movement, major caption contributions, ending at net income; each step references its caption and no new drivers are introduced here.",
        recurring_signatures=("net margin consistent with operating performance", "tracks operating income net of stable below-the-line items"),
        anomaly_triggers=("net income moving opposite to operating income (a below-the-line surprise)", "net-margin trend break"),
        required_evidence=("component caption movements",),
        materiality_guidance="Assess at the net-margin level; defer detail to components.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven by component captions; {materiality_note}",
        detailed_template="Subtotal caption: explain through component captions; no transaction drill-down.",
        escalation_triggers=("unexplained net income swing",),
        is_subtotal=True,
    ),
    aliases=("Net Income", "Net Profit", "Net Earnings"),
)


# ===========================================================================
# Balance Sheet caption contracts (Balance at Date)
# ===========================================================================

_register(
    CaptionContract(
        caption_id="cash",
        display_name="Cash & Cash Equivalents",
        statement="bs",
        category="Current Asset",
        normal_balance="debit",
        normal_movement="Balance at date; debit-normal asset.",
        source_ledgers=("Cash/Bank GL", "Bank statement", "Cash sub-ledger"),
        permitted_drivers=(
            "operating cash generation", "receipts and disbursements timing",
            "debt draws and repayments", "capital expenditure", "dividends",
            "FX on foreign-currency cash", "bank transfers",
        ),
        drill_down_dimensions=("entity", "bank account", "cash-flow category (operating/investing/financing)"),
        entry_level_attribution="bank account and source document where available; especially large manual transfers and disbursements",
        expected_variance_bands="Inherently volatile; band against the cash-flow categories rather than a fixed percentage and flag a movement the cash-flow categories do not explain.",
        decomposition_rule="Tie the cash movement to the cash-flow categories (operating, investing, financing, FX); the four MUST sum to the total cash movement. Do not explain cash in isolation from the cash-flow statement; if the categories are unavailable, state cash-flow category decomposition is unavailable and use bank GL / document evidence.",
        recurring_signatures=("regular collection and disbursement cycles", "seasonal working-capital swings", "predictable debt-service and payroll cycles"),
        anomaly_triggers=(
            "cash movement inconsistent with reported earnings",
            "unexplained transfers",
            "an FX effect disproportionate to the foreign-currency holdings",
            "a large manual transfer with a weak narration",
        ),
        required_evidence=("mapped cash GL balance movement", "bank reconciliation where available"),
        materiality_guidance="Use Balance at Date; recommend bank reconciliation for material entries.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Use Balance at Date; tie movement to collections, payments, payroll, financing, FX and transfers; reconcile to bank where available.",
        escalation_triggers=("material unreconciled cash movement",),
        balance_at_date=True,
    ),
    aliases=("Cash", "Cash & Cash Equivalents", "Cash and Cash Equivalents", "Bank"),
)

_register(
    CaptionContract(
        caption_id="accounts_receivable",
        display_name="Accounts Receivable",
        statement="bs",
        category="Current Asset",
        normal_balance="debit",
        normal_movement="Balance at date; debit-normal asset.",
        source_ledgers=("AR GL", "AR sub-ledger", "Invoices/Credit memos"),
        permitted_drivers=(
            "revenue change", "collection speed (DSO)", "customer mix",
            "disputed balances", "allowance and write-off movements",
            "FX on foreign receivables",
        ),
        drill_down_dimensions=("entity", "customer", "invoice", "aging bucket"),
        entry_level_attribution="customer and invoice, only when transaction-linked; show the invoice date that drives the aging and read write-off narration",
        expected_variance_bands="Express as DSO; flag if DSO moves outside +/-5 days, or if AR growth materially outpaces revenue growth.",
        decomposition_rule="Decompose into revenue effect + collection effect (change in DSO) + allowance effect + FX, summing to the total; express the collection story in days, then localise to the customers and invoices responsible. If aging/DSO data is unavailable, state the limitation.",
        recurring_signatures=("regular billing and collection cycle", "quarter-end billing surges", "large-customer payment cycles"),
        anomaly_triggers=(
            "AR growing faster than revenue",
            "a DSO trend break",
            "a single customer dominating the increase",
            "the aging shifting to older buckets",
            "a write-off with a weak narration",
        ),
        required_evidence=("mapped AR GL balance movement", "aging/customer linkage where available"),
        materiality_guidance="Use Balance at Date; customer attribution only if transaction-linked. Flag DSO trend breaks even when the dollar move is modest.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}; DSO moved from {prior_dso} to {current_dso} days, concentrated in {dominant_driver}; {materiality_note}",
        detailed_template="Use Balance at Date; explain via revenue change, collection speed (DSO in days) and aging shift; customer attribution only if transaction-linked.",
        escalation_triggers=(
            "DSO trend break",
            "aging deterioration",
            "customer concentration",
            "a write-off with a weak narration",
        ),
        balance_at_date=True,
    ),
    aliases=("Accounts Receivable", "AR", "Trade Receivables", "Receivables"),
)

_register(
    CaptionContract(
        caption_id="inventory",
        display_name="Inventory",
        statement="bs",
        category="Current Asset",
        normal_balance="debit",
        normal_movement="Balance at date; debit-normal asset.",
        source_ledgers=("Inventory GL", "Inventory sub-ledger", "Purchase/Adjustment journals"),
        permitted_drivers=(
            "purchase volume", "sales/consumption volume", "production timing",
            "write-downs and reserves", "standard-cost changes",
            "in-transit timing",
        ),
        drill_down_dimensions=("entity", "warehouse/location", "SKU/item"),
        entry_level_attribution="warehouse/SKU where available; write-downs, count adjustments and standard-cost resets are usually manual and material",
        expected_variance_bands="Express as DIO; flag if DIO moves outside +/-7 days, or if inventory builds while sales decline.",
        decomposition_rule="Decompose into purchase effect + consumption/sales effect + write-down effect + cost-revaluation effect, summing to the total; express the turnover change in days, then localise to warehouse and SKU.",
        recurring_signatures=("regular purchase and consumption postings", "seasonal pre-build ahead of peak selling periods"),
        anomaly_triggers=(
            "inventory building while sales fall (obsolescence risk)",
            "a DIO trend break",
            "a write-down concentrated in one period or one manual entry",
            "any negative inventory balance",
        ),
        required_evidence=("mapped inventory GL balance movement", "adjustment journals where available"),
        materiality_guidance="Use Balance at Date; flag write-downs and revaluations and turnover deterioration.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}; DIO moved from {prior_dio} to {current_dio} days, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Use Balance at Date; explain via purchases, consumption, write-downs and cost revaluation; express DIO in days if available.",
        escalation_triggers=("material inventory write-down", "standard cost revaluation"),
        balance_at_date=True,
    ),
    aliases=("Inventory", "Stock", "Inventories"),
)

_register(
    CaptionContract(
        caption_id="prepaid_expenses",
        display_name="Prepaid Expenses & Other Current Assets",
        statement="bs",
        category="Current Asset",
        normal_balance="debit",
        normal_movement="Balance at date; debit-normal asset.",
        source_ledgers=("Prepaid/Other current asset GL", "Amortization schedules"),
        permitted_drivers=(
            "new prepayments (insurance/software/rent)", "amortisation run-off",
            "reclassification to expense", "reclass of the long-term portion",
        ),
        drill_down_dimensions=("entity", "prepaid contract/vendor"),
        entry_level_attribution="prepayment schedule where available; vendor/contract reference in the narration identifies the prepayment",
        expected_variance_bands="Smooth; flag a step-change that does not tie to a new prepayment or an amortisation event.",
        decomposition_rule="Tie the movement to the prepaid schedule: additions less amortisation +/- reclass; identify the largest new prepayment and the amortisation run-off.",
        recurring_signatures=("regular amortisation release", "annual insurance and software renewals creating build-and-amortise cycles"),
        anomaly_triggers=("the balance growing without new prepayments", "stale prepaids not amortising", "misclassified items"),
        required_evidence=("mapped prepaid GL balance movement", "amortization schedule where available"),
        materiality_guidance="Use Balance at Date; flag stale or non-amortizing balances.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Use Balance at Date; explain via new prepayments, amortization release and reclassification.",
        escalation_triggers=("stale prepaid balance not amortizing",),
        balance_at_date=True,
    ),
    aliases=("Prepaid Expenses", "Prepaid Expenses & Other Current Assets", "Prepaids", "Other Current Assets"),
)

_register(
    CaptionContract(
        caption_id="ppe",
        display_name="Property, Plant & Equipment",
        statement="bs",
        category="Non-Current Asset",
        normal_balance="debit",
        normal_movement="Balance at date; debit-normal asset with roll-forward.",
        source_ledgers=("PP&E GL", "Fixed asset register"),
        permitted_drivers=(
            "capital additions", "depreciation", "disposals", "impairments",
            "transfers from CWIP", "FX on foreign assets",
        ),
        drill_down_dimensions=("entity", "asset class", "individual asset"),
        entry_level_attribution="asset class / fixed asset register where available; additions reference a PO/project and disposals reference the asset and proceeds",
        expected_variance_bands="The movement should reconcile to the FAR roll-forward; flag any movement not explained by additions, depreciation, or disposals.",
        decomposition_rule="Provide the PP&E roll-forward: opening + additions - depreciation - disposals +/- impairment +/- FX = closing; each component tied to the register and any residual flagged.",
        recurring_signatures=("steady depreciation reduction", "capex concentrated around project milestones"),
        anomaly_triggers=(
            "net PP&E rising while capex is flat",
            "a disposal without a gain/loss entry",
            "an impairment indicator left unaddressed",
            "the roll-forward not tying",
        ),
        required_evidence=("mapped PP&E GL balance movement", "fixed asset register where available"),
        materiality_guidance="Use Balance at Date and roll-forward; flag impairment and large additions/disposals.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Use Balance at Date; present the additions/depreciation/disposals roll-forward; tie to fixed asset register if available.",
        escalation_triggers=("impairment charge", "material asset disposal"),
        balance_at_date=True,
    ),
    aliases=("PP&E", "Fixed Assets / PP&E", "Property, Plant & Equipment", "Property Plant and Equipment", "Fixed Assets"),
)

_register(
    CaptionContract(
        caption_id="intangibles",
        display_name="Intangibles & Goodwill",
        statement="bs",
        category="Non-Current Asset",
        normal_balance="debit",
        normal_movement="Balance at date; debit-normal asset.",
        source_ledgers=("Intangibles/Goodwill GL", "Amortization/Impairment schedules"),
        permitted_drivers=(
            "acquisitions (additions)", "amortisation of finite-lived intangibles",
            "impairment", "capitalised development costs",
        ),
        drill_down_dimensions=("entity", "asset / cash-generating unit"),
        entry_level_attribution="intangible class / CGU / asset register where available; acquisition and impairment entries are manual and material",
        expected_variance_bands="Goodwill should be flat absent an acquisition or impairment (flag any movement); finite-lived intangibles should amortise smoothly.",
        decomposition_rule="Separate goodwill (static; any movement is an acquisition or impairment event) from finite-lived intangibles (smooth amortisation); tie each to the register.",
        recurring_signatures=("steady amortisation of finite-life intangibles", "goodwill flat between deals"),
        anomaly_triggers=("any goodwill movement", "intangible amortisation pausing", "impairment indicators left unaddressed"),
        required_evidence=("mapped intangibles GL balance movement", "schedules where available"),
        materiality_guidance="Use Balance at Date; flag impairment and acquisitions.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Use Balance at Date; explain via additions, amortization and impairment.",
        escalation_triggers=("goodwill or intangible impairment", "material acquisition"),
        balance_at_date=True,
    ),
    aliases=("Intangibles", "Intangibles & Goodwill", "Goodwill", "Intangible Assets"),
)

_register(
    CaptionContract(
        caption_id="accounts_payable",
        display_name="Accounts Payable",
        statement="bs",
        category="Current Liability",
        normal_balance="credit",
        normal_movement="Balance at date; credit-normal liability.",
        source_ledgers=("AP GL", "AP sub-ledger", "Bills/Payments"),
        permitted_drivers=(
            "purchase volume", "payment timing (DPO)", "vendor mix",
            "FX on foreign payables", "accrual-to-AP reclassifications",
        ),
        drill_down_dimensions=("entity", "vendor", "invoice"),
        entry_level_attribution="vendor and invoice, only when transaction-linked; the invoice date drives the aging and the PO reference identifies the purchase",
        expected_variance_bands="Express as DPO; flag if DPO moves outside +/-5 days, or if AP diverges materially from purchasing volume. A DPO extension may be deliberate working-capital management - note it rather than flag it.",
        decomposition_rule="Decompose into purchasing effect + payment-timing effect (change in DPO) + FX, summing to the total; express the timing story in days, then localise to vendors. If DPO/vendor data is unavailable, state the limitation.",
        recurring_signatures=("regular bill and payment cycle", "month-end payment runs", "predictable vendor-term cycles"),
        anomaly_triggers=(
            "AP rising while purchasing falls",
            "a DPO trend break",
            "debit balances in AP",
            "a large manual AP journal outside the invoice interface",
        ),
        required_evidence=("mapped AP GL balance movement", "vendor linkage where available"),
        materiality_guidance="Use Balance at Date; vendor attribution only if transaction-linked. A DPO extension can signal liquidity stress or deliberate strategy; context determines which.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}; DPO moved from {prior_dpo} to {current_dpo} days, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Use Balance at Date; explain via purchasing effect, payment timing/DPO in days, vendor mix and FX; vendor attribution only if transaction-linked.",
        escalation_triggers=("material DPO stretch", "large accrual-to-AP reclass"),
        balance_at_date=True,
    ),
    aliases=("Accounts Payable", "AP", "Trade Payables", "Payables"),
)

_register(
    CaptionContract(
        caption_id="accrued_liabilities",
        display_name="Accrued Liabilities",
        statement="bs",
        category="Current Liability",
        normal_balance="credit",
        normal_movement="Balance at date; credit-normal liability.",
        source_ledgers=("Accruals GL", "Accrual journals"),
        permitted_drivers=(
            "bonus-accrual build and release", "tax accruals",
            "utility and operating accruals",
            "reversal of prior-period accruals against actuals", "true-up",
        ),
        drill_down_dimensions=("entity", "accrual type (comp/tax/operating)", "schedule line"),
        entry_level_attribution="accrual type where available; accruals are manual, so confirm a reversal ties to a prior accrual and flag generic narration",
        expected_variance_bands="Cyclical; band against the prior-year same-period pattern. Bonus-accrual months are recurring, not anomalous.",
        decomposition_rule="Separate each major accrual type and explain its build or release against the schedule; a reversal against actuals is normal, not an anomaly. Flag stale or generic accruals.",
        recurring_signatures=("regular accrual build and reversal", "bonus accrual building through the year and releasing at payout", "quarterly tax accruals"),
        anomaly_triggers=(
            "an accrual building without an obvious obligation",
            "stale accruals not releasing",
            "a large unexplained true-up",
            "an accrual journal with a generic narration",
        ),
        required_evidence=("mapped accruals GL balance movement", "accrual journals where available"),
        materiality_guidance="Use Balance at Date; flag stale or generic accruals.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Use Balance at Date; explain via bonus/tax/operating accruals and reversals/true-ups; flag stale or generic accruals.",
        escalation_triggers=("stale accrual not reversing", "material accrual true-up"),
        balance_at_date=True,
    ),
    aliases=("Accrued Liabilities", "Accruals", "Accrued Expenses"),
)

_register(
    CaptionContract(
        caption_id="debt",
        display_name="Debt",
        statement="bs",
        category="Liability",
        normal_balance="credit",
        normal_movement="Balance at date; credit-normal liability with roll-forward.",
        source_ledgers=("Debt/Loan GL", "Debt schedule"),
        permitted_drivers=(
            "new borrowings", "scheduled and voluntary repayments",
            "current-portion reclassification", "debt-issuance-cost amortisation",
            "FX on foreign-currency debt",
        ),
        drill_down_dimensions=("entity", "debt instrument"),
        entry_level_attribution="debt instrument where a debt schedule is available; confirm the instrument and whether the movement is scheduled vs voluntary",
        expected_variance_bands="Should reconcile to the debt schedule exactly; flag any movement not in the schedule. Covenant proximity is a key flag.",
        decomposition_rule="Provide the debt roll-forward: opening + draws - repayments +/- reclassification +/- FX = closing; tie to the schedule and note any covenant-relevant change.",
        recurring_signatures=("scheduled amortisation", "predictable revolver draws and paydowns"),
        anomaly_triggers=(
            "debt movement not in the schedule",
            "proximity to a covenant threshold",
            "a missed current-portion reclassification",
        ),
        required_evidence=("mapped debt GL balance movement", "debt schedule where available"),
        materiality_guidance="Use Balance at Date and roll-forward; flag draws, refinancing and FX.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Use Balance at Date; present the draws/repayments/reclass/FX roll-forward; tie to debt schedule if available.",
        escalation_triggers=("refinancing event", "material new draw"),
        balance_at_date=True,
    ),
    aliases=("Long-Term Debt", "Debt", "Borrowings", "Loans", "Total Liabilities"),
)

_register(
    CaptionContract(
        caption_id="equity",
        display_name="Equity",
        statement="bs",
        category="Equity",
        normal_balance="credit",
        normal_movement="Balance at date; credit-normal with roll-forward.",
        source_ledgers=("Equity GL", "Retained earnings", "Share capital"),
        permitted_drivers=(
            "net income (to retained earnings)", "dividends",
            "share issuance and buyback", "stock-based compensation",
            "OCI movements (FX translation, AFS, pension, hedges)",
        ),
        drill_down_dimensions=("entity", "equity component (retained earnings/share capital/OCI/treasury)"),
        entry_level_attribution="equity component where available; share activity references a board resolution - confirm authorisation from the narration",
        expected_variance_bands="Should reconcile to the statement of changes in equity; flag any movement not explained by a known component.",
        decomposition_rule="Provide the equity roll-forward: opening + net income - dividends +/- share activity +/- SBC +/- OCI = closing; each component tied to its source and net income references the Net Income caption.",
        recurring_signatures=("retained earnings movement matching net income", "predictable dividend cycles", "routine stock-based compensation"),
        anomaly_triggers=(
            "an equity movement not explained by the roll-forward",
            "an unexpected OCI swing",
            "share activity without a board reference in the narration",
        ),
        required_evidence=("mapped equity GL balance movement", "Net Income component where available"),
        materiality_guidance="Use Balance at Date and roll-forward; reconcile retained earnings to Net Income.",
        concise_template="{caption} {direction} by {amount} ({pct}) versus {comparison}, driven mainly by {dominant_driver}; {materiality_note}",
        detailed_template="Use Balance at Date; present the net income/dividends/share/OCI roll-forward; reference Net Income component where applicable.",
        escalation_triggers=("unexpected equity movement not explained by net income or distributions",),
        balance_at_date=True,
    ),
    aliases=("Equity", "Total Equity", "Shareholders Equity", "Stockholders Equity"),
)
