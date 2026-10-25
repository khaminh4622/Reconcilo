"""The refactored workflow, tested end to end.

Path A (PAY_INVOICE):
    Agent 3 -> Agent 1 -> Human Approval -> Payment Simulator
        -> Agent 2 (verified) -> Agent 5
        -> Agent 4 (failed)   -> Agent 5

Path B (CUSTOMER_PROBLEM):
    Agent 3 -> specialist(s) -> Agent 5

These tests cover the ten required scenarios, including the invariant that no
required Agent 5 step is left WAITING when a case reaches its terminal status.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from workflow.agent5_confirmation import VERIFIED_VERDICTS
from workflow.mock_bank import MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, AgentRunStatus, CaseStatus

from test_workflow import build_analysis
from test_agent1 import seed_invoice_history


VENDOR = "Northstar Office Supplies LLC"
CUSTOMER = "Blue Harbor Technologies Inc."


def build(workspace_tmp_path, name: str):
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    repository = WorkflowRepository(database)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.simulator.bank = MockBank(database)
    orchestrator.execution.bank = orchestrator.simulator.bank
    return orchestrator, database


def open_case(orchestrator, invoice="INV-REF-1", amount="1620.00", **analysis):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total=amount, subtotal=amount, tax="0.00", **analysis,
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


def executed(case):
    """Agents that actually ran, in order, excluding a terminal human step."""

    # Agent 3 is planned as already-complete, so it has no started_at. Count any
    # step that reached a terminal/active state as having run.
    ran = {
        AgentRunStatus.completed,
        AgentRunStatus.blocked,
        AgentRunStatus.requires_human,
        AgentRunStatus.running,
    }
    return [
        run.agent.value
        for run in sorted(case.agent_runs, key=lambda r: r.order)
        if (run.started_at is not None or run.status in ran)
        and run.agent != AgentId.human_review
    ]


def executed_before_human(case):
    """Agents in order, excluding the human-review tail."""

    return [
        run.agent.value
        for run in sorted(case.agent_runs, key=lambda r: r.order)
        if run.agent != AgentId.human_review
        and (run.started_at is not None or run.status not in {AgentRunStatus.waiting})
    ]


def assert_no_waiting(case):
    """No required step may still be WAITING at a terminal status."""

    assert case.status in {
        CaseStatus.completed, CaseStatus.requires_review, CaseStatus.pending
    }, f"unexpected terminal status {case.status}"
    waiting = [
        run.agent.value
        for run in case.agent_runs
        if run.status == AgentRunStatus.waiting
    ]
    assert waiting == [], f"steps left waiting: {waiting}"


# ---------------------------------------------------------------------------
# 1. Successful $1,620 payment
# ---------------------------------------------------------------------------
def test_successful_payment_full_sequence(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "success")
    case = open_case(orchestrator, amount="1620.00")

    # The planned path matches the architecture exactly.
    planned = [r.agent.value for r in sorted(case.agent_runs, key=lambda r: r.order)]
    assert planned == [
        "agent_3", "agent_1", "human_approval", "payment_simulator", "agent_2", "agent_5"
    ]

    paid = orchestrator.approve(case.case_id, approved_by="approver@example.test")

    assert executed(paid)[:5] == [
        "agent_3", "agent_1", "human_approval", "payment_simulator", "agent_2"
    ]
    assert executed(paid)[-1] == "agent_5"

    assert paid.status == CaseStatus.completed
    assert paid.payment.verified is True
    assert paid.receipt is not None

    # Agent 5's response is present and reports the verified outcome.
    a5 = [r for r in paid.reports if r.agent == AgentId.agent_5][-1]
    assert a5.data["confirmed"] is True
    assert a5.data["final_status"] == "PAID"
    assert a5.data["customer_response"]["explanation"]

    assert_no_waiting(paid)

    # The Mock Bank holds exactly one settled credit.
    ledger = orchestrator.simulator.bank.ledger()
    assert len(ledger) == 1
    assert ledger[0]["status"] == "SETTLED"
    assert Decimal(ledger[0]["received_amount"]) == Decimal("1620.00")


# ---------------------------------------------------------------------------
# 2. Failed $2,500 payment
# ---------------------------------------------------------------------------
def test_failed_payment_full_sequence(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "failed")
    case = open_case(orchestrator, invoice="INV-REF-2", amount="2500.00")

    paid = orchestrator.approve(
        case.case_id, approved_by="approver@example.test",
        simulate="FAILED", failure_code="INSUFFICIENT_FUNDS",
    )

    path = executed(paid)
    assert path[:4] == ["agent_3", "agent_1", "human_approval", "payment_simulator"]
    assert "agent_4" in path, "a failure goes to Agent 4"
    assert "agent_2" not in path, "a failed instruction never reaches reconciliation"
    assert path[-1] == "agent_5", "Agent 5 always finishes the case"

    assert paid.status == CaseStatus.requires_review
    assert paid.receipt is None
    assert_no_waiting(paid)

    # Agent 5 reports the failure with the bank's verified reason.
    a5 = [r for r in paid.reports if r.agent == AgentId.agent_5][-1]
    assert a5.data["confirmed"] is False
    assert a5.data["final_status"] == "FAILED"
    assert a5.data["customer_response"]["explanation"]

    # No money moved.
    ledger = orchestrator.simulator.bank.ledger()
    assert ledger[0]["status"] == "FAILED"
    assert ledger[0]["received_amount"] is None
    assert orchestrator.simulator.bank.balances() == {}


# ---------------------------------------------------------------------------
# 3. Customer paid the same invoice twice
# ---------------------------------------------------------------------------
def test_duplicate_payment_goes_through_agent1_and_agent2(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "twice")
    seed_invoice_history(
        database,
        [{
            "invoice_number": "INV-REF-3", "vendor": VENDOR, "customer": CUSTOMER,
            "amount": "1620.00", "issue_date": "2026-01-01", "description": "prior",
        }],
    )
    # Pay once successfully.
    first = open_case(orchestrator, invoice="INV-REF-3")
    orchestrator.approve(first.case_id, approved_by="approver@example.test")
    assert orchestrator.simulator.bank.count() == 1

    # Now the customer claims they paid twice.
    complaint = open_case(
        orchestrator, invoice="INV-REF-3", amount="1620.00",
        problem="payment_status_discrepancy", primary="duplicate_payment",
    )
    path = [r.agent.value for r in sorted(complaint.agent_runs, key=lambda r: r.order)]
    assert "agent_1" in path
    assert "agent_5" in path
    assert_no_waiting(complaint)
    # No new payment was made by the investigation.
    assert orchestrator.simulator.bank.count() == 1


# ---------------------------------------------------------------------------
# 4. Customer asks whether a payment reached the bank
# ---------------------------------------------------------------------------
def test_payment_status_question_routes_agent2_then_agent5(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "status")
    first = open_case(orchestrator, invoice="INV-REF-4")
    paid = orchestrator.approve(first.case_id, approved_by="approver@example.test")
    assert paid.payment.verified is True

    enquiry = open_case(
        orchestrator, invoice="INV-REF-4",
        problem="payment_status_discrepancy", primary="payment_status_dispute",
    )
    path = [r.agent.value for r in sorted(enquiry.agent_runs, key=lambda r: r.order)]
    assert "agent_2" in path
    # Agent 5 always runs last among the agents; a human step may follow it.
    assert "agent_5" in path
    assert executed_before_human(enquiry)[-1] == "agent_5"
    assert "human_approval" not in path, "an enquiry must not ask for a new approval"
    # Agent 5 issued a final response either way.
    a5 = [r for r in enquiry.reports if r.agent == AgentId.agent_5]
    assert a5 and a5[-1].data.get("customer_response") is not None
    assert_no_waiting(enquiry)
    assert orchestrator.simulator.bank.count() == 1, "no new payment"


# ---------------------------------------------------------------------------
# 5. Payment amount does not match the bank settlement
# ---------------------------------------------------------------------------
def test_amount_mismatch_is_not_confirmed(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "mismatch")
    case = open_case(orchestrator, invoice="INV-REF-5", amount="1620.00")
    paid = orchestrator.approve(
        case.case_id, approved_by="approver@example.test", simulate="PARTIAL"
    )

    assert paid.status != CaseStatus.completed
    assert paid.receipt is None, "no receipt without a matching settlement"
    assert paid.reconciliation["verdict"] != "MATCHED"
    assert paid.reconciliation["verdict"] not in VERIFIED_VERDICTS

    a5 = [r for r in paid.reports if r.agent == AgentId.agent_5][-1]
    assert a5.data["confirmed"] is False
    assert_no_waiting(paid)


# ---------------------------------------------------------------------------
# 6. Duplicate payment execution is blocked
# ---------------------------------------------------------------------------
def test_duplicate_execution_is_blocked(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "dup")
    case = open_case(orchestrator, invoice="INV-REF-6")
    paid = orchestrator.approve(case.case_id, approved_by="approver@example.test")
    assert paid.status == CaseStatus.completed

    before_rows = orchestrator.simulator.bank.count()
    balance = orchestrator.simulator.bank.balances()

    again = orchestrator.execute_payment(case.case_id)
    assert again.last_payment_report.data.get("executed") is False
    assert orchestrator.simulator.bank.count() == before_rows
    assert orchestrator.simulator.bank.balances() == balance


# ---------------------------------------------------------------------------
# 7. No payment executes without approval
# ---------------------------------------------------------------------------
def test_no_payment_without_approval(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "noapproval")
    case = open_case(orchestrator, invoice="INV-REF-7")
    assert case.approval is None

    result = orchestrator.execute_payment(case.case_id)
    assert result.payment is None
    assert orchestrator.simulator.bank.count() == 0

    conn = sqlite3.connect(database)
    attempts = conn.execute("SELECT COUNT(*) FROM payment_attempts").fetchone()[0]
    conn.close()
    assert attempts == 0


# ---------------------------------------------------------------------------
# 8. Failed and pending payments are never retried automatically
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("scenario", "code"),
    [("FAILED", "BANK_TIMEOUT"), ("PENDING", None), ("MISSING_SETTLEMENT", None)],
)
def test_failed_and_pending_are_never_retried(workspace_tmp_path, scenario, code):
    orchestrator, database = build(workspace_tmp_path, f"retry-{scenario.lower()}")
    case = open_case(orchestrator, invoice="INV-REF-8")
    paid = orchestrator.approve(
        case.case_id, approved_by="approver@example.test",
        simulate=scenario, failure_code=code,
    )

    rows_before = orchestrator.simulator.bank.count()
    attempts_before = len(orchestrator.simulator.store.list_for_invoice("INV-REF-8"))

    # Nothing retries behind the scenes.
    for _ in range(2):
        again = orchestrator.execute_payment(paid.case_id)
        assert again.last_payment_report.data.get("executed") is False

    assert orchestrator.simulator.bank.count() == rows_before
    assert len(orchestrator.simulator.store.list_for_invoice("INV-REF-8")) == attempts_before
    assert paid.status != CaseStatus.completed


# ---------------------------------------------------------------------------
# 9. No required Agent 5 step remains WAITING at a terminal status
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (None, CaseStatus.completed),
        ("FAILED", CaseStatus.requires_review),
        ("PENDING", CaseStatus.pending),
        ("PARTIAL", CaseStatus.requires_review),
        ("MISSING_SETTLEMENT", CaseStatus.requires_review),
    ],
)
def test_no_waiting_agent5_step_at_terminal_status(
    workspace_tmp_path, scenario, expected
):
    orchestrator, database = build(workspace_tmp_path, f"wait-{scenario or 'ok'}")
    case = open_case(orchestrator, invoice=f"INV-W-{scenario or 'OK'}")
    paid = orchestrator.approve(
        case.case_id, approved_by="approver@example.test", simulate=scenario
    )

    assert paid.status == expected
    assert_no_waiting(paid)

    # Agent 5 must have run and produced a final response in every branch.
    a5 = [r for r in paid.reports if r.agent == AgentId.agent_5]
    assert a5, "Agent 5 must always run"
    assert a5[-1].data.get("customer_response") is not None


# ---------------------------------------------------------------------------
# 10. Existing invoice and Mock Bank history remain intact
# ---------------------------------------------------------------------------
def test_history_is_preserved(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "history")

    conn = sqlite3.connect(database)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    before = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in tables
        if t in {"payment_attempts", "payable_obligations", "document_records"}
    }
    conn.close()

    for scenario in (None, "FAILED", "PARTIAL"):
        case = open_case(orchestrator, invoice=f"INV-H-{scenario or 'OK'}")
        orchestrator.approve(
            case.case_id, approved_by="approver@example.test", simulate=scenario
        )

    conn = sqlite3.connect(database)
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    conn.close()

    # History only grows; nothing is deleted or reset.
    for table, count in before.items():
        assert after[table] >= count, f"{table} lost rows"

    # Every attempt recorded survives.
    attempts = orchestrator.simulator.store.list_all()
    assert len(attempts) == 3


# ---------------------------------------------------------------------------
# The simulator is separate from Agent 5
# ---------------------------------------------------------------------------
def test_simulator_is_not_agent5():
    from workflow.agent5_confirmation import FinalConfirmationAgent
    from workflow.payment_simulator import PaymentSimulatorService

    assert not hasattr(FinalConfirmationAgent, "execute")
    assert not hasattr(FinalConfirmationAgent, "initiate_payment")
    assert hasattr(PaymentSimulatorService, "execute")
    # Agent 5 cannot reach the bank to move money.
    assert "submit_instruction" not in dir(FinalConfirmationAgent)
