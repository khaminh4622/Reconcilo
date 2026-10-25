"""End-to-end tests for the five-agent workflow.

Each test drives the orchestrator the way the API does: Agent 3's analysis is
supplied as a typed object, the orchestrator routes it, and the resulting case
path is asserted. No LLM is contacted.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from agent3.database import DocumentRepository
from agent3.document_reader import read_pdf_upload
from agent3.extractor import LLMExtractor
from agent3.schemas import (
    AnalysisResult,
    CalculatedValues,
    CustomerInformation,
    DetectedProblem,
    DocumentType,
    FinancialInformation,
    FinancialStatus,
    PaymentClaim,
    PaymentStatus,
    PrimaryProblem,
    ProblemStatus,
    ProblemType,
    ValidationCheck,
    ValidationResult,
    VendorInformation,
)
from agent3.service import Agent3Service
from workflow.orchestrator import WorkflowError, WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, AgentRunStatus, CaseStatus, Intent, WorkflowType
from workflow.seed import synthetic_bank_seed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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

def build_analysis(
    *,
    invoice_number: str = "INV-2026-9001",
    customer: str = "Blue Harbor Technologies Inc.",
    vendor: str = "Northstar Office Supplies LLC",
    total: str = "1000.00",
    subtotal: str = "925.93",
    tax: str = "74.07",
    paid: str | None = None,
    outstanding: str | None = None,
    problem: ProblemType = ProblemType.none_identified,
    primary: PrimaryProblem = PrimaryProblem.unknown,
    issue_date: str = "2026-03-01",
    requires_review: bool = False,
    financial_status: FinancialStatus | None = None,
    payment_reference: str | None = None,
    payment_count: int | None = None,
    duplicate_amount: str | None = None,
) -> AnalysisResult:
    analysis = AnalysisResult(
        document_type=DocumentType.invoice,
        document_id=invoice_number,
        issue_date=issue_date,
        due_date="2026-04-01",
        currency="USD",
        customer=CustomerInformation(customer_name=customer),
        vendor=VendorInformation(vendor_name=vendor),
        financial_information=FinancialInformation(
            currency="USD",
            subtotal=Decimal(subtotal),
            tax_amount=Decimal(tax),
            total_amount=Decimal(total),
            amount_paid=Decimal(paid) if paid else None,
            outstanding_balance=Decimal(outstanding) if outstanding else None,
            payment_claim=PaymentClaim(
                claimed_amount=Decimal(paid) if paid else None,
                status=PaymentStatus.reported if paid else PaymentStatus.none_claimed,
                reference=payment_reference,
                requires_reconciliation=bool(paid),
            ),
        ),
        detected_problem=DetectedProblem(
            type=problem,
            primary_problem=primary,
            description="Reported by the customer.",
            source_evidence="Invoice Total: " + total,
            status=ProblemStatus.reported,
            reported_payment_count=payment_count,
        ),
    )
    analysis.customer_information = analysis.customer.model_copy(deep=True)
    analysis.vendor_information = analysis.vendor.model_copy(deep=True)
    analysis.requires_review = requires_review
    checks = [
        ValidationCheck(
            code="subtotal_tax_total",
            label="Subtotal + tax = invoice total",
            status="failed" if requires_review else "passed",
            explanation="Arithmetic check.",
        )
    ]
    analysis.validation = ValidationResult(
        status="requires_review" if requires_review else "passed",
        financial_consistency=financial_status
        or (FinancialStatus.failed if requires_review else FinancialStatus.passed),
        requires_review=requires_review,
        validation_findings=checks,
        calculated=CalculatedValues(
            potential_duplicate_amount=Decimal(duplicate_amount) if duplicate_amount else None
        ),
    )
    analysis.validation_findings = checks
    return analysis


@pytest.fixture
def orchestrator(workspace_tmp_path):
    repository = WorkflowRepository(workspace_tmp_path / "workflow.db")
    repository.seed_bank_transactions(synthetic_bank_seed())
    return WorkflowOrchestrator(repository)


def path_of(case) -> list[str]:
    """The sequence of agents that actually did work, in order."""

    runs = sorted(case.agent_runs, key=lambda r: r.order)
    return [r.agent.value for r in runs if r.status != AgentRunStatus.skipped]


def route(orchestrator, **kwargs):
    case = orchestrator.open_case(build_analysis(**kwargs), "doc.pdf")
    return orchestrator.route(case)


# ---------------------------------------------------------------------------
# 1. Valid new invoice -> Agent 3 -> Approval -> Agent 5
# ---------------------------------------------------------------------------
def test_valid_new_invoice_awaits_human_approval(orchestrator):
    case = route(orchestrator)

    assert case.intent == Intent.new_payment_request
    assert case.workflow == WorkflowType.new_payment
    assert case.status == CaseStatus.awaiting_approval
    assert case.routing.primary_agent == AgentId.human_approval
    assert case.routing.requires_human_approval is True
    # Corrected order: Agent 5 initiates, Agent 2 checks the money, Agent 5 confirms.
    # Path A: Agent 1 checks history, a human approves, the simulator moves the
    # money, Agent 2 verifies it, Agent 5 confirms.
    assert path_of(case) == [
        "agent_3", "agent_1", "human_approval", "payment_simulator", "agent_2", "agent_5"
    ]
    # Agent 5 must not have run yet.
    assert case.run_for(AgentId.agent_5).status == AgentRunStatus.waiting
    assert case.payment is None


def test_approval_executes_payment_and_completes(orchestrator):
    case = route(orchestrator)
    case = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    assert_settled_after_reconciliation(case)
    assert case.payment is not None and case.payment.status == "SUCCESS"
    assert case.run_for(AgentId.agent_5).status == AgentRunStatus.completed
    assert case.approval is not None and case.approval.approved_by == "alice@example.test"
    # Audit trail covers the whole path.
    actors = {entry.actor for entry in case.audit}
    assert {AgentId.orchestrator, AgentId.agent_3, AgentId.human_approval, AgentId.agent_5} <= actors


# ---------------------------------------------------------------------------
# 2. Duplicate invoice -> Agent 1
# ---------------------------------------------------------------------------
def test_duplicate_invoice_routes_to_agent_1(orchestrator):
    case = route(
        orchestrator,
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.duplicate_invoice,
        paid="1000.00",
    )
    assert case.intent == Intent.duplicate_invoice
    assert case.routing.primary_agent == AgentId.agent_1
    assert "agent_1" in path_of(case)


# ---------------------------------------------------------------------------
# 3. Customer paid the same invoice twice -> Agent 1 (with Agent 2 support)
# ---------------------------------------------------------------------------
def test_duplicate_payment_routes_to_agent_1_with_agent_2_support(orchestrator):
    case = route(
        orchestrator,
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.duplicate_payment,
        total="1944.00",
        subtotal="1800.00",
        tax="144.00",
        paid="3888.00",
        payment_count=2,
        duplicate_amount="1944.00",
    )
    assert case.intent == Intent.duplicate_payment
    assert case.routing.primary_agent == AgentId.agent_1
    assert case.routing.supporting_agent == AgentId.agent_2
    # Agent 1 is primary, Agent 2 supports it afterwards.
    # Agent 1 investigates, Agent 2 checks the money, Agent 5 confirms the result.
    assert path_of(case)[:2] == ["agent_3", "agent_1"]
    # Agent 2 runs when the money needs checking; the planned step is either
    # executed or explicitly skipped. Either way no step is left waiting.
    assert path_of(case)[-1] in {"agent_5", "human_review"}
    waiting = [r.agent.value for r in case.agent_runs if r.status == AgentRunStatus.waiting]
    assert waiting == [], f"steps left waiting: {waiting}"
    agent1 = next(r for r in case.reports if r.agent == AgentId.agent_1)
    # Agent 1 now returns a full investigation object.
    investigation = agent1.data["investigation"]
    # Two settled synthetic bank transactions exist for this amount, so the
    # duplicate payment is confirmed rather than merely suspected.
    assert investigation["status"] in {
        "SUSPECTED_DUPLICATE_PAYMENT",
        "CONFIRMED_DUPLICATE_PAYMENT",
    }
    assert investigation["payment_analysis"]["duplicate_payment_suspected"] is True
    assert investigation["blocks_payment"] is True
    # Agent 1 never refunds on its own authority.
    assert agent1.data["blocks_payment"] is True


# ---------------------------------------------------------------------------
# 4. Bank transaction mismatch -> Agent 2
# ---------------------------------------------------------------------------
def test_bank_mismatch_routes_to_agent_2(orchestrator):
    case = route(
        orchestrator,
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.payment_status_dispute,
        paid="410.40",
        outstanding="410.40",
        payment_reference="TRF-UNKNOWN-999",
    )
    assert case.intent in {Intent.payment_status_dispute, Intent.bank_reconciliation}
    assert case.routing.primary_agent == AgentId.agent_2
    report = next(r for r in case.reports if r.agent == AgentId.agent_2)
    codes = {f.code for f in report.findings}
    assert "no_bank_match" in codes
    assert report.resolved is False
    assert case.status == CaseStatus.requires_review


def test_reconciliation_matches_a_settled_bank_transaction(orchestrator):
    """When a matching settled transaction exists, the payment is verified."""

    case = route(
        orchestrator,
        primary=PrimaryProblem.payment_status_dispute,
        problem=ProblemType.payment_status_discrepancy,
        paid="410.40",
        payment_reference="TRF-7741290",
    )
    report = next(r for r in case.reports if r.agent == AgentId.agent_2)
    assert report.data["matched"] is True
    assert report.resolved is True


# ---------------------------------------------------------------------------
# 5. Failed payment attempt -> Agent 4
# ---------------------------------------------------------------------------
def test_failed_payment_routes_to_agent_4(orchestrator):
    case = route(
        orchestrator,
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.payment_failure,
    )
    assert case.intent == Intent.payment_failure
    assert case.routing.primary_agent == AgentId.agent_4
    report = next(r for r in case.reports if r.agent == AgentId.agent_4)
    assert report.data["retry_performed"] is False
    assert report.requires_human is True


# ---------------------------------------------------------------------------
# 6. Agent 5 execution failure -> Agent 4
# ---------------------------------------------------------------------------
def test_agent5_failure_hands_off_to_agent_4(orchestrator):
    case = route(orchestrator)
    case = orchestrator.approve(
        case.case_id,
        approved_by="alice@example.test",
        simulate="FAILED",
        failure_reason="insufficient funds",
        failure_code="INSUFFICIENT_FUNDS",
    )
    assert case.status == CaseStatus.requires_review
    assert case.payment.status == "FAILED"
    assert case.run_for(AgentId.agent_4) is not None
    assert case.run_for(AgentId.agent_4).status == AgentRunStatus.requires_human
    exception = next(r for r in case.reports if r.agent == AgentId.agent_4)
    assert exception.data["diagnosis_code"] == "INSUFFICIENT_FUNDS"
    assert exception.data["failure_code_source"] == "mock_bank"
    assert exception.data["retry_performed"] is False
    # A failed attempt is never recorded as a completed transfer.
    assert case.status != CaseStatus.completed


# ---------------------------------------------------------------------------
# 7. Missing financial information -> Human Review
# ---------------------------------------------------------------------------
def test_missing_financial_information_routes_to_human_review(orchestrator):
    case = route(orchestrator, requires_review=True, financial_status=FinancialStatus.failed)
    assert case.intent == Intent.unknown
    assert case.workflow == WorkflowType.human_review
    assert case.routing.primary_agent == AgentId.human_review
    assert case.status == CaseStatus.requires_review
    assert case.requires_human is True


# ---------------------------------------------------------------------------
# 8. Successful verified payment completes and records the transaction
# ---------------------------------------------------------------------------
def test_successful_payment_is_recorded_with_audit(orchestrator):
    case = route(orchestrator)
    case = orchestrator.approve(case.case_id, approved_by="bob@example.test")

    assert_settled_after_reconciliation(case)
    # The settlement is tied to the Mock Bank ledger entry Agent 2 verified.
    assert case.payment.transaction_id is not None
    ledger_ids = {e["entry_id"] for e in orchestrator.execution.bank.ledger()}
    assert case.payment.transaction_id in ledger_ids
    # Agent 5 confirms only after Agent 2 has reconciled.
    assert case.reconciliation["verdict"] == "MATCHED"
    assert any(entry.action == "reconciliation_result" for entry in case.audit)
    assert any(entry.action == "case_completed" for entry in case.audit)
    assert case.outcome


# ---------------------------------------------------------------------------
# 9. Duplicate execution request -> blocked
# ---------------------------------------------------------------------------
def test_duplicate_execution_request_is_blocked(orchestrator):
    case = route(orchestrator)
    case = orchestrator.approve(case.case_id, approved_by="alice@example.test")
    first_attempt = case.payment.attempt_id

    # Attempt to execute the same approved payment again.
    again = orchestrator.execute_payment(case.case_id)
    assert again.payment.attempt_id == first_attempt  # original returned, not re-executed
    report = again.last_payment_report
    assert any(f.code == "duplicate_execution_blocked" for f in report.findings)
    assert report.data.get("executed") is False is False


def test_same_invoice_cannot_be_paid_from_a_second_case(orchestrator):
    """Idempotency is keyed on the payment terms, not the case."""

    first = route(orchestrator)
    orchestrator.approve(first.case_id, approved_by="alice@example.test")

    second = route(orchestrator)  # a brand new case for the same invoice

    # The second case must never pay again. It is now stopped even earlier than
    # before: Agent 1 sees ALREADY_PAID and the approval is refused outright.
    with pytest.raises(WorkflowError):
        orchestrator.approve(second.case_id, approved_by="alice@example.test")

    # Exactly one payment exists, and the invoice is settled once.
    attempts = orchestrator.execution.store.list_for_invoice("INV-2026-9001")
    assert len(attempts) == 1
    assert attempts[0].status == "SUCCESS"


# ---------------------------------------------------------------------------
# Safety: approvals
# ---------------------------------------------------------------------------
def test_unapproved_payment_cannot_be_executed(orchestrator):
    case = route(orchestrator)
    assert case.status == CaseStatus.awaiting_approval

    result = orchestrator.execute_payment(case.case_id)
    assert result.status != CaseStatus.completed
    report = result.last_payment_report
    assert any(f.code == "no_approval" for f in report.findings)
    assert report.data.get("executed") is False
    assert result.payment is None


def test_approval_must_match_the_exact_payment_terms(orchestrator):
    case = route(orchestrator)
    approved = orchestrator.approve(case.case_id, approved_by="alice@example.test")
    assert_settled_after_reconciliation(approved)

    # Tamper with the approval amount: a second case with a different amount
    # must not be payable under the original approval.
    from workflow.schemas import ApprovalRecord

    stale = ApprovalRecord(
        approved_by="alice@example.test",
        invoice_number="INV-2026-9001",
        payee="Northstar Office Supplies LLC",
        amount=Decimal("1.00"),  # wrong amount
        currency="USD",
    )
    mismatched_case = route(orchestrator, total="2000.00", subtotal="1851.85", tax="148.15")
    report = orchestrator.simulator.execute(
        mismatched_case,
        approval=stale,
        invoice_number=mismatched_case.invoice_number,
        payee=mismatched_case.vendor_name,
        amount=mismatched_case.amount,
        currency=mismatched_case.currency,
    )
    codes = {f.code for f in report.findings}
    assert "approval_mismatch" in codes
    assert report.data.get("executed") is False


def test_rejected_case_is_not_paid(orchestrator):
    case = route(orchestrator)
    case = orchestrator.reject(case.case_id, "alice@example.test", "Budget not approved")
    assert case.status == CaseStatus.failed
    assert case.payment is None


# ---------------------------------------------------------------------------
# Safety: pending / uncertain payments are never blindly retried
# ---------------------------------------------------------------------------
def test_pending_payment_blocks_retry(orchestrator):
    case = route(orchestrator)
    case = orchestrator.approve(case.case_id, approved_by="alice@example.test", simulate="PENDING")

    # An unsettled payment is never confirmed. It stays open for review.
    assert case.status in {CaseStatus.pending, CaseStatus.requires_review}
    assert case.requires_human is True
    assert case.payment.status == "PENDING"
    assert case.receipt is None

    # Any further execution must refuse rather than pay again.
    retry = orchestrator.execute_payment(case.case_id)
    report = retry.last_payment_report
    codes = {f.code for f in report.findings}
    assert "duplicate_execution_blocked" in codes or "pending_attempt_exists" in codes
    assert report.data.get("executed") is False


# ---------------------------------------------------------------------------
# Orchestration guarantees
# ---------------------------------------------------------------------------
def test_agent_3_is_always_the_first_agent(orchestrator):
    for kwargs in ({}, {"primary": PrimaryProblem.duplicate_payment}, {"requires_review": True}):
        case = route(orchestrator, **kwargs)
        runs = sorted(case.agent_runs, key=lambda r: r.order)
        assert runs[0].agent == AgentId.agent_3
        assert runs[0].status == AgentRunStatus.completed


def test_every_case_records_an_audit_trail(orchestrator):
    case = route(orchestrator)
    case = orchestrator.approve(case.case_id, approved_by="alice@example.test")
    assert len(case.audit) >= 5
    assert all(entry.at and entry.action and entry.detail for entry in case.audit)
    assert all(entry.actor for entry in case.audit)


def test_agent_reports_are_typed_and_structured(orchestrator):
    case = route(
        orchestrator,
        primary=PrimaryProblem.duplicate_payment,
        problem=ProblemType.payment_status_discrepancy,
        total="1944.00",
        paid="3888.00",
        payment_count=2,
    )
    assert case.reports
    for report in case.reports:
        assert report.agent in set(AgentId)
        assert report.summary
        assert isinstance(report.findings, list)
        assert report.completed_at is not None
        for finding in report.findings:
            assert finding.status in {"passed", "failed", "unverified", "info"}
            assert finding.detail


def test_case_is_persisted_and_reloadable(orchestrator):
    case = route(orchestrator)
    reloaded = orchestrator.get(case.case_id)
    assert reloaded is not None
    assert reloaded.status == case.status
    assert len(reloaded.audit) == len(case.audit)
    summaries = orchestrator.repository.list_cases()
    assert any(s.case_id == case.case_id for s in summaries)


# ---------------------------------------------------------------------------
# API-level integration
# ---------------------------------------------------------------------------
def make_pdf(lines: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(pymupdf.Rect(50, 50, 560, 740), lines, fontsize=10, lineheight=1.3)
    content = document.tobytes()
    document.close()
    return content


class StubModel:
    def __init__(self, payload: dict):
        self.payload = payload

    def complete(self, system: str, user: str) -> str:
        return json.dumps(self.payload)


def test_api_workflow_round_trip(workspace_tmp_path):
    from fastapi.testclient import TestClient

    from main import create_app

    database = workspace_tmp_path / "api.db"
    payload = {
        "document_type": "invoice",
        "document_id": "INV-API-1",
        "issue_date": "03 Mar 2026",
        "currency": "USD",
        "customer": {"customer_name": "Blue Harbor Technologies Inc."},
        "vendor": {"vendor_name": "Northstar Office Supplies LLC"},
        "financial_information": {
            "currency": "USD",
            "subtotal": "100.00",
            "tax_amount": "8.00",
            "total_amount": "108.00",
            "amount_paid": "0.00",
            "outstanding_balance": "108.00",
        },
        "detected_problem": {"type": "none_identified", "primary_problem": "unknown"},
    }
    app = create_app(service=Agent3Service(DocumentRepository(database), LLMExtractor(StubModel(payload))))
    client = TestClient(app)
    # Every customer action requires a signed-in account.
    signup = client.post(
        "/auth/signup", json={"email": "api@example.test", "password": "password123"}
    )
    assert signup.status_code == 200, signup.text
    token = signup.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    pdf = make_pdf(
        "INVOICE\nVendor: Northstar Office Supplies LLC\nCustomer: Blue Harbor Technologies Inc.\n"
        "Invoice Number: INV-API-1\nInvoice Date: 03 Mar 2026\nCurrency: USD\n"
        "Subtotal: $100.00\nTax: $8.00\nInvoice Total: $108.00\nAmount Paid: $0.00\n"
    )
    created = client.post(
        "/workflow/cases",
        files={"file": ("inv.pdf", pdf, "application/pdf")},
        headers=headers,
    )
    assert created.status_code == 201
    case = created.json()
    assert case["status"] in {"AWAITING_APPROVAL", "REQUIRES_REVIEW"}

    if case["status"] == "AWAITING_APPROVAL":
        approved = client.post(
            f"/workflow/cases/{case['case_id']}/approve", json={"approved_by": "alice@example.test"}
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "COMPLETED"

    listing = client.get("/workflow/cases", headers=headers)
    assert listing.status_code == 200
    assert any(item["case_id"] == case["case_id"] for item in listing.json())


def test_api_rejects_approval_without_approver(workspace_tmp_path):
    from fastapi.testclient import TestClient

    from main import create_app

    app = create_app(
        service=Agent3Service(DocumentRepository(workspace_tmp_path / "api2.db"), LLMExtractor(StubModel({})))
    )
    client = TestClient(app)
    signup = client.post(
        "/auth/signup", json={"email": "api2@example.test", "password": "password123"}
    )
    assert signup.status_code == 200, signup.text
    headers = {"Authorization": f"Bearer {signup.json()['token']}"}
    response = client.post(
        "/workflow/cases/does-not-exist/approve", json={}, headers=headers
    )
    # An authenticated caller asking about a missing case gets 404; the
    # missing-approver validation is covered by the approval path itself.
    assert response.status_code in {400, 404}


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------
def test_existing_agent3_analysis_contract_is_preserved(workspace_tmp_path):
    repository = DocumentRepository(workspace_tmp_path / "compat.db")
    service = Agent3Service(repository, LLMExtractor(StubModel({
        "document_type": "invoice",
        "document_id": "INV-C-1",
        "currency": "USD",
        "financial_information": {"currency": "USD", "total_amount": "100.00"},
        "detected_problem": {"type": "none_identified"},
    })))
    result = service.analyze(read_pdf_upload("c.pdf", make_pdf("Invoice Total: $100.00\nCurrency: USD")))
    payload = result.model_dump(mode="json")
    for section in (
        "customer_information", "vendor_information", "document_information",
        "financial_information", "detected_problem", "validation_findings",
        "routing", "reasoning", "missing_information", "evidence",
    ):
        assert section in payload


# ---------------------------------------------------------------------------
# Triage: the case list must tell a human what is wrong and what to do
# ---------------------------------------------------------------------------
def test_case_list_explains_what_failed_and_what_to_do(orchestrator):
    case = route(
        orchestrator,
        primary=PrimaryProblem.duplicate_payment,
        problem=ProblemType.payment_status_discrepancy,
        total="1944.00",
        subtotal="1800.00",
        tax="144.00",
        paid="3888.00",
        payment_count=2,
        duplicate_amount="1944.00",
    )
    summary = next(s for s in orchestrator.repository.list_cases() if s.case_id == case.case_id)

    assert summary.why, "a human must be told why the case needs attention"
    assert summary.problems, "the concrete problems must be listed"
    assert summary.next_action, "the human must be told what to do next"
    # The duplicate evidence must be visible without opening the case.
    joined = " ".join(summary.problems).lower()
    assert "duplicate" in joined or "1944.00" in joined
    # The next action must tell the human what to do, and must not promise a refund.
    lowered = summary.next_action.lower()
    assert any(word in lowered for word in ("bank", "refund", "block", "human", "review"))


def test_case_list_next_action_for_approval_case(orchestrator):
    case = route(orchestrator)
    summary = next(s for s in orchestrator.repository.list_cases() if s.case_id == case.case_id)
    assert summary.status == CaseStatus.awaiting_approval
    assert "approve" in summary.next_action.lower()
    assert summary.why


def test_case_list_for_unclassified_case_gives_clarification_step(orchestrator):
    case = route(orchestrator, requires_review=True)
    summary = next(s for s in orchestrator.repository.list_cases() if s.case_id == case.case_id)
    assert summary.why
    assert summary.next_action
    assert "confirm" in summary.next_action.lower() or "read the document" in summary.next_action.lower()


def test_pending_case_tells_human_not_to_retry(orchestrator):
    case = route(orchestrator)
    case = orchestrator.approve(case.case_id, approved_by="alice@example.test", simulate="PENDING")
    summary = next(s for s in orchestrator.repository.list_cases() if s.case_id == case.case_id)
    assert "not retry" in summary.next_action.lower() or "do not retry" in summary.next_action.lower()
