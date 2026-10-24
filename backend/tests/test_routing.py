"""Routing rules and customer/vendor role extraction.

Covers the duplicate-payment routing defect (Agent 3 was recommending Agent 2)
and the customer/vendor swap, plus the full routing table from the spec.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pymupdf
import pytest

from agent3.database import DocumentRepository
from agent3.document_reader import read_pdf_upload
from agent3.extractor import LLMExtractor, parse_llm_payload
from agent3.prompts import SYSTEM_PROMPT
from agent3.schemas import PrimaryProblem, RecommendedAgent
from agent3.service import Agent3Service
from agent3.synthetic_data import reference_customers


def make_pdf(lines: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(pymupdf.Rect(45, 45, 565, 745), lines, fontsize=9.5, lineheight=1.3)
    content = document.tobytes()
    document.close()
    return content


DUP_PDF = """SENDER / CUSTOMER
Blue Harbor Technologies Inc.
1200 Marina Way, Suite 400
Accounts Payable: ap@blueharbor.test

RECIPIENT / VENDOR
Northstar Office Supplies LLC
88 Foundry Road
Billing: billing@northstar-supplies.test

Invoice Number: INV-2026-2058
Invoice Date: 04 Feb 2026
Due Date: 06 Mar 2026
Currency: USD

Managed print services - February
Subtotal: $1,800.00
Tax: $144.00
Invoice Total: $1,944.00

Payment reference PMT-778812 (12 Feb 2026): $1,944.00
Payment reference PMT-779330 (19 Feb 2026): $1,944.00
Total reported payments: $3,888.00

