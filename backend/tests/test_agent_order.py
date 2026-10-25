"""Tests pinning the corrected agent order.

    AGENT 2 CHECKS THE MONEY.
    AGENT 5 CONFIRMS THE FINAL RESULT.

The rules enforced here:

* Agent 5 initiates a payment *before* Agent 2 runs, but never confirms it then.
* Agent 2 reconciles the instruction against the Mock Bank ledger.
* Agent 5 confirms the final status only when Agent 2 reports MATCHED with
  independent evidence.
* An existing-payment request routes Agent 3 straight to Agent 2 when Agent 1
  is unnecessary.
* Agent 5 remains the final visible agent.

All transactions are fictional.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from workflow.agent5_execution import BLOCKING_VERDICTS, CONFIRMABLE_VERDICTS
from workflow.mock_bank import BankEntry, MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, CaseStatus

from test_workflow import build_analysis
from test_agent1 import seed_invoice_history


VENDOR = "Northstar Office Supplies LLC"
CUSTOMER = "Blue Harbor Technologies Inc."


def build(workspace_tmp_path, name: str, invoice: str = "INV-ORDER-1"):
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    repository = WorkflowRepository(database)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.execution.bank = MockBank(database)
    return orchestrator, database


def open_case(orchestrator, invoice="INV-ORDER-1", amount="1250.00", **analysis_kwargs):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total=amount, subtotal=amount, tax="0.00", **analysis_kwargs,
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


def path_of(case):
    return [run.agent.value for run in sorted(case.agent_runs, key=lambda r: r.order)]


def executed_path(case):
    """Agents that actually did something, in order."""

    # Agent 3 is planned as already complete, so also treat completed runs as
    # executed.
    return [
        run.agent.value
        for run in sorted(case.agent_runs, key=lambda r: r.order)
        if run.started_at is not None or run.status.value in {"completed", "requires_human"}
    ]


# ---------------------------------------------------------------------------
# 1. The planned timeline shows the corrected order
# ---------------------------------------------------------------------------
def test_new_payment_timeline_is_a5_then_a2_then_a5(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "timeline")
    case = open_case(orchestrator)

    assert path_of(case) == [
        "agent_3", "agent_1", "human_approval", "payment_simulator", "agent_2", "agent_5"
    ]
    # Agent 5 is the final visible agent.
    assert path_of(case)[-1] == "agent_5"

    # Agent 5 appears once, as the final confirmation step.
    agents5 = [run for run in case.agent_runs if run.agent == AgentId.agent_5]
    assert len(agents5) == 1
    assert agents5[0].phase == "confirmation"
    assert any(run.agent == AgentId.payment_simulator for run in case.agent_runs)


def test_simulator_runs_before_agent2(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "order")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    order = executed_path(paid)
    assert order.index("payment_simulator") < order.index("agent_2")


def test_agent5_confirmation_happens_after_agent2(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "confirm")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    runs = sorted(paid.agent_runs, key=lambda r: r.order)
    initiation = next(r for r in runs if r.agent == AgentId.payment_simulator)
    confirmation = next(r for r in runs if r.agent == AgentId.agent_5)
    reconciliation = next(r for r in runs if r.agent == AgentId.agent_2)

    assert initiation.completed_at is not None
    assert initiation.order < reconciliation.order < confirmation.order


# ---------------------------------------------------------------------------
# 2. Agent 5 cannot confirm before Agent 2 has verified
# ---------------------------------------------------------------------------
def test_initiation_does_not_mark_paid(workspace_tmp_path):
    """After initiation alone, the invoice must not be PAID."""

    from workflow.obligations import ObligationRepository, obligation_key

    orchestrator, database = build(workspace_tmp_path, "initonly")
    case = open_case(orchestrator)

    report = orchestrator.simulator.execute(
        case,
        approval=None,  # no approval -> nothing happens
        invoice_number=case.invoice_number, payee=case.vendor_name,
        amount=case.amount, currency=case.currency,
    )
    assert report.data.get("executed") is False

    obligation = ObligationRepository(database).get_obligation(
        obligation_key(case.invoice_number, VENDOR, "USD")
    )
    assert obligation is None or obligation["state"] != "PAID"


def test_confirm_refuses_when_agent2_did_not_match(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "refuse")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    # Re-confirm with a non-matching verdict: Agent 5 must refuse.
    report = orchestrator.execution.confirm_payment(
        paid,
        {"verdict": "PARTIAL_PAYMENT", "independent_evidence": False},
        approval=paid.approval,
    )
    assert report.data["confirmed"] is False
    assert report.data["final_status"] != "PAID"
    assert any(
        f.code in {"reconciliation_not_confirmed", "settlement_not_verified"}
        for f in report.findings
    )


def test_confirm_refuses_matched_without_independent_evidence(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "noevidence")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    report = orchestrator.execution.confirm_payment(
        paid,
        {"verdict": "MATCHED", "independent_evidence": False},
        approval=paid.approval,
    )
    assert report.data["confirmed"] is False
    assert any(f.code == "unverified_evidence" for f in report.findings)


@pytest.mark.parametrize(
    "verdict",
    sorted(BLOCKING_VERDICTS),
)
def test_blocking_verdicts_never_confirm(workspace_tmp_path, verdict):
    orchestrator, _ = build(workspace_tmp_path, f"blk-{verdict}")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    report = orchestrator.execution.confirm_payment(
        paid, {"verdict": verdict, "independent_evidence": True}, approval=paid.approval
    )
    assert report.resolved is False
    assert report.data["confirmed"] is False


def test_only_matched_verdicts_are_confirmable():
    assert CONFIRMABLE_VERDICTS == {"MATCHED", "SETTLEMENT_VERIFIED"}
    assert CONFIRMABLE_VERDICTS.isdisjoint(BLOCKING_VERDICTS)


# ---------------------------------------------------------------------------
# 3. The Mock Bank is independent of Agent 5
# ---------------------------------------------------------------------------
def test_mock_bank_is_a_separate_ledger(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "sepbanks")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    conn = sqlite3.connect(database)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "mock_bank_ledger" in tables
    # The bank holds its own record, keyed by its own reference.
    entries = orchestrator.execution.bank.ledger()
    assert len(entries) == 1
    assert entries[0]["payment_reference"] == paid.payment.provider_reference
    assert entries[0]["status"] == "SETTLED"


def test_agent2_reads_the_mock_bank_ledger(workspace_tmp_path):
    from workflow.bank_reconciliation import ReconciliationService

    orchestrator, database = build(workspace_tmp_path, "readbank")
    case = open_case(orchestrator)
    orchestrator.approve(case.case_id, approved_by="alice@example.test")

    service = ReconciliationService(database)
    recommendation = service.recommendation()
    assert recommendation["verdict"] == "MATCHED"
    assert recommendation["independent_evidence"] is True


def test_pending_bank_entry_is_not_matched(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "pendingbank")
    case = open_case(orchestrator)
    paid = orchestrator.approve(
        case.case_id, approved_by="alice@example.test", simulate="PENDING"
    )

    assert paid.reconciliation["verdict"] != "MATCHED"
    assert paid.payment.status == "PENDING"
    assert paid.receipt is None
    assert paid.status == CaseStatus.pending


def test_partial_bank_credit_is_not_confirmed(workspace_tmp_path):
    """The bank settles less than expected, so Agent 5 must not confirm."""

    orchestrator, database = build(workspace_tmp_path, "partialbank")
    case = open_case(orchestrator)
    paid = orchestrator.approve(
        case.case_id, approved_by="alice@example.test", simulate="PARTIAL"
    )

    from workflow.bank_reconciliation import ReconciliationService

    recommendation = ReconciliationService(database).recommendation()
    assert recommendation["verdict"] == "PARTIAL_PAYMENT"
    report = orchestrator.execution.confirm_payment(paid, recommendation, paid.approval)
    assert report.data["confirmed"] is False


def test_missing_bank_credit_is_not_confirmed(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "nobank")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    conn = sqlite3.connect(database)
    conn.execute("DELETE FROM mock_bank_ledger")
    conn.commit()
    conn.close()

    from workflow.bank_reconciliation import ReconciliationService

    recommendation = ReconciliationService(database).recommendation()
    assert recommendation["verdict"] != "MATCHED"
    report = orchestrator.execution.confirm_payment(paid, recommendation, paid.approval)
    assert report.data["confirmed"] is False


# ---------------------------------------------------------------------------
# 4. Existing-payment requests route A3 -> A2 without Agent 1
# ---------------------------------------------------------------------------
def test_existing_payment_routes_direct_to_agent2(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "direct")
    case = open_case(
        orchestrator,
        problem="payment_status_discrepancy",
        primary="bank_reconciliation",
    )

    assert case.intent.value == "bank_reconciliation"
    agents = path_of(case)
    # Agent 3 routes straight to Agent 2, then Agent 5 explains the outcome.
    assert agents[:3] == ["agent_3", "agent_2", "agent_5"]
    assert "agent_1" not in agents
    assert "human_approval" not in agents, "an enquiry needs no approval"


def test_duplicate_route_still_investigates_first(workspace_tmp_path):
    """A duplicate claim is investigated by Agent 1 before any money moves."""

    orchestrator, database = build(workspace_tmp_path, "duproute")
    seed_invoice_history(
        database,
        [{
            "invoice_number": "INV-ORDER-1", "vendor": VENDOR, "customer": CUSTOMER,
            "amount": "1250.00", "issue_date": "2026-01-01", "description": "prior",
        }],
    )
    case = open_case(
        orchestrator,
        problem="payment_status_discrepancy",
        primary="duplicate_invoice",
    )

    agents = path_of(case)
    # Agent 1 investigates first; Agent 2 follows to check the money.
    assert agents[:2] == ["agent_3", "agent_1"]
    assert "agent_2" in agents
    # Whatever the outcome, no payment is made without a fresh approval.
    assert case.approval is None
    assert orchestrator.execution.bank.count() == 0


# ---------------------------------------------------------------------------
# 5. Human approval is still required and still precedes execution
# ---------------------------------------------------------------------------
def test_no_payment_without_approval(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "noapproval")
    case = open_case(orchestrator)
    assert case.approval is None

    result = orchestrator.execute_payment(case.case_id)
    assert result.payment is None
    assert orchestrator.execution.bank.count() == 0

    conn = sqlite3.connect(database)
    assert conn.execute("SELECT COUNT(*) FROM payment_attempts").fetchone()[0] == 0
    conn.close()


def test_approval_precedes_execution(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "aprorder")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    runs = sorted(paid.agent_runs, key=lambda r: r.order)
    approval_run = next(r for r in runs if r.agent == AgentId.human_approval)
    initiation = next(r for r in runs if r.agent == AgentId.payment_simulator)
    assert approval_run.order < initiation.order
    assert paid.approval is not None


# ---------------------------------------------------------------------------
# 6. The whole corrected pipeline
# ---------------------------------------------------------------------------
def test_full_pipeline_confirms_and_issues_receipt(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "pipeline")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    assert executed_path(paid)[:4] == [
        "agent_3", "agent_1", "human_approval", "payment_simulator"
    ]
    executed = executed_path(paid)
    assert executed.index("agent_2") < executed.index("agent_5")
    assert executed[-1] in {"agent_5", "human_review"}
    assert paid.status == CaseStatus.completed
    assert paid.payment.verified is True
    assert paid.receipt is not None
    assert paid.receipt.synthetic is True
    assert paid.reconciliation["verdict"] == "MATCHED"

    actions = [entry.action for entry in paid.audit]
    assert "reconciliation_result" in actions
    assert actions.index("reconciliation_result") < actions.index("receipt_issued")


def test_receipt_records_the_reconciliation_proof(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "receiptproof")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    verification = " ".join(paid.receipt.verification)
    assert "Agent 2 reconciled" in verification
    assert "Agent 5 issued this final confirmation" in verification
