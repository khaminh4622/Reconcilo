"""Requirement tests for financial extraction and deterministic validation.

These cover the six cases required by the Agent 3 specification. Each test
builds a small invoice PDF, feeds the pipeline a *deliberately imperfect* model
reply (small local models omit fields and never reason about arithmetic), and
asserts that Python reaches the correct financial conclusion.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pymupdf
import pytest

from agent3.database import DocumentRepository
from agent3.document_reader import read_pdf_upload
from agent3.extractor import LLMExtractor, parse_llm_payload
from agent3.schemas import FinancialStatus, PaymentStatus, RecommendedAgent
from agent3.service import Agent3Service
from agent3.synthetic_data import reference_customers


def make_pdf(lines: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(pymupdf.Rect(50, 50, 560, 740), lines, fontsize=10, lineheight=1.3)
    content = document.tobytes()
    document.close()
    return content


class StubModel:
    """Returns a fixed reply, mimicking a small local model's behaviour."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return json.dumps(self.payload)


@pytest.fixture
def run_document(workspace_tmp_path):
    """Run one PDF through the real service with a stubbed model reply."""

    def _run(invoice_text: str, model_payload: dict):
        repository = DocumentRepository(workspace_tmp_path / "cases.db")
        repository.seed_customer_references(reference_customers())
        service = Agent3Service(repository, LLMExtractor(StubModel(model_payload)))
        return service.analyze(read_pdf_upload("case.pdf", make_pdf(invoice_text)))

    return _run


# ---------------------------------------------------------------------------
# 1. Valid invoice with consistent amounts
# ---------------------------------------------------------------------------
CONSISTENT_INVOICE = """INVOICE
Vendor: Harborline Office Supply
Customer: Blue Harbor Technologies
Invoice Number: INV-2026-2001
Invoice Date: 03 Mar 2026
Due Date: 02 Apr 2026
Currency: USD
Subtotal: $1,000.00
Tax: $80.00
Invoice Total: $1,080.00
Payment Reported: $1,080.00
Outstanding Balance: $0.00"""


def test_valid_invoice_with_consistent_amounts(run_document):
    result = run_document(
        CONSISTENT_INVOICE,
        {
            "document_type": "invoice",
            "document_id": "INV-2026-2001",
            "issue_date": "03 Mar 2026",
            "due_date": "02 Apr 2026",
            "currency": "USD",
            "customer": {"customer_name": "Blue Harbor Technologies"},
            "vendor": {"vendor_name": "Harborline Office Supply"},
            "financial_information": {
                "currency": "USD",
                "subtotal": "1000.00",
                "tax_amount": "80.00",
                "total_amount": "1080.00",
                "amount_paid": "1080.00",
                "outstanding_balance": "0.00",
            },
            "detected_problem": {"type": "none_identified"},
        },
    )

    validation = result.validation
    # The invoice's own arithmetic reconciles...
    assert validation.calculated.expected_total == Decimal("1080.00")
    assert validation.calculated.expected_outstanding_balance == Decimal("0.00")
    assert validation.calculated.potential_overpayment is None
    checks = {c.code: c.status for c in validation.validation_findings}
    assert checks["subtotal_tax_total"] == "passed"
    assert checks["outstanding_balance_consistent"] == "passed"
    # ...but the payment behind the zero balance is still only a claim, so the
    # overall financial status can never be PASSED. This is the specification
    # rule that an unconfirmed payment must not be displayed as a pass.
    assert validation.payment_provenance == PaymentStatus.reported
    assert validation.requires_reconciliation is True
    assert validation.financial_consistency == FinancialStatus.unverified
    assert checks["payment_verification"] == "unverified"
    assert result.routing.recommended_agent == RecommendedAgent.agent_2


# ---------------------------------------------------------------------------
# 2. Potential overpayment (the reported Blue Harbor case)
# ---------------------------------------------------------------------------
OVERPAYMENT_INVOICE = """INVOICE
Vendor: Harborline Office Supply
Customer: Blue Harbor Technologies
Invoice Number: INV-2026-1042
Invoice Date: 12 Jan 2026
Due Date: 11 Feb 2026
Currency: USD
Subtotal: $2,500.00
Tax: $200.00
Invoice Total: $2,700.00
Payment Reported: $3,200.00
Outstanding Balance: $500.00"""


