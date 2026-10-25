"""Agent 5's plain-language answer on every case that ends.

The facts come from the agents' structured reports; an optional model may only
reword them, and a rewording that claims a settlement is discarded unless the
settlement was verified.
"""

from __future__ import annotations

from pathlib import Path

from workflow.agent5_confirmation import phrasing_is_safe
from workflow.mock_bank import MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, AgentRunStatus, CaseStatus

from test_workflow import build_analysis


VENDOR = "Northstar Office Supplies LLC"
CUSTOMER = "Blue Harbor Technologies Inc."


def build(workspace_tmp_path, name: str):
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    orchestrator = WorkflowOrchestrator(WorkflowRepository(database))
    orchestrator.simulator.bank = MockBank(database)
    orchestrator.execution.bank = orchestrator.simulator.bank
    return orchestrator


def open_case(orchestrator, invoice, amount="300.00", **analysis):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total=amount, subtotal=amount, tax="0.00", **analysis,
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


def test_success_says_paid_with_receipt(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "ok")
    paid = orchestrator.approve(open_case(orchestrator, "INV-CR-1").case_id, approved_by="admin")

    response = paid.customer_response
    assert paid.status == CaseStatus.completed
    assert response.outcome == "PAID"
    assert response.payment_settled is True
    assert "300.00 USD" in response.answer
    assert paid.receipt.receipt_id[:8] in response.answer
    assert response.next_steps
    # Persisted with the case.
    assert orchestrator.get(paid.case_id).customer_response.outcome == "PAID"


def test_failure_explains_bank_reason_and_never_claims_payment(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "fail")
    failed = orchestrator.approve(
        open_case(orchestrator, "INV-CR-2").case_id,
        approved_by="admin", simulate="FAILED", failure_code="INSUFFICIENT_FUNDS",
    )

    response = failed.customer_response
    assert failed.status == CaseStatus.requires_review
    assert response.outcome == "PAYMENT_FAILED"
    assert response.payment_settled is False
    assert "INSUFFICIENT_FUNDS" in response.answer
    assert "has not been paid" in response.answer
    assert any("retried automatically" in step for step in response.next_steps)


def test_duplicate_of_paid_invoice_is_explained(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "dup")
    orchestrator.approve(open_case(orchestrator, "INV-CR-3").case_id, approved_by="admin")

    repeat = open_case(orchestrator, "INV-CR-3")

    response = repeat.customer_response
    assert repeat.status == CaseStatus.requires_review
    assert response.outcome == "DUPLICATE_CHECKED"
    assert response.payment_settled is False
    assert "duplicate" in response.answer.lower()
    assert any("never issued automatically" in step for step in response.next_steps)


def test_status_dispute_without_settlement_does_not_claim_payment(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "dispute")
    case = open_case(
        orchestrator, "INV-CR-4", paid="300.00",
        problem="payment_status_discrepancy", primary="payment_status_dispute",
    )

    response = case.customer_response
    assert response.outcome == "PAYMENT_NOT_FOUND"
    assert response.payment_settled is False
    assert "cannot confirm it was paid" in response.answer
    assert any("transfer confirmation" in step for step in response.next_steps)


def test_status_dispute_with_settlement_reports_it_found(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "found")
    orchestrator.approve(open_case(orchestrator, "INV-CR-5").case_id, approved_by="admin")

    case = open_case(
        orchestrator, "INV-CR-5", paid="300.00",
        problem="payment_status_discrepancy", primary="payment_status_dispute",
    )

    assert case.status == CaseStatus.completed
    assert case.customer_response.outcome == "PAYMENT_FOUND"
    assert case.customer_response.payment_settled is True


def test_human_review_tells_customer_what_to_do_and_agent5_runs(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "review")
    case = open_case(orchestrator, "INV-CR-6", requires_review=True)

    path = [(r.agent.value, r.status) for r in sorted(case.agent_runs, key=lambda r: r.order)]
    assert path == [
        ("agent_3", AgentRunStatus.completed),
        ("human_review", AgentRunStatus.requires_human),
        ("agent_5", AgentRunStatus.completed),
    ]
    response = case.customer_response
    assert case.status == CaseStatus.requires_review
    assert response.outcome == "NEEDS_REVIEW"
    assert response.payment_settled is False
    assert any("review" in step.lower() for step in response.next_steps)


def test_payment_overridden_to_review_still_gets_agent5(workspace_tmp_path):
    """A payment request with failed checks goes to review; Agent 5 still answers."""

    orchestrator = build(workspace_tmp_path, "override")
    analysis = build_analysis(invoice_number="INV-CR-7", vendor=VENDOR, customer=CUSTOMER)
    analysis.contradictions = ["Two different totals are printed."]
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))

    assert case.status == CaseStatus.requires_review
    assert [r.agent.value for r in sorted(case.agent_runs, key=lambda r: r.order)][-2:] == [
        "human_review", "agent_5"
    ]
    assert "contradictions" in case.customer_response.answer


def test_unsafe_model_phrasing_is_discarded(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "phrase")
    orchestrator.execution.phraser = lambda case, facts: {
        "answer": "Good news: your payment has been paid and settled.",
        "next_steps": ["Nothing to do."],
    }
    case = open_case(
        orchestrator, "INV-CR-8", paid="300.00",
        problem="payment_status_discrepancy", primary="payment_status_dispute",
    )

    assert case.customer_response.generated_by == "deterministic"
    assert "cannot confirm" in case.customer_response.answer


def test_safe_model_phrasing_is_used_for_verified_payment(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "phrase-ok")
    orchestrator.execution.phraser = lambda case, facts: {
        "answer": "All done - your payment settled and the receipt is ready.",
    }
    paid = orchestrator.approve(open_case(orchestrator, "INV-CR-9").case_id, approved_by="admin")

    assert paid.customer_response.generated_by == "llm"
    assert paid.customer_response.outcome == "PAID"


def test_phrasing_guard():
    assert phrasing_is_safe({"answer": "Your payment has settled."}, settled=True)
    assert not phrasing_is_safe({"answer": "Your payment has settled."}, settled=False)
    assert not phrasing_is_safe({"answer": "ok", "next_steps": ["The invoice was paid."]}, settled=False)
    assert phrasing_is_safe({"answer": "We could not find the payment yet."}, settled=False)


def test_agent5_report_is_last_agent_on_terminal_paths(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "last")
    case = open_case(orchestrator, "INV-CR-10", problem="payment_status_discrepancy", primary="payment_failure")

    agents = [r.agent for r in sorted(case.agent_runs, key=lambda r: r.order)]
    assert agents[:3] == [AgentId.agent_3, AgentId.agent_4, AgentId.agent_5]
    assert case.customer_response.outcome == "FAILURE_CHECKED"
