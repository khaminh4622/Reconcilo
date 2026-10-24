"""Unit tests for the model boundary and Pydantic extraction contract."""

from __future__ import annotations

import json

import pytest

from agent3.extractor import ExtractionError, LLMExtractor, extraction_json_schema, parse_llm_payload
from agent3.schemas import AnalysisSource, ProblemStatus, ProblemType
from agent3.validator import validate_document


SAMPLE_TEXT = """[Page 1]
INVOICE
Invoice Number: INV-100
Vendor: Blue Finch Supplies
Customer: Avery Nguyen
Issue Date: 2026-01-10
Due Date: 2026-02-09
Currency: USD
Subtotal: 100.00
Tax: 8.00
Discount: 0.00
Total Due: 108.00
Amount Paid: 0.00
Outstanding Balance: 108.00
"""


class StaticClient:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.content


def test_normal_invoice_extraction_uses_the_mocked_llm_boundary():
    payload = {
        "document_type": "invoice",
        "document_id": "INV-100",
        "issue_date": "2026-01-10",
        "currency": "USD",
        "vendor": {"vendor_name": "Blue Finch Supplies"},
        "customer": {"customer_name": "Avery Nguyen"},
        "financial_information": {
            "currency": "USD", "subtotal": "100.00", "tax_amount": "8.00",
            "discount": "0.00", "total_amount": "108.00",
        },
        "missing_information": [], "contradictions": [], "information_requiring_verification": [],
    }
    client = StaticClient(json.dumps(payload))
    result = LLMExtractor(client).extract(SAMPLE_TEXT)

    assert len(client.calls) == 1
    assert "DOCUMENT START" in client.calls[0][1]
    assert result.analysis_source == AnalysisSource.live_llm
    assert result.document_id == "INV-100"
    assert str(result.financial_information.total_amount) == "108.00"
    assert validate_document(result).status == "passed"


def test_customer_payment_dispute_is_understood_as_a_reported_discrepancy():
    source_text = "[Page 1]\nI already paid invoice INV-9, but it still says unpaid."
    result = parse_llm_payload(
        {
            "document_type": "other",
            "detected_problem": {
                "type": "payment_status_discrepancy",
                "description": "The customer reports a paid invoice still marked unpaid.",
                "source_evidence": "I already paid invoice INV-9, but it still says unpaid.",
                "status": "reported",
            },
            "missing_information": ["payment receipt"],
            "contradictions": [],
            "information_requiring_verification": ["payment settlement status"],
        },
        source_text,
    )

    assert result.detected_problem.type == ProblemType.payment_status_discrepancy
    assert result.detected_problem.status == ProblemStatus.reported
    assert result.detected_problem.page == 1
    assert result.detected_problem.source_evidence in source_text


def test_refund_request_and_missing_customer_information_are_preserved_not_invented():
    source_text = "[Page 1]\nPlease refund the $45 charge on order ORD-44."
    result = parse_llm_payload(
        {
            "document_type": "refund_request",
            "document_id": "ORD-44",
            "financial_information": {"total_amount": "45.00", "currency": "USD"},
            "detected_problem": {
                "type": "refund_request", "description": "Refund requested for a charge.",
                "source_evidence": "Please refund the $45 charge on order ORD-44.", "status": "reported",
            },
            "missing_information": ["customer name", "customer email"],
            "contradictions": [],
            "information_requiring_verification": [],
        },
        source_text,
    )

    assert result.customer.customer_name is None
    assert result.customer.email is None
    assert result.missing_information == ["customer name", "customer email"]
    assert result.detected_problem.type == ProblemType.refund_request


def test_unsupported_contradiction_and_problem_evidence_are_discarded():
    result = parse_llm_payload(
        {
            "document_type": "invoice",
            "contradictions": ["The totals conflict."],
            "detected_problem": {
                "type": "unpaid_invoice", "description": "Claimed unpaid", "source_evidence": "Not in this PDF", "status": "reported"
            },
        },
        SAMPLE_TEXT,
    )

    assert result.contradictions == []
    assert result.detected_problem.source_evidence is None
    assert result.detected_problem.status == ProblemStatus.requires_clarification
    assert result.processing_errors


def test_unknown_document_type_is_other_and_malformed_values_are_not_guessed():
    result = parse_llm_payload(
        {
            "document_type": "shipping_manifest",
            "financial_information": {"total_amount": {"not": "a number"}},
            "missing_information": "not a list",
        },
        "[Page 1]\nA previously unseen delivery note.",
    )

    assert result.document_type.value == "other"
    assert result.financial_information.total_amount is None
    assert result.processing_errors


def test_malformed_llm_json_is_rejected():
    with pytest.raises(ExtractionError):
        LLMExtractor(StaticClient("{not valid json")).extract(SAMPLE_TEXT)


def test_strict_output_schema_requires_all_declared_top_level_fields():
    schema = extraction_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_negative_ledger_payment_line_is_normalised_to_a_magnitude():
    """A statement's "-612.40" payment line must not become a negative amount_paid."""

    text = (
        "[Page 1]\nPrevious balance 612.40\nPayment received 11 Aug -612.40\n"
        "Total amount now due 773.15\n"
    )
    result = parse_llm_payload(
        {
            "document_type": "bank_statement",
            "financial_information": {"amount_paid": "-612.40", "total_amount": "773.15"},
        },
        text,
    )

    assert str(result.financial_information.amount_paid) == "612.40"
    assert any("normalised to its magnitude" in message for message in result.processing_errors)


def test_negative_total_is_preserved_because_it_is_a_real_inconsistency():
    text = "[Page 1]\nTotal Due: -50.00\n"
    result = parse_llm_payload(
        {"document_type": "invoice", "financial_information": {"total_amount": "-50.00"}},
        text,
    )
    assert str(result.financial_information.total_amount) == "-50.00"
