"""Agent 1 reports before a payment case can be approved.

The approver must decide with Agent 1's duplicate and payment-history check in
hand, so the check runs while the case is routed, not inside ``approve()``.
``approve()`` keeps a read-only re-check as defence in depth.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from workflow.mock_bank import MockBank
from workflow.orchestrator import WorkflowError, WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, AgentRunStatus, CaseStatus, SyntheticTransaction

from test_agent1 import seed_invoice_history
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
    return orchestrator, database


def open_case(orchestrator, invoice="INV-PRE-1", amount="640.00", **analysis):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total=amount, subtotal=amount, tax="0.00", **analysis,
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


def test_agent1_has_reported_when_case_awaits_approval(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "reported")
    case = open_case(orchestrator)

    assert case.status == CaseStatus.awaiting_approval
    agent1 = case.run_for(AgentId.agent_1)
    assert agent1.status == AgentRunStatus.completed
    assert agent1.completed_at is not None
    assert any(report.agent == AgentId.agent_1 for report in case.reports)

    # In the audit trail Agent 1 finishes before the case is put up for approval.
    actions = [(entry.actor, entry.action) for entry in case.audit]
    finished = actions.index((AgentId.agent_1, "agent_completed"))
    awaiting = actions.index((AgentId.orchestrator, "awaiting_approval"))
    assert finished < awaiting


def test_already_paid_invoice_is_blocked_before_approval(workspace_tmp_path):
    """The invoice is on record and the bank already shows the money settled."""

    orchestrator, database = build(workspace_tmp_path, "paidout")
    seed_invoice_history(
        database,
        [{
            "invoice_number": "INV-PRE-PAID", "vendor": VENDOR, "customer": CUSTOMER,
            "amount": "640.00", "issue_date": "2026-03-01", "description": "prior",
        }],
    )
    orchestrator.repository.seed_bank_transactions([
        SyntheticTransaction(
            reference="PAID-OUTSIDE-1", amount=Decimal("640.00"), currency="USD",
            direction="credit", counterparty=VENDOR, status="settled", value_date="2026-03-05",
        )
    ])
    case = open_case(orchestrator, invoice="INV-PRE-PAID")

    assert case.status == CaseStatus.requires_review
    assert case.run_for(AgentId.agent_1).status == AgentRunStatus.blocked
    assert case.run_for(AgentId.human_approval).status == AgentRunStatus.skipped
    assert "already paid" in case.pending_reason.lower()
    assert any(entry.action == "blocked_before_approval" for entry in case.audit)
    # Agent 5 still answers, and nothing became approvable.
    assert case.run_for(AgentId.agent_5).status == AgentRunStatus.completed
    assert case.customer_response.outcome == "BLOCKED_DUPLICATE"
    with pytest.raises(WorkflowError):
        orchestrator.approve(case.case_id, approved_by="admin@example.test")
    assert orchestrator.simulator.bank.count() == 0


def test_repeat_of_unpaid_invoice_stays_payable_once(workspace_tmp_path):
    """Existing rule kept: an unpaid repeat reuses the obligation and pays once."""

    orchestrator, database = build(workspace_tmp_path, "unpaid")
    seed_invoice_history(
        database,
        [{
            "invoice_number": "INV-PRE-OPEN", "vendor": VENDOR, "customer": CUSTOMER,
            "amount": "640.00", "issue_date": "2026-03-01", "description": "prior",
        }],
    )
    case = open_case(orchestrator, invoice="INV-PRE-OPEN")

    assert case.status == CaseStatus.awaiting_approval
    report = next(r for r in case.reports if r.agent == AgentId.agent_1)
    assert report.data["investigation"]["previous_payment_status"] == "UNPAID_ELIGIBLE_FOR_APPROVAL"
    assert orchestrator.approve(case.case_id, approved_by="admin").status == CaseStatus.completed
    assert orchestrator.simulator.bank.count() == 1


def test_approve_rechecks_for_money_moved_since_agent1(workspace_tmp_path):
    """Two cases for one invoice both await approval; only the first may pay."""

    orchestrator, _ = build(workspace_tmp_path, "recheck")
    first = open_case(orchestrator, invoice="INV-PRE-RACE")
    second = open_case(orchestrator, invoice="INV-PRE-RACE")
    assert first.status == second.status == CaseStatus.awaiting_approval

    paid = orchestrator.approve(first.case_id, approved_by="admin@example.test")
    assert paid.status == CaseStatus.completed

    again = orchestrator.approve(second.case_id, approved_by="admin@example.test")
    assert again.status == CaseStatus.requires_review
    assert again.approval is None
    assert "cannot be approved again" in again.pending_reason
    assert orchestrator.simulator.bank.count() == 1


def test_legacy_case_without_agent1_report_still_gets_the_check(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "legacy")
    case = open_case(orchestrator, invoice="INV-PRE-LEGACY")
    # Simulate a case stored before Agent 1 moved into routing.
    case.reports = [r for r in case.reports if r.agent != AgentId.agent_1]
    case.run_for(AgentId.agent_1).status = AgentRunStatus.waiting
    orchestrator.repository.save_case(case)

    paid = orchestrator.approve(case.case_id, approved_by="admin@example.test")

    assert any(report.agent == AgentId.agent_1 for report in paid.reports)
    assert paid.status == CaseStatus.completed


def test_approval_records_the_approver_account(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "approver")
    case = open_case(orchestrator, invoice="INV-PRE-WHO")

    paid = orchestrator.approve(
        case.case_id, approved_by="Bank Admin (acct-123)", approver_account_id="acct-123"
    )

    assert paid.approval.approved_by_account_id == "acct-123"
    stored = orchestrator.repository.get_case(case.case_id)
    entry = next(e for e in stored.audit if e.action == "approved")
    assert entry.actor_account_id == "acct-123"
    # Still bound to invoice, payee and amount.
    assert paid.approval.invoice_number == "INV-PRE-WHO"
    assert paid.approval.payee == VENDOR
    assert str(paid.approval.amount) == "640.00"


def test_rejection_records_the_account_and_closes_with_agent5(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "reject")
    case = open_case(orchestrator, invoice="INV-PRE-NO")

    rejected = orchestrator.reject(
        case.case_id, "Bank Admin (acct-9)", "Vendor bank details changed.", rejector_account_id="acct-9"
    )

    assert rejected.status == CaseStatus.failed
    assert rejected.requires_human is False
    entry = next(e for e in rejected.audit if e.action == "rejected")
    assert entry.actor_account_id == "acct-9"
    assert rejected.run_for(AgentId.payment_simulator).status == AgentRunStatus.skipped
    assert rejected.run_for(AgentId.agent_5).status == AgentRunStatus.completed
    assert rejected.customer_response.outcome == "REJECTED"
    # The customer sees the reason but never the approver's identity.
    assert "Vendor bank details changed" in rejected.customer_response.answer
    assert "acct-9" not in rejected.customer_response.answer
    assert orchestrator.simulator.bank.count() == 0


@pytest.mark.parametrize(
    ("simulate", "code", "expected"),
    [(None, None, CaseStatus.completed), ("FAILED", "BANK_TIMEOUT", CaseStatus.requires_review),
     ("PENDING", None, CaseStatus.pending)],
)
def test_reexecuting_a_finished_case_keeps_its_outcome(workspace_tmp_path, simulate, code, expected):
    """Regression: re-running execute on a COMPLETED case flipped it to PENDING."""

    orchestrator, _ = build(workspace_tmp_path, f"reexec-{simulate}")
    case = open_case(orchestrator, invoice=f"INV-PRE-RE-{simulate}")
    done = orchestrator.approve(case.case_id, approved_by="admin", simulate=simulate, failure_code=code)
    assert done.status == expected
    outcome = done.customer_response.outcome if done.customer_response else None
    rows = orchestrator.simulator.bank.count()

    again = orchestrator.execute_payment(case.case_id)

    assert again.last_payment_report.data.get("executed") is False
    assert again.status == expected
    assert (again.customer_response.outcome if again.customer_response else None) == outcome
    assert (again.receipt is not None) == (done.receipt is not None)
    assert orchestrator.simulator.bank.count() == rows
    assert any(e.action == "execution_blocked" for e in again.audit)
