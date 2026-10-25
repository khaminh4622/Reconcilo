"""Regression tests for new-payment intent classification.

The reported bug: a valid new invoice whose cover note said "Please review this
new invoice for approval. No payment has been requested or made yet." was
classified UNKNOWN and sent to generic human review.

Two defects caused it:

1. The cue table had no payment-request patterns for approval language, so the
   word "payment status" on the invoice matched bank_reconciliation instead.
2. A document that merely *describes* its unpaid state ("no payment has been
   made yet") was treated as refusing payment.

These tests pin the corrected behaviour and guard the boundaries so an invoice
submitted only for recordkeeping never becomes a payment request.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from agent3.document_reader import read_pdf_upload
from agent3.extractor import LLMExtractor, parse_llm_payload
from agent3.schemas import (
    AnalysisResult,
    DetectedProblem,
    DocumentType,
    FinancialInformation,
    PrimaryProblem,
    ProblemType,
)
from agent3.validator import infer_primary_problem, is_unpaid_invoice
from workflow.orchestrator import WorkflowOrchestrator, _intent_from_analysis
from workflow.repository import WorkflowRepository
from workflow.agent1_schemas import InvestigationStatus
from workflow.schemas import AgentId, CaseStatus, Intent

from test_agent1 import seed_invoice_history


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pdfs" / "Agent1_Test_Original_Unpaid_Invoice.pdf"

COVER_NOTE = (
    "Please review this new invoice for approval. No payment has been requested or made yet."
)


def assert_settled_after_reconciliation(case):
    """Agent 5 confirms a payment only after Agent 2 reconciles it.

    Corrected order: Agent 5 initiates, Agent 2 checks the money, then Agent 5
    confirms. A case reaches COMPLETED only when Agent 2 reported MATCHED with
    independent evidence.
    """

    from workflow.schemas import CaseStatus

    assert case.payment is not None
    assert case.reconciliation is not None
    verdict = case.reconciliation.get("verdict")
    assert verdict, "Agent 2 must always record a verdict"

    if verdict == "MATCHED" and case.reconciliation.get("independent_evidence"):
        assert case.payment.status == "SUCCESS"
        assert case.payment.verified is True
        assert case.receipt is not None
        assert case.status == CaseStatus.completed
    else:
        # Money may have moved, but it is not confirmed.
        assert case.status != CaseStatus.completed
        assert case.receipt is None
        assert case.status in {CaseStatus.requires_review, CaseStatus.pending}


def payment_attempt(case):
    """The single payment attempt recorded for a case."""

    assert case.payment is not None
    return case.payment

def build_pdf(cover_note: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(
        pymupdf.Rect(50, 50, 560, 740),
        "\n".join(
            [
                "INVOICE",
                "Vendor: Harborline Office Supply",
                "Customer: Avery Nguyen",
                "Company: Lantern Studio",
                "Invoice Number: INV-2026-7701",
                "Invoice Date: 15 Mar 2026",
                "Due Date: 14 Apr 2026",
                "Currency: USD",
                "Description: Quarterly office furniture refurbishment",
                "Subtotal: $2,400.00",
                "Tax: $192.00",
                "Invoice Total: $2,592.00",
                "Amount Paid: $0.00",
                "Outstanding Balance: $2,592.00",
                "Payment Status: Unpaid",
                "",
                cover_note,
            ]
        ),
        fontsize=10,
        lineheight=1.3,
    )
    content = document.tobytes()
    document.close()
    return content


class StubModel:
    """A model that omits primary_problem, which is what triggered the bug."""

    def __init__(self, payload: dict):
        self.payload = payload

    def complete(self, system: str, user: str) -> str:
        import json

        return json.dumps(self.payload)


BASE_PAYLOAD = {
    "document_type": "invoice",
    "document_id": "INV-2026-7701",
    "issue_date": "15 Mar 2026",
    "due_date": "14 Apr 2026",
    "currency": "USD",
    "customer": {"customer_name": "Avery Nguyen", "company_name": "Lantern Studio"},
    "vendor": {"vendor_name": "Harborline Office Supply"},
    "financial_information": {
        "currency": "USD",
        "subtotal": "2400.00",
        "tax_amount": "192.00",
        "total_amount": "2592.00",
        "amount_paid": "0.00",
        "outstanding_balance": "2592.00",
        "payment_status": "Unpaid",
    },
    # Note: no primary_problem key, reproducing the model behaviour that failed.
    "detected_problem": {
        "type": "unpaid_invoice",
        "description": "A new invoice with an outstanding balance.",
        "source_evidence": "Payment Status: Unpaid",
        "status": "reported",
    },
}


def analyze(payload: dict, cover_note: str = COVER_NOTE, database: Path | None = None) -> AnalysisResult:
    """Run the full Agent 3 service path, exactly as an upload does.

    Calling the extractor directly would skip validation and routing, so tests
    must go through ``Agent3Service`` to exercise real behaviour.
    """

    from uuid import uuid4

    from agent3.database import DocumentRepository
    from agent3.service import Agent3Service

    document = read_pdf_upload("invoice.pdf", build_pdf(cover_note))
    if database is None:
        scratch = Path(__file__).resolve().parents[1] / ".test-scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        database = scratch / f"a3-{uuid4().hex[:10]}.db"
    service = Agent3Service(DocumentRepository(database), LLMExtractor(StubModel(payload)))
    return service.analyze(document)


# ---------------------------------------------------------------------------
# The reported case
# ---------------------------------------------------------------------------
def test_reported_invoice_is_classified_as_new_payment_request():
    """The exact failing document must now resolve to a payment request."""

    result = analyze(BASE_PAYLOAD)
    intent = _intent_from_analysis(result)
    assert intent == Intent.new_payment_request, (
        f"expected new_payment_request, got {intent.value}"
    )
    assert result.detected_problem.primary_problem == PrimaryProblem.payment_request


def test_reported_invoice_extracts_and_validates_financials():
    result = analyze(BASE_PAYLOAD)

    financial = result.financial_information
    assert financial.subtotal == Decimal("2400.00")
    assert financial.tax_amount == Decimal("192.00")
    assert financial.total_amount == Decimal("2592.00")
    assert result.currency == "USD"
    codes = {c.code: c.status for c in result.validation.validation_findings}
    assert codes.get("subtotal_tax_total") == "passed"
    assert result.validation.financial_consistency.value == "passed"
    # A clean invoice is payable without a blocking validation issue.
    assert result.validation.missing_fields == []


def test_primary_problem_still_honoured_when_model_supplies_it():
    payload = dict(BASE_PAYLOAD)
    payload["detected_problem"] = {**BASE_PAYLOAD["detected_problem"], "primary_problem": "payment_request"}
    result = analyze(payload)
    assert result.detected_problem.primary_problem == PrimaryProblem.payment_request


# ---------------------------------------------------------------------------
# The document file itself
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not FIXTURE.is_file(), reason="fixture PDF is not present")
def test_fixture_pdf_text_contains_the_cover_note():
    text = read_pdf_upload(FIXTURE.name, FIXTURE.read_bytes()).text
    assert "please review this new invoice for approval" in text.casefold()


# ---------------------------------------------------------------------------
# Payment-request phrasing the model may omit
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cover_note",
    [
        COVER_NOTE,
        "Please process this invoice.",
        "This is a new invoice.",
        "We have not paid this invoice yet.",
        "Please submit this bill for payment approval.",
        "Kindly arrange payment of the outstanding balance.",
        "Invoice attached for your approval.",
        "This invoice is due for payment.",
    ],
)
def test_payment_request_language_is_recognised(cover_note):
    result = analyze(BASE_PAYLOAD, cover_note=cover_note)
    assert result.detected_problem.primary_problem == PrimaryProblem.payment_request
    assert _intent_from_analysis(result) == Intent.new_payment_request


# ---------------------------------------------------------------------------
# Boundaries: not every invoice is a payment request
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cover_note",
    [
        "Do not process this invoice. It is for recordkeeping only.",
        "For your information only - no payment is due at this time.",
        "For investigation only. Please do not pay this invoice.",
        "For reference only.",
    ],
)
def test_recordkeeping_documents_are_not_payment_requests(cover_note):
    result = analyze(BASE_PAYLOAD, cover_note=cover_note)
    assert result.detected_problem.primary_problem != PrimaryProblem.payment_request
    assert _intent_from_analysis(result) != Intent.new_payment_request


def test_describing_an_unpaid_invoice_is_not_refusing_payment():
    """"No payment has been made yet" means unpaid, not "do not pay"."""

    result = analyze(BASE_PAYLOAD)
    assert result.detected_problem.primary_problem == PrimaryProblem.payment_request


def test_no_outstanding_amount_is_not_a_payment_request():
    payload = dict(BASE_PAYLOAD)
    payload["financial_information"] = {
        **BASE_PAYLOAD["financial_information"],
        "amount_paid": "2592.00",
        "outstanding_balance": "0.00",
    }
    result = analyze(payload)
    assert result.detected_problem.primary_problem != PrimaryProblem.payment_request


def test_paid_duplicate_claim_is_not_a_payment_request():
    result = analyze(BASE_PAYLOAD, cover_note="I already paid this invoice but it still shows unpaid.")
    assert result.routing.primary_problem != PrimaryProblem.payment_request
    assert result.routing.primary_problem == PrimaryProblem.bank_reconciliation


# ---------------------------------------------------------------------------
# Intent distinction across all six categories
# ---------------------------------------------------------------------------
def _infer(cover_note: str, problem: ProblemType = ProblemType.unpaid_invoice, **financial) -> PrimaryProblem:
    values = {
        "currency": "USD",
        "total_amount": Decimal("2592.00"),
        "amount_paid": Decimal("0.00"),
    }
    values.update(financial)
    analysis = AnalysisResult(
        document_type=DocumentType.invoice,
        financial_information=FinancialInformation(**values),
        detected_problem=DetectedProblem(type=problem),
    )
    return infer_primary_problem(analysis, cover_note)[0]


@pytest.mark.parametrize(
    ("cover_note", "expected"),
    [
        (COVER_NOTE, PrimaryProblem.payment_request),
        ("Please process this invoice.", PrimaryProblem.payment_request),
        ("I paid this invoice twice by mistake.", PrimaryProblem.duplicate_payment),
        ("You have sent me this same invoice number twice.", PrimaryProblem.duplicate_invoice),
        ("The transfer failed and was declined by the bank.", PrimaryProblem.payment_failure),
        ("I already paid this invoice but it still shows unpaid.", PrimaryProblem.bank_reconciliation),
    ],
)
def test_the_six_intents_are_distinguished(cover_note, expected):
    assert _infer(cover_note) == expected


def test_unclassifiable_document_stays_unknown():
    analysis = AnalysisResult(
        document_type=DocumentType.other,
        financial_information=FinancialInformation(),
        detected_problem=DetectedProblem(type=ProblemType.none_identified),
    )
    assert infer_primary_problem(analysis, "A photograph of a cat.")[0] == PrimaryProblem.unknown


# ---------------------------------------------------------------------------
# End-to-end: routing, persistence and approval
# ---------------------------------------------------------------------------
def build_orchestrator(workspace_tmp_path, name: str) -> WorkflowOrchestrator:
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    WorkflowRepository(database)
    return WorkflowOrchestrator(WorkflowRepository(database))


def test_new_payment_request_routes_to_human_approval(workspace_tmp_path):
    orchestrator = build_orchestrator(workspace_tmp_path, "newpay")
    analysis = analyze(BASE_PAYLOAD)
    case = orchestrator.route(orchestrator.open_case(analysis, "Agent1_Test_Original_Unpaid_Invoice.pdf"))

    assert case.intent == Intent.new_payment_request
    assert case.status == CaseStatus.awaiting_approval
    agents = [run.agent for run in sorted(case.agent_runs, key=lambda r: r.order)]
    # Corrected order: approval, then Agent 5 initiates, Agent 2 reconciles,
    # and Agent 5 confirms.
    assert agents[:3] == [AgentId.agent_3, AgentId.agent_1, AgentId.human_approval]
    assert AgentId.payment_simulator in agents
    assert agents[-2:] == [AgentId.agent_2, AgentId.agent_5]
    # Agent 2 reconciles before Agent 5 confirms.
    assert AgentId.agent_2 in agents
    # Not generic clarification review.
    assert AgentId.human_review not in agents


def test_new_invoice_is_persisted_to_the_database(workspace_tmp_path):
    orchestrator = build_orchestrator(workspace_tmp_path, "persist")
    analysis = analyze(BASE_PAYLOAD)
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))

    stored = orchestrator.repository.get_case(case.case_id)
    assert stored is not None
    assert stored.invoice_number == "INV-2026-7701"
    assert stored.vendor_name == "Harborline Office Supply"
    assert stored.amount == Decimal("2592.00")


def test_approved_new_payment_executes_once(workspace_tmp_path):
    orchestrator = build_orchestrator(workspace_tmp_path, "approve")
    analysis = analyze(BASE_PAYLOAD)
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))

    assert case.status == CaseStatus.awaiting_approval
    approved = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    assert_settled_after_reconciliation(approved)
    assert approved.payment is not None and approved.payment.status == "SUCCESS"
    # The settlement lives in the independent Mock Bank ledger.
    assert len(orchestrator.execution.bank.ledger()) == 1
    assert len(orchestrator.repository.list_attempts()) == 1


def test_unapproved_new_invoice_cannot_be_paid(workspace_tmp_path):
    """No approval means no payment, and the refusal is recorded."""

    orchestrator = build_orchestrator(workspace_tmp_path, "unapproved")
    analysis = analyze(BASE_PAYLOAD)
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))
    assert case.status == CaseStatus.awaiting_approval

    result = orchestrator.execute_payment(case.case_id)

    # The payment was refused and nothing was executed.
    assert result.status != CaseStatus.completed
    assert orchestrator.repository.list_attempts() == []
    assert orchestrator.repository.list_bank_transactions() == []
    report = result.last_payment_report
    assert report is not None
    assert any(f.code == "no_approval" for f in report.findings)
    # The refusal is auditable.
    assert any(
        entry.action in {"execution_result", "payment_blocked", "execution_blocked"}
        for entry in result.audit
    )


def test_reuploaded_invoice_is_found_in_the_database(workspace_tmp_path):
    """Once saved, the invoice is available for duplicate comparison.

    A first upload of a new invoice goes to approval without a duplicate check
    (there is nothing to compare against yet). After it is saved, Agent 1 can
    see it on a later submission.
    """

    orchestrator = build_orchestrator(workspace_tmp_path, "reupload")
    first_analysis = analyze(BASE_PAYLOAD)
    first = orchestrator.open_case(first_analysis, "invoice.pdf")
    orchestrator.repository.save_case(first)

    # Agent 3 records each analysed document in document_records.
    from agent3.database import DocumentRepository

    DocumentRepository(orchestrator.repository.path).save(first_analysis, "hash-regression")

    # The saved invoice is now part of the comparison history.
    history = orchestrator.duplicates.history.list_records(customer="Avery Nguyen")
    assert any(
        record.invoice_number == "INV-2026-7701" for record in history
    ), "the saved invoice must be available for duplicate detection"


def test_agent_1_finds_the_reuploaded_invoice(workspace_tmp_path):
    """With a record on file, the same invoice is recognised as a duplicate."""

    orchestrator = build_orchestrator(workspace_tmp_path, "reupload2")
    first_analysis = analyze(BASE_PAYLOAD)
    from agent3.database import DocumentRepository

    DocumentRepository(orchestrator.repository.path).save(first_analysis, "hash-regression-2")

    second_analysis = analyze(BASE_PAYLOAD)
    case = orchestrator.open_case(second_analysis, "invoice.pdf")
    investigation = orchestrator.duplicates.build_investigation(case, second_analysis)

    assert investigation.records_compared >= 1
    assert investigation.status in {
        InvestigationStatus.confirmed_duplicate_invoice,
        InvestigationStatus.possible_duplicate,
    }


# ---------------------------------------------------------------------------
# A missing, unverifiable date must not block a payable invoice
# ---------------------------------------------------------------------------
def test_missing_issue_date_does_not_block_approval(workspace_tmp_path):
    """Stacked PDF layouts lose the date; the invoice is still payable.

    The reported failure: "Issue date" and its value sat on separate lines, so
    the date could not be verified and was reported missing. Treating every
    missing field as blocking sent a valid invoice to generic human review.
    """

    from workflow.orchestrator import _blocking_reasons

    orchestrator = build_orchestrator(workspace_tmp_path, "nodate")
    analysis = analyze(BASE_PAYLOAD)
    # Simulate the date being unverifiable.
    analysis.issue_date = None
    analysis.validation.missing_fields = ["issue_date"]
    analysis.requires_review = True

    assert _blocking_reasons(analysis) == []

    case = orchestrator.route(orchestrator.open_case(analysis, "stacked.pdf"))
    assert case.intent == Intent.new_payment_request
    assert case.status == CaseStatus.awaiting_approval


def test_missing_total_still_blocks_approval(workspace_tmp_path):
    """A money-relevant gap must still stop payment."""

    from workflow.orchestrator import _blocking_reasons

    analysis = analyze(BASE_PAYLOAD)
    analysis.validation.missing_fields = ["financial_information.total_amount"]
    reasons = _blocking_reasons(analysis)
    assert reasons and "total_amount" in reasons[0]


def test_missing_vendor_still_blocks_approval():
    from workflow.orchestrator import _blocking_reasons

    analysis = analyze(BASE_PAYLOAD)
    analysis.validation.missing_fields = ["vendor.vendor_name"]
    assert _blocking_reasons(analysis)