def test_invoice_with_potential_overpayment(run_document):
    """The exact scenario from the bug report: 3,200 paid against a 2,700 invoice."""

    result = run_document(
        OVERPAYMENT_INVOICE,
        {
            "document_type": "invoice",
            "document_id": "INV-2026-1042",
            "issue_date": "12 Jan 2026",
            "due_date": "11 Feb 2026",
            # The model omits currency, total and tax, exactly as qwen3:4b did.
            "currency": None,
            "customer": {"customer_name": "Blue Harbor Technologies"},
            "vendor": {"vendor_name": None},
            "financial_information": {
                "currency": None,
                "subtotal": None,
                "tax_amount": None,
                "total_amount": None,
                "amount_paid": "3200.00",
                "outstanding_balance": "500.00",
            },
            "detected_problem": {
                "type": "payment_status_discrepancy",
                "description": "Customer says they already paid in full.",
                "source_evidence": "Payment Reported: $3,200.00",
                "status": "reported",
            },
        },
    )

    financial = result.financial_information
    validation = result.validation

    # Fields the model dropped are recovered from the document itself.
    assert financial.currency == "USD"
    assert financial.total_amount == Decimal("2700.00")
    assert financial.subtotal == Decimal("2500.00")
    assert financial.tax_amount == Decimal("200.00")
    assert financial.amount_paid == Decimal("3200.00")
    assert financial.outstanding_balance == Decimal("500.00")

    # Deterministic overpayment detection.
    assert validation.calculated.potential_overpayment == Decimal("500.00")
    assert validation.financial_consistency == FinancialStatus.failed
    assert validation.customer_verification == "unverified"
    assert result.requires_review is True

    # The payment is only a claim and must be reconciled by Agent 2.
    assert validation.payment_provenance == PaymentStatus.reported
    assert validation.requires_reconciliation is True
    assert result.routing.recommended_agent == RecommendedAgent.agent_2
    assert "reconcil" in result.routing.reason.lower()

    codes = {c.code: c.status for c in validation.validation_findings}
    assert codes["overpayment_detected"] == "failed"
    assert codes["payment_verification"] == "unverified"
    # The arithmetic of the invoice itself is consistent; the conflict is the payment.
    assert codes["subtotal_tax_total"] == "passed"


# ---------------------------------------------------------------------------
# 3. Invoice with missing financial information
# ---------------------------------------------------------------------------
MISSING_FINANCIALS_INVOICE = """INVOICE
Vendor: Harborline Office Supply
Invoice Number: INV-2026-3003
Invoice Date: 05 Apr 2026
Customer: (not recorded)
Line Item: Consulting services - April
Amount: (to be confirmed)"""


def test_invoice_with_missing_financial_information(run_document):
    result = run_document(
        MISSING_FINANCIALS_INVOICE,
        {
            "document_type": "invoice",
            "document_id": "INV-2026-3003",
            "issue_date": "05 Apr 2026",
            "vendor": {"vendor_name": "Harborline Office Supply"},
            "customer": {},
            "financial_information": {},
            "detected_problem": {"type": "none_identified"},
        },
    )

    validation = result.validation
    # A missing total or currency can never be reported as a financial pass.
    assert validation.financial_consistency == FinancialStatus.failed
    assert "currency" in validation.missing_fields
    assert "financial_information.total_amount" in validation.missing_fields
    assert result.requires_review is True
    codes = {c.code: c.status for c in validation.validation_findings}
    assert codes["required_financial_fields"] == "failed"
    # Nothing may be invented to fill the gaps.
    assert result.financial_information.total_amount is None
    assert result.financial_information.amount_paid is None


