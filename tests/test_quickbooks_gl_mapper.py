# @file    tests/test_quickbooks_gl_mapper.py
#
# Unit tests for QuickBooks General Ledger report normalization.

from __future__ import annotations

from app.services.erp.quickbooks_mapper import normalize_general_ledger_report

SAMPLE_NESTED_GL_REPORT = {
    "Header": {
        "ReportName": "GeneralLedger",
        "StartPeriod": "2026-01-01",
        "EndPeriod": "2026-12-31",
        "ReportBasis": "Accrual",
    },
    "Columns": {
        "Column": [
            {"ColTitle": "Date", "ColType": "Date", "MetaData": [{"Name": "ColKey", "Value": "tx_date"}]},
            {"ColTitle": "Transaction Type", "ColType": "String", "MetaData": [{"Name": "ColKey", "Value": "txn_type"}]},
            {"ColTitle": "Num", "ColType": "String", "MetaData": [{"Name": "ColKey", "Value": "doc_num"}]},
            {"ColTitle": "Name", "ColType": "String", "MetaData": [{"Name": "ColKey", "Value": "name"}]},
            {"ColTitle": "Memo/Description", "ColType": "String", "MetaData": [{"Name": "ColKey", "Value": "memo"}]},
            {"ColTitle": "Split", "ColType": "String", "MetaData": [{"Name": "ColKey", "Value": "split_acc"}]},
            {"ColTitle": "Amount", "ColType": "Money", "MetaData": [{"Name": "ColKey", "Value": "subt_nat_amount"}]},
            {"ColTitle": "Balance", "ColType": "Money", "MetaData": [{"Name": "ColKey", "Value": "rbal_nat_amount"}]},
        ]
    },
    "Rows": {
        "Row": [
            {
                "type": "Section",
                "Header": {"ColData": [{"value": "1000 Checking"}]},
                "Rows": {
                    "Row": [
                        {
                            "type": "Data",
                            "ColData": [
                                {"value": ""},
                                {"value": "Beginning Balance"},
                                {"value": ""},
                                {"value": ""},
                                {"value": ""},
                                {"value": ""},
                                {"value": ""},
                                {"value": "0.00"},
                            ],
                        },
                        {
                            "type": "Data",
                            "ColData": [
                                {"value": "2026-03-15"},
                                {"value": "Deposit", "id": "123"},
                                {"value": "1001"},
                                {"value": "Customer A", "id": "5"},
                                {"value": "Payment received"},
                                {"value": "Income"},
                                {"value": "500.00"},
                                {"value": "500.00"},
                            ],
                        },
                    ]
                },
                "Summary": {
                    "ColData": [
                        {"value": "Total for 1000 Checking"},
                        {"value": ""},
                        {"value": ""},
                        {"value": ""},
                        {"value": ""},
                        {"value": ""},
                        {"value": "500.00"},
                        {"value": "500.00"},
                    ]
                },
            }
        ]
    },
}


def test_nested_general_ledger_report_parses_transaction_rows():
    entries, diag = normalize_general_ledger_report(SAMPLE_NESTED_GL_REPORT, realm_id="realm-1")

    assert diag["top_level_row_count"] == 1
    assert diag["nested_row_count"] == 2
    assert diag["data_row_count"] == 2
    assert diag["records_processed"] == 1
    assert len(entries) == 1

    row = entries[0]
    assert row["account_no"] == "1000"
    assert str(row["posting_date"]) == "2026-03-15"
    assert row["document_no"] == "1001"
    assert row["amount"] == 500
    assert row["debit_amount"] == 500
    assert row["description"] == "Payment received"
    assert row["raw_source_json"]["transaction_id"] == "123"
    assert row["raw_source_json"]["name"] == "Customer A"


def test_debit_credit_columns_parsed():
    report = {
        "Columns": {
            "Column": [
                {"ColTitle": "Transaction date"},
                {"ColTitle": "Distribution account"},
                {"ColTitle": "Description"},
                {"ColTitle": "Debit"},
                {"ColTitle": "Credit"},
            ]
        },
        "Rows": {
            "Row": [
                {
                    "type": "Section",
                    "Header": {"ColData": [{"value": "2000 Accounts Payable"}]},
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {"value": "2026-06-01"},
                                    {"value": "2000 Accounts Payable"},
                                    {"value": "Vendor bill"},
                                    {"value": "150.00"},
                                    {"value": ""},
                                ],
                            }
                        ]
                    },
                }
            ]
        },
    }

    entries, diag = normalize_general_ledger_report(report, realm_id="realm-2")
    assert len(entries) == 1
    assert entries[0]["debit_amount"] == 150
    assert entries[0]["credit_amount"] is None
    assert entries[0]["amount"] == 150
    assert diag["records_processed"] == 1


def test_empty_report_returns_zero_entries():
    report = {"Columns": {"Column": []}, "Rows": {"Row": []}}
    entries, diag = normalize_general_ledger_report(report, realm_id="realm-3")
    assert entries == []
    assert diag["records_processed"] == 0
    assert diag["data_row_count"] == 0
