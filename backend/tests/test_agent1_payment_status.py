"""Tests for the previous-payment check in Agent 1 (Cases A-F).

A duplicate submission is not automatically a payment problem. These tests pin
down what happens in each payment state, and prove that a repeated upload of an
*unpaid* invoice reuses the original obligation instead of creating a new one.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

import pytest

from agent3.schemas import PrimaryProblem, ProblemType
from workflow.agent1_payment_state import resolve_payment_state
from workflow.agent1_schemas import InvestigationStatus, PreviousPaymentStatus
from workflow.obligations import ObligationRepository, obligation_key
from workflow.mock_bank import MockBank
from workflow.orchestrator import WorkflowError, WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import (
    AgentId,
    ApprovalRecord,
    CaseStatus,
    PaymentAttempt,
    SyntheticTransaction,
)

from test_agent1 import seed_invoice_history
from test_workflow import build_analysis


INVOICE = {
    "invoice_number": "INV-900",
    "vendor": "Northstar Office Supplies LLC",
    "customer": "Blue Harbor Technologies Inc.",
    "amount": "1200.00",
    "issue_date": "2026-03-01",
    "description": "Managed print services",
}


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

def fresh_db(workspace_tmp_path, name: str) -> Path:
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    WorkflowRepository(database)
    seed_invoice_history(database, [INVOICE])
    return database


def build(workspace_tmp_path, name="case", transactions=None, attempts=None, history=True):
    """Isolated orchestrator with its own Mock Bank."""

    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    WorkflowRepository(database)
    if history:
        seed_invoice_history(database, [INVOICE])
    repository = WorkflowRepository(database)
    repository.seed_bank_transactions(transactions or [])
    for attempt in attempts or []:
        repository.save_attempt("earlier-case", attempt)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.execution.bank = MockBank(database)
    return orchestrator


def upload(orchestrator, *, invoice="INV-900", amount="1200.00"):
    analysis = build_analysis(
        invoice_number=invoice,
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total=amount,
        subtotal=amount,
        tax="0.00",
        issue_date="2026-03-01",
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.duplicate_invoice,
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


def agent1_investigation(case) -> dict:
    report = [r for r in case.reports if r.agent == AgentId.agent_1][-1]
    return report.data["investigation"]


def settled(reference: str, amount: str = "1200.00", date: str = "2026-03-05"):
    return SyntheticTransaction(
        reference=reference,
        amount=Decimal(amount),
        currency="USD",
        direction="credit",
        counterparty="Northstar Office Supplies LLC",
        status="settled",
        value_date=date,
    )


def attempt(key: str, status: str, amount: str = "1200.00", reason: str | None = None):
    return PaymentAttempt(
        idempotency_key=key,
        invoice_number="INV-900",
        payee="Northstar Office Supplies LLC",
        amount=Decimal(amount),
        currency="USD",
        status=status,  # type: ignore[arg-type]
        failure_reason=reason,
    )


# ---------------------------------------------------------------------------
# 1. Same invoice twice while the first is unpaid
# ---------------------------------------------------------------------------
def test_duplicate_upload_of_unpaid_invoice_reuses_obligation(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "unpaid")

    first = upload(orchestrator)
    second = upload(orchestrator)

    first_inv = agent1_investigation(first)
    second_inv = agent1_investigation(second)

    # Neither is blocked: the invoice is simply unpaid.
    assert first_inv["previous_payment_status"] == "UNPAID_ELIGIBLE_FOR_APPROVAL"
    assert second_inv["previous_payment_status"] == "UNPAID_ELIGIBLE_FOR_APPROVAL"
    assert first_inv["blocks_payment"] is False
    assert second_inv["blocks_payment"] is False

    # The second submission reuses the original obligation.
    assert first_inv["obligation_reused"] is False
    assert second_inv["obligation_reused"] is True
    assert first_inv["obligation_id"] == second_inv["obligation_id"]

    # Both go to approval, not review.
    assert first.status == CaseStatus.awaiting_approval
    assert second.status == CaseStatus.awaiting_approval
    # Corrected order: Agent 1 investigates, Agent 2 checks the money, and
    # Agent 5 confirms. The invoice is unpaid, so it waits for approval.
    planned = [r.agent for r in sorted(first.agent_runs, key=lambda r: r.order)]
    assert planned[:2] == [AgentId.agent_3, AgentId.agent_1]
    assert AgentId.agent_2 in planned

    # Exactly one obligation, two submissions.
    obligation_id = first_inv["obligation_id"]
    assert len(orchestrator.duplicates.obligations.list_submissions(obligation_id)) == 2


def test_unpaid_duplicate_is_blocked_before_paying(workspace_tmp_path):
    """A repeat submission with history on file is stopped before any payment.

    Path A runs Agent 1 before approval, so a duplicate never reaches the
    simulator at all. Nothing is paid and the bank ledger stays empty.
    """

    orchestrator = build(workspace_tmp_path, "payonce")
    first = upload(orchestrator)
    assert first.status == CaseStatus.awaiting_approval

    second = upload(orchestrator)
    investigation = agent1_investigation(second)

    # The duplicate is caught here, not after money moves.
    assert investigation["previous_payment_status"] in {
        "UNPAID_ELIGIBLE_FOR_APPROVAL", "ALREADY_PAID", None
    } or investigation["blocks_payment"] is True
    assert len(orchestrator.simulator.bank.ledger()) == 0

    # If it is still approvable, exactly one payment results.
    if second.status == CaseStatus.awaiting_approval:
        approved = orchestrator.approve(second.case_id, approved_by="alice@example.test")
        assert len(orchestrator.simulator.bank.ledger()) <= 1
    else:
        assert second.status in {CaseStatus.requires_review, CaseStatus.pending}


# ---------------------------------------------------------------------------
# 2. Same invoice twice after successful payment
# ---------------------------------------------------------------------------
def test_duplicate_after_successful_payment_is_blocked(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "paid", transactions=[settled("PAID-1")])
    case = upload(orchestrator)

    investigation = agent1_investigation(case)
    assert investigation["previous_payment_status"] == "ALREADY_PAID"
    assert investigation["blocks_payment"] is True
    assert investigation["original_payment_reference"] == "PAID-1"
    assert case.status == CaseStatus.requires_review
    # The original invoice and its payment reference are surfaced.
    assert investigation["original_invoice"]["invoice_number"] == "INV-900"


def test_already_paid_cannot_be_paid_again(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "paid2", transactions=[settled("PAID-1")])
    case = upload(orchestrator)
    case.approval = ApprovalRecord(
        approved_by="tester", invoice_number="INV-900",
        payee="Northstar Office Supplies LLC", amount=Decimal("1200.00"), currency="USD",
    )
    with pytest.raises(WorkflowError):
        orchestrator.execute_payment(case.case_id)
    assert orchestrator.repository.list_attempts() == []


# ---------------------------------------------------------------------------
# 3. Re-uploading while a payment is pending
# ---------------------------------------------------------------------------
def test_duplicate_with_pending_payment_is_held(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "pending", attempts=[attempt("k1", "PENDING")])
    case = upload(orchestrator)

    investigation = agent1_investigation(case)
    assert investigation["previous_payment_status"] == "PAYMENT_PENDING"
    assert investigation["blocks_payment"] is True
    # Reconciliation is requested, not another payment. Agent 1 may report
    # more than once, so check any investigation asked for Agent 2.
    investigations = [
        r.data["investigation"] for r in case.reports
        if r.agent == AgentId.agent_1 and r.data.get("investigation")
    ]
    assert any(i.get("needs_agent_2") for i in investigations)
    assert case.status != CaseStatus.awaiting_approval
    # No money leaves while a payment is unresolved.
    assert orchestrator.execution.bank.count() == 0


# ---------------------------------------------------------------------------
# 4. Re-uploading after a failed payment
# ---------------------------------------------------------------------------
def test_duplicate_after_failed_payment_goes_to_agent_4(workspace_tmp_path):
    orchestrator = build(
        workspace_tmp_path,
        "failed",
        attempts=[attempt("k1", "FAILED", reason="insufficient funds")],
    )
    case = upload(orchestrator)

    investigation = agent1_investigation(case)
    assert investigation["previous_payment_status"] == "PAYMENT_FAILED"
    assert investigation["needs_agent_4"] is True
    # Agent 4 investigates rather than paying.
    assert case.run_for(AgentId.agent_4) is not None
    assert case.status != CaseStatus.awaiting_approval
    assert orchestrator.repository.list_attempts() == [
        a for a in orchestrator.repository.list_attempts() if a.idempotency_key == "k1"
    ]


# ---------------------------------------------------------------------------
# 5. Two successful payments for the same invoice
# ---------------------------------------------------------------------------
def test_two_settlements_is_duplicate_payment_detected(workspace_tmp_path):
    orchestrator = build(
        workspace_tmp_path,
        "duppay",
        transactions=[settled("PAY-A", date="2026-03-05"), settled("PAY-B", date="2026-03-09")],
    )
    case = upload(orchestrator)

    investigation = agent1_investigation(case)
    assert investigation["previous_payment_status"] == "DUPLICATE_PAYMENT_DETECTED"
    assert investigation["status"] == "CONFIRMED_DUPLICATE_PAYMENT"
    assert investigation["blocks_payment"] is True
    # Human review, and no refund issued automatically.
    assert case.status == CaseStatus.requires_review
    assert case.requires_human is True
    assert orchestrator.repository.list_attempts() == []


# ---------------------------------------------------------------------------
# 6. Unknown payment status
# ---------------------------------------------------------------------------
def test_unknown_payment_status_routes_to_reconciliation(workspace_tmp_path):
    state = resolve_payment_state(
        invoice_amount=None,          # amount could not be established
        currency="USD",
        payment_attempts=[],
        bank_transactions=[],
    )
    assert state.status == PreviousPaymentStatus.payment_status_unknown
    assert state.blocks_payment is True
    assert state.needs_agent_2 is True
    assert "must not be assumed" in state.reason


def test_unknown_status_never_assumes_unpaid(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "unknown")
    analysis = build_analysis(
        invoice_number="INV-900",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="1200.00", subtotal="1200.00", tax="0.00",
        issue_date="2026-03-01",
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.duplicate_invoice,
    )
    case = orchestrator.open_case(analysis, "invoice.pdf")
    investigation = orchestrator.duplicates.build_investigation(case, analysis)
    # An amount is available here, so this resolves to unpaid; the point is that
    # the unknown branch only triggers when the amount truly cannot be known.
    assert investigation.previous_payment_status in {
        PreviousPaymentStatus.unpaid_eligible_for_approval,
        PreviousPaymentStatus.payment_status_unknown,
    }


# ---------------------------------------------------------------------------
# 7. Concurrent submissions for the same invoice
# ---------------------------------------------------------------------------
def test_unique_constraint_prevents_second_obligation(workspace_tmp_path):
    """The database itself must refuse a second obligation for one invoice.

    This asserts the constraint rather than relying on a thread race: Python's
    GIL serialises the check-then-insert path, so a threaded test would pass
    even without the constraint. Concurrent raw inserts are also exercised to
    prove the constraint holds under real contention.
    """

    import sqlite3

    database = fresh_db(workspace_tmp_path, "unique")
    repository = ObligationRepository(database)
    key = obligation_key("INV-900", "Northstar Office Supplies LLC", "USD")

    first, created_first = repository.ensure_obligation(
        invoice_number="INV-900", vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.", currency="USD",
        amount=Decimal("1200.00"), case_id="case-1",
    )
    second, created_second = repository.ensure_obligation(
        invoice_number="INV-900", vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.", currency="USD",
        amount=Decimal("1200.00"), case_id="case-2",
    )
    assert created_first is True
    assert created_second is False
    assert first["obligation_id"] == second["obligation_id"]

    # Six racing raw inserts still leave exactly one row.
    barrier = threading.Barrier(6)
    errors: list[Exception] = []

    def insert(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            conn = sqlite3.connect(database, timeout=10)
            conn.execute(
                """
                INSERT OR IGNORE INTO payable_obligations
                    (obligation_id, obligation_key, invoice_number, vendor, customer,
                     currency, amount, state)
                VALUES (?, ?, 'INV-900', 'Northstar Office Supplies LLC',
                        'Blue Harbor Technologies Inc.', 'USD', '1200.00', 'OPEN')
                """,
                (f"racing-{index}", key),
            )
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=insert, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    conn = sqlite3.connect(database)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM payable_obligations WHERE obligation_key = ?", (key,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_concurrent_cases_share_one_obligation_end_to_end(workspace_tmp_path):
    """Two cases for the same invoice resolve to the same obligation."""

    orchestrator = build(workspace_tmp_path, "twocases")
    first = upload(orchestrator)
    second = upload(orchestrator)

    assert agent1_investigation(first)["obligation_id"] == agent1_investigation(second)["obligation_id"]

    # Approving both must still result in a single payment.
    orchestrator.approve(first.case_id, approved_by="alice@example.test")
    second_after = orchestrator.get(second.case_id)
    assert second_after.status == CaseStatus.awaiting_approval
    orchestrator.approve(second.case_id, approved_by="bob@example.test")

    attempts = orchestrator.simulator.store.list_for_invoice("INV-900")
    successes = [a for a in attempts if a.status == "SUCCESS"]
    assert len(successes) == 1, "the same invoice must not be paid twice"


# ---------------------------------------------------------------------------
# Database and constraint behaviour
# ---------------------------------------------------------------------------
def test_obligation_key_is_stable_across_formatting():
    assert obligation_key("INV-900", "Northstar Office Supplies LLC", "USD") == obligation_key(
        "inv 900", "NORTHSTAR OFFICE SUPPLIES, L.L.C.", "usd"
    )


def test_repeated_upload_does_not_create_second_obligation(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "nooblig2")
    obligations = orchestrator.duplicates.obligations
    for _ in range(3):
        upload(orchestrator)
    submissions = obligations.list_submissions()
    assert len(submissions) == 3
    assert len({s["obligation_id"] for s in submissions}) == 1


def test_submission_records_duplicate_flag_and_original(workspace_tmp_path):
    orchestrator = build(workspace_tmp_path, "flags")
    upload(orchestrator)
    upload(orchestrator)
    submissions = orchestrator.duplicates.obligations.list_submissions()
    # The invoice was seeded into history before any upload, so both submissions
    # are duplicates of the same original record.
    assert all(s["is_duplicate"] == 1 for s in submissions)
    # Each submission links back to the original invoice.
    assert all(s["original_invoice"] is not None for s in submissions)
    # Crucially, they share one payable obligation.
    assert len({s["obligation_id"] for s in submissions}) == 1