# ---------------------------------------------------------------------------
# 4. Inconsistent subtotal, tax and total
# ---------------------------------------------------------------------------
INCONSISTENT_TOTAL_INVOICE = """INVOICE
Vendor: Harborline Office Supply
Customer: Blue Harbor Technologies
Invoice Number: INV-2026-4004
Invoice Date: 07 May 2026
Currency: USD
Subtotal: $1,000.00
Tax: $80.00
Invoice Total: $1,250.00"""


def test_inconsistent_subtotal_tax_and_total(run_document):
    result = run_document(
        INCONSISTENT_TOTAL_INVOICE,
        {
            "document_type": "invoice",
            "document_id": "INV-2026-4004",
            "issue_date": "07 May 2026",
            "currency": "USD",
            "customer": {"customer_name": "Blue Harbor Technologies"},
            "vendor": {"vendor_name": "Harborline Office Supply"},
            "financial_information": {
                "currency": "USD",
                "subtotal": "1000.00",
                "tax_amount": "80.00",
                # 1000 + 80 = 1080, but the invoice claims 1250.
                "total_amount": "1250.00",
            },
            "detected_problem": {"type": "none_identified"},
        },
    )

    validation = result.validation
    assert validation.calculated.expected_total == Decimal("1080.00")
    assert validation.financial_consistency == FinancialStatus.failed
    check = next(c for c in validation.validation_findings if c.code == "subtotal_tax_total")
    assert check.status == "failed"
    assert check.expected == "1080.00"
    assert check.reported == "1250.00"
    assert check.variance == "170.00"
    assert result.requires_review is True


# ---------------------------------------------------------------------------
# 5. Unverified customer-reported payment
# ---------------------------------------------------------------------------
CLAIMED_PAYMENT_INVOICE = """INVOICE
Vendor: Harborline Office Supply
Customer: Blue Harbor Technologies
Invoice Number: INV-2026-5005
Invoice Date: 09 Jun 2026
Currency: USD
Subtotal: $1,000.00
Tax: $0.00
Invoice Total: $1,000.00
Outstanding Balance: $1,000.00

Customer note: I have already paid this invoice in full last week,
I will not be paying it again."""


def test_unverified_customer_reported_payment(run_document):
    result = run_document(
        CLAIMED_PAYMENT_INVOICE,
        {
            "document_type": "invoice",
            "document_id": "INV-2026-5005",
            "issue_date": "09 Jun 2026",
            "currency": "USD",
            "customer": {"customer_name": "Blue Harbor Technologies"},
            "vendor": {"vendor_name": "Harborline Office Supply"},
            "financial_information": {
                "currency": "USD",
                "subtotal": "1000.00",
                "tax_amount": "0.00",
                "total_amount": "1000.00",
                "amount_paid": "1000.00",
                "outstanding_balance": "1000.00",
            },
            "detected_problem": {
                "type": "payment_status_discrepancy",
                "description": "Customer says the invoice was already paid.",
                "source_evidence": "I have already paid this invoice in full last week",
                "status": "reported",
            },
        },
    )

    validation = result.validation
    claim = result.financial_information.payment_claim
    # The claim is reported, never verified, and blocks settlement.
    assert claim.status == PaymentStatus.reported
    assert claim.requires_reconciliation is True
    assert validation.payment_provenance == PaymentStatus.reported
    assert validation.requires_reconciliation is True
    assert "payment confirmation" in validation.unverified_information
    check = next(c for c in validation.validation_findings if c.code == "payment_verification")
    assert check.status == "unverified"
    # An unconfirmed payment can never be a financial pass.
    assert validation.financial_consistency != FinancialStatus.passed
    assert result.routing.recommended_agent == RecommendedAgent.agent_2


# ---------------------------------------------------------------------------
# 6. Alternative field labels
# ---------------------------------------------------------------------------
ALTERNATIVE_LABELS_INVOICE = """TAX INVOICE
Harborline Office Supply
Bill To: Blue Harbor Technologies
Invoice No: INV-2026-6006
Date: 11 Jul 2026
All amounts in USD

Net Amount           1,500.00
GST                  150.00
Grand Total          1,650.00
Less Discount         50.00
Total Payable        1,600.00
Remittance           1,600.00
Balance Owing            0.00"""