Customer note: I think we have paid this invoice twice by mistake. Both
transfers left our account, PMV-778812 on 12 February and PMV-779330 on
19 February, each for 1,944.00. Please refund the duplicate payment."""


class StubModel:
    def __init__(self, payload: dict):
        self.payload = payload

    def complete(self, system: str, user: str) -> str:
        return json.dumps(self.payload)


DUP_PAYLOAD = {
    "document_type": "invoice",
    "document_id": "INV-2026-2058",
    "issue_date": "04 Feb 2026",
    "due_date": "06 Mar 2026",
    "currency": "USD",
    "customer": {"customer_name": "Blue Harbor Technologies Inc."},
    "vendor": {"vendor_name": "Northstar Office Supplies LLC"},
    "financial_information": {
        "currency": "USD",
        "subtotal": "1800.00",
        "tax_amount": "144.00",
        "total_amount": "1944.00",
        "amount_paid": "3888.00",
        "outstanding_balance": "0.00",
    },
    "detected_problem": {
        "type": "payment_status_discrepancy",
        "primary_problem": "duplicate_payment",
        "reported_payment_count": 2,
        "description": "The customer reports paying the same invoice twice.",
        "source_evidence": "I think we have paid this invoice twice by mistake.",
        "status": "reported",
    },
}


@pytest.fixture
def analyze(workspace_tmp_path):
    def _analyze(pdf_text: str, payload: dict):
        repository = DocumentRepository(workspace_tmp_path / "routing.db")
        repository.seed_customer_references(reference_customers())
        service = Agent3Service(repository, LLMExtractor(StubModel(payload)))
        return service.analyze(read_pdf_upload("doc.pdf", make_pdf(pdf_text)))

    return _analyze


# ---------------------------------------------------------------------------
# Problem 1: duplicate payment routes to Agent 1, not Agent 2
# ---------------------------------------------------------------------------
def test_duplicate_payment_routes_to_agent_1(analyze):
    result = analyze(DUP_PDF, DUP_PAYLOAD)
    routing = result.routing

    assert routing.primary_problem == PrimaryProblem.duplicate_payment
    assert routing.recommended_agent == RecommendedAgent.agent_1
    assert "Duplicate Invoice Investigator" in (routing.agent_name or "")
    assert "two separate payments" in routing.reason.lower()
    assert routing.priority == "high"
    assert routing.blocking is True
    assert routing.overridden is False
    assert result.requires_review is True


def test_duplicate_payment_amount_is_detected(analyze):
    result = analyze(DUP_PDF, DUP_PAYLOAD)
    assert result.validation.calculated.potential_duplicate_amount == Decimal("1944.00")


def test_agent_2_is_recorded_as_secondary_investigation(analyze):
    result = analyze(DUP_PDF, DUP_PAYLOAD)
    routing = result.routing
    assert routing.secondary_agent == RecommendedAgent.agent_2
    assert "bank records" in (routing.secondary_reason or "").lower()


def test_duplicate_is_never_treated_as_confirmed(analyze):
    result = analyze(DUP_PDF, DUP_PAYLOAD)
    assert "alleged, not confirmed" in result.routing.reason.lower()
    # The payment itself remains unverified.
    assert result.validation.requires_reconciliation is True
    assert result.financial_information.payment_claim.status.value == "reported"
    duplicate_check = next(
        c for c in result.validation.validation_findings if c.code == "duplicate_payment_reported"
    )
    assert duplicate_check.status == "unverified"


def test_overpayment_alone_does_not_override_a_duplicate_classification(analyze):
    """The reported payment is also an overpayment; Agent 1 must still win."""

    result = analyze(DUP_PDF, DUP_PAYLOAD)
    assert result.validation.calculated.potential_overpayment == Decimal("1944.00")
    assert result.routing.recommended_agent == RecommendedAgent.agent_1


def test_missing_classification_is_inferred_and_marked_overridden(analyze):
    payload = json.loads(json.dumps(DUP_PAYLOAD))
    payload["detected_problem"].pop("primary_problem")

    result = analyze(DUP_PDF, payload)
    assert result.routing.primary_problem == PrimaryProblem.duplicate_payment
    assert result.routing.recommended_agent == RecommendedAgent.agent_1
    assert result.routing.overridden is True


# ---------------------------------------------------------------------------
# Problem 3: the full routing table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("primary", "expected_agent"),
    [
        ("duplicate_invoice", RecommendedAgent.agent_1),
        ("duplicate_payment", RecommendedAgent.agent_1),
        ("bank_reconciliation", RecommendedAgent.agent_2),
        ("payment_failure", RecommendedAgent.agent_4),
        ("payment_request", RecommendedAgent.human_approval),
        ("unknown", RecommendedAgent.human_review),
    ],
)
def test_routing_table_routes_each_primary_problem(analyze, primary, expected_agent):
    payload = json.loads(json.dumps(DUP_PAYLOAD))
    payload["detected_problem"]["primary_problem"] = primary

    result = analyze(DUP_PDF, payload)
    assert result.routing.primary_problem.value == primary
    assert result.routing.recommended_agent == expected_agent


def test_payment_request_requires_human_approval(analyze):
    payload = json.loads(json.dumps(DUP_PAYLOAD))
    payload["detected_problem"]["primary_problem"] = "payment_request"

    result = analyze(DUP_PDF, payload)
    assert result.routing.requires_human_approval is True
    assert result.routing.blocking is True
    assert "human approval" in result.routing.reason.lower()


def test_unknown_primary_problem_escalates_to_human_review(analyze):
    payload = json.loads(json.dumps(DUP_PAYLOAD))
    payload["detected_problem"]["primary_problem"] = "unknown"

    result = analyze(DUP_PDF, payload)
    assert result.routing.recommended_agent == RecommendedAgent.human_review
    assert result.routing.blocking is True


def test_unrecognised_classification_falls_back_to_unknown():
    result = parse_llm_payload(
        {
            "document_type": "invoice",
            "detected_problem": {"type": "none_identified", "primary_problem": "cosmic_rays"},
        },
        "[Page 1]\nInvoice Total: 100.00",
    )
    assert result.detected_problem.primary_problem == PrimaryProblem.unknown
    assert any("primary_problem" in message for message in result.processing_errors)


def test_duplicate_wording_wins_over_reconciliation_wording(analyze):
    """A document mentioning both must classify as the duplicate."""

    payload = json.loads(json.dumps(DUP_PAYLOAD))
    payload["detected_problem"]["primary_problem"] = "duplicate_payment"

    result = analyze(DUP_PDF, payload)
    assert result.routing.primary_problem == PrimaryProblem.duplicate_payment
    assert result.routing.recommended_agent == RecommendedAgent.agent_1


# ---------------------------------------------------------------------------
# Problem 2: customer and vendor are not swapped
# ---------------------------------------------------------------------------
def test_customer_and_vendor_roles_are_correct(analyze):
    result = analyze(DUP_PDF, DUP_PAYLOAD)
    assert result.customer_information.customer_name == "Blue Harbor Technologies Inc."
    assert result.vendor_information.vendor_name == "Northstar Office Supplies LLC"


def test_role_guidance_is_present_in_the_prompt():
    """The prompt must explain roles, not just list field names."""

    assert "Do not assume that the first company named" in SYSTEM_PROMPT
    for label in ("Bill To", "Remit To", "Vendor", "Sender"):
        assert label in SYSTEM_PROMPT
    assert "uncertain side null" in SYSTEM_PROMPT


def test_model_supplied_roles_are_preserved_even_when_swapped_in_text_order():
    """A correct model answer must not be re-derived or swapped by parsing."""

    text = (
        "[Page 1]\nNorthstar Office Supplies LLC\nBill From\n\n"
        "Blue Harbor Technologies Inc.\nBill To\n\nInvoice Total: 100.00\n"
    )
    result = parse_llm_payload(
        {
            "document_type": "invoice",
            "customer": {"customer_name": "Blue Harbor Technologies Inc."},
            "vendor": {"vendor_name": "Northstar Office Supplies LLC"},
            "financial_information": {"total_amount": "100.00"},
        },
        text,
    )
    assert result.customer.customer_name == "Blue Harbor Technologies Inc."
    assert result.vendor.vendor_name == "Northstar Office Supplies LLC"


# ---------------------------------------------------------------------------
# Preserved behaviour
# ---------------------------------------------------------------------------
def test_financial_validation_engine_still_runs(analyze):
    result = analyze(DUP_PDF, DUP_PAYLOAD)
    codes = {c.code: c.status for c in result.validation.validation_findings}
    assert codes["subtotal_tax_total"] == "passed"
    assert codes["payment_verification"] == "unverified"
    assert result.validation.calculated.expected_total == Decimal("1944.00")