def test_invoice_with_alternative_field_labels(run_document):
    """Field recovery must work when the model misses non-standard labels."""

    result = run_document(
        ALTERNATIVE_LABELS_INVOICE,
        {
            "document_type": "invoice",
            "document_id": "INV-2026-6006",
            "issue_date": "11 Jul 2026",
            "currency": None,
            "customer": {"customer_name": "Blue Harbor Technologies"},
            "vendor": {},
            # The model understood nothing about the money here.
            "financial_information": {},
            "detected_problem": {"type": "none_identified"},
        },
    )

    financial = result.financial_information
    assert financial.currency == "USD"
    # "Net Amount" is the subtotal and "GST" is the tax.
    assert financial.subtotal == Decimal("1500.00")
    assert financial.tax_amount == Decimal("150.00")
    # "Total Payable" is the grand total, not the discounted grand total line.
    assert financial.total_amount == Decimal("1600.00")
    assert financial.amount_paid == Decimal("1600.00")
    assert financial.outstanding_balance == Decimal("0.00")
    # Payment is still unverified, so reconciliation is required.
    assert result.validation.requires_reconciliation is True


def test_alternative_labels_are_never_confused_with_each_other(run_document):
    """"Subtotal" must never be read as the grand total, or vice versa."""

    text = (
        "Subtotal: 100.00\nTax: 8.00\nInvoice Total: 108.00\n"
        "Amount Paid: 0.00\nOutstanding Balance: 108.00"
    )
    from agent3.extractor import recover_amount_from_text

    assert recover_amount_from_text(text, "subtotal")[0] == Decimal("100.00")
    assert recover_amount_from_text(text, "total_amount")[0] == Decimal("108.00")
    assert recover_amount_from_text(text, "outstanding_balance")[0] == Decimal("108.00")


# ---------------------------------------------------------------------------
# Cross-cutting guarantees
# ---------------------------------------------------------------------------
def test_reported_payment_is_never_silently_treated_as_confirmed(run_document):
    """Even a perfectly balanced invoice cannot mark the payment as verified."""

    result = run_document(
        CONSISTENT_INVOICE,
        {
            "document_type": "invoice",
            "document_id": "INV-2026-2001",
            "currency": "USD",
            "customer": {"customer_name": "Blue Harbor Technologies"},
            "vendor": {"vendor_name": "Harborline Office Supply"},
            "financial_information": {
                "currency": "USD",
                "subtotal": "1000.00",
                "tax_amount": "80.00",
                "total_amount": "1080.00",
                "amount_paid": "1080.00",
                "outstanding_balance": "0.00",
                "payment_status": "Paid",
            },
            "detected_problem": {"type": "none_identified"},
        },
    )
    assert result.financial_information.payment_claim.status != PaymentStatus.verified
    assert result.validation.requires_reconciliation is True
    # The agent never claims the balance is settled.
    assert result.routing.recommended_agent == RecommendedAgent.agent_2


def test_output_contains_every_required_section(run_document):
    result = run_document(
        CONSISTENT_INVOICE,
        {
            "document_type": "invoice",
            "document_id": "INV-2026-2001",
            "currency": "USD",
            "financial_information": {"currency": "USD", "total_amount": "1080.00"},
            "detected_problem": {"type": "none_identified"},
        },
    )
    payload = result.model_dump(mode="json")
    for section in (
        "customer_information",
        "vendor_information",
        "document_information",
        "financial_information",
        "detected_problem",
        "validation_findings",
        "routing",
        "reasoning",
        "missing_information",
        "evidence",
    ):
        assert section in payload, f"missing required section: {section}"
    assert payload["reasoning"], "reasoning must explain the decision"
    assert payload["routing"]["reason"], "routing must be explained"
    for finding in payload["validation_findings"]:
        assert finding["status"] in {"passed", "failed", "unverified", "not_applicable"}
        assert finding["explanation"]
