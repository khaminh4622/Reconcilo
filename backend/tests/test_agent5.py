"""Agent 5 tests: payment execution, verification, settlement and receipts.

Every test runs against a real SQLite database and the real synthetic provider.
No LLM is involved: payment outcomes are never decided by a model.
"""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from workflow.agent1_payment_state import resolve_payment_state
from workflow.agent1_schemas import PreviousPaymentStatus
from workflow.mock_bank import MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.payment_provider import (
    ProviderStatus,
    SyntheticPaymentProvider,
    reset_provider,
)
from workflow.payment_store import PaymentRecordStore, ReceiptStore
from workflow.repository import WorkflowRepository
from workflow.schemas import (
    AgentId,
    ApprovalRecord,
    CaseStatus,
    PaymentAttempt,
    SyntheticTransaction,
)

from test_workflow import build_analysis


INVOICE = "INV-A5-TEST"
VENDOR = "Northstar Office Supplies LLC"
CUSTOMER = "Blue Harbor Technologies Inc."
AMOUNT = "1000.00"


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

def make_orchestrator(workspace_tmp_path, name: str, outcome: str = "SETTLED", **kwargs):
    """Isolated orchestrator with its own Mock Bank.

    ``outcome`` is what the Mock Bank does with a payment instruction:
    SETTLED, PROCESSING or FAILED.
    """

    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    repository = WorkflowRepository(database)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.execution.bank = MockBank(database)
    return orchestrator, database

def open_case(orchestrator, invoice=INVOICE, amount=AMOUNT):
    analysis = build_analysis(
        invoice_number=invoice,
        vendor=VENDOR,
        customer=CUSTOMER,
        total=amount,
        subtotal=amount,
        tax="0.00",
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


def approvals_in(database) -> int:
    conn = sqlite3.connect(database)
    try:
        return conn.execute("SELECT COUNT(*) FROM payment_attempts").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Valid approved invoice executes successfully
# ---------------------------------------------------------------------------
def test_approved_invoice_executes_successfully(workspace_tmp_path):
    orchestrator, database = make_orchestrator(workspace_tmp_path, "valid")
    case = open_case(orchestrator)
    assert case.status == CaseStatus.awaiting_approval

    result = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    assert_settled_after_reconciliation(result)
    payment = result.payment
    assert payment is not None
    assert payment.status == "SUCCESS"
    assert payment.provider_status == "SETTLED"
    assert payment.verified is True
    assert payment.provider_reference and payment.provider_reference.startswith("MB-")
    assert payment.settled_at is not None
    assert payment.idempotency_key


# ---------------------------------------------------------------------------
# 2. Unapproved invoice cannot execute
# ---------------------------------------------------------------------------
def test_unapproved_invoice_cannot_execute(workspace_tmp_path):
    orchestrator, database = make_orchestrator(workspace_tmp_path, "unapproved")
    case = open_case(orchestrator)
    assert case.approval is None

    result = orchestrator.execute_payment(case.case_id)

    assert result.status != CaseStatus.completed
    assert result.payment is None
    assert approvals_in(database) == 0
    report = result.last_payment_report
    assert any(f.code == "no_approval" for f in report.findings)


# ---------------------------------------------------------------------------
# 3. Already-paid invoice cannot execute again
# ---------------------------------------------------------------------------
def test_already_paid_invoice_cannot_execute_again(workspace_tmp_path):
    orchestrator, database = make_orchestrator(workspace_tmp_path, "already")
    first = open_case(orchestrator)
    orchestrator.approve(first.case_id, approved_by="alice@example.test")
    assert approvals_in(database) == 1

    # A fresh case for the same invoice, with its own approval, must not pay twice.
    second = open_case(orchestrator)
    if second.status == CaseStatus.awaiting_approval:
        second = orchestrator.approve(second.case_id, approved_by="bob@example.test")

    assert second.status != CaseStatus.completed
    assert approvals_in(database) == 1


# ---------------------------------------------------------------------------
# 4. Repeated execution returns the existing transaction
# ---------------------------------------------------------------------------
def test_repeated_execution_returns_existing_transaction(workspace_tmp_path):
    orchestrator, database = make_orchestrator(workspace_tmp_path, "repeat")
    case = open_case(orchestrator)
    done = orchestrator.approve(case.case_id, approved_by="alice@example.test")
    original_reference = done.payment.provider_reference
    original_attempt = done.payment.attempt_id

    again = orchestrator.execute_payment(case.case_id)

    report = again.last_payment_report
    assert report.data.get("executed") is False
    # The original instruction is returned rather than a second transfer.
    assert report.data["attempt"]["attempt_id"] == original_attempt
    assert report.data["attempt"]["provider_reference"] == original_reference
    assert approvals_in(database) == 1


# ---------------------------------------------------------------------------
# 5. Successful settlement updates the invoice to PAID
# ---------------------------------------------------------------------------
def test_successful_settlement_marks_invoice_paid(workspace_tmp_path):
    from workflow.obligations import ObligationRepository, obligation_key

    orchestrator, database = make_orchestrator(workspace_tmp_path, "paid")
    case = open_case(orchestrator)
    result = orchestrator.approve(case.case_id, approved_by="alice@example.test")

    assert result.payment.status == "SUCCESS"
    assert result.payment.verified is True

    obligation = ObligationRepository(database).get_obligation(
        obligation_key(INVOICE, VENDOR, "USD")
    )
    assert obligation is not None
    assert obligation["state"] == "PAID"

    # The settlement links to the Mock Bank ledger entry Agent 2 verified.
    ledger_ids = {e["entry_id"] for e in orchestrator.execution.bank.ledger()}
    assert result.payment.transaction_id in ledger_ids

    receipt = result.receipt
    assert receipt is not None
    assert receipt.synthetic is True
    assert receipt.amount == Decimal(AMOUNT)
    assert receipt.provider_reference == result.payment.provider_reference
    stored = ReceiptStore(database).get_for_case(case.case_id)
    assert stored is not None and stored.receipt_id == receipt.receipt_id


# ---------------------------------------------------------------------------
# 6. Failed payment routes to Agent 4
# ---------------------------------------------------------------------------
def test_failed_payment_routes_to_agent_4(workspace_tmp_path):
    orchestrator, _ = make_orchestrator(workspace_tmp_path, "failed", outcome="FAILED")
    case = open_case(orchestrator)
    result = orchestrator.approve(
        case.case_id, approved_by="alice@example.test", simulate="FAILED"
    )

    assert result.payment.status == "FAILED"
    assert result.payment.provider_status == "FAILED"
    assert result.payment.verified is False
    assert result.receipt is None
    assert result.status == CaseStatus.requires_review

    exception_report = next(r for r in result.reports if r.agent == AgentId.agent_4)
    # Agent 4 receives the provider response and full attempt history.
    assert exception_report.data["provider_reference"] == result.payment.provider_reference
    assert exception_report.data["provider_response"] is not None
    assert exception_report.data["attempt_history"]
    assert exception_report.data["retry_performed"] is False
    # A failed attempt is never recorded as a completed transfer.
    assert result.status != CaseStatus.completed


# ---------------------------------------------------------------------------
# 7. Pending payment does not become PAID
# ---------------------------------------------------------------------------
def test_pending_payment_is_not_paid(workspace_tmp_path):
    orchestrator, database = make_orchestrator(
        workspace_tmp_path, "pending", outcome="PENDING"
    )
    case = open_case(orchestrator)
    result = orchestrator.approve(
        case.case_id, approved_by="alice@example.test", simulate="PENDING"
    )

    assert result.payment.status == "PENDING"
    assert result.receipt is None
    assert result.status == CaseStatus.pending
    # The bank holds the transfer but has not settled it.
    assert [e["status"] for e in orchestrator.execution.bank.ledger()] == ["PENDING"]
    assert result.reconciliation["verdict"] != "MATCHED"
    assert len(orchestrator.execution.store.list_for_invoice(INVOICE)) == 1


# ---------------------------------------------------------------------------
# 8. Pending payment cannot be blindly retried
# ---------------------------------------------------------------------------
def test_pending_payment_cannot_be_retried(workspace_tmp_path):
    orchestrator, database = make_orchestrator(
        workspace_tmp_path, "pendingretry", outcome="PENDING"
    )
    case = open_case(orchestrator)
    orchestrator.approve(
        case.case_id, approved_by="alice@example.test", simulate="PENDING"
    )
    assert approvals_in(database) == 1

    retry = orchestrator.execute_payment(case.case_id)
    report = retry.last_payment_report

    assert report.data.get("executed") is False
    codes = {f.code for f in report.findings}
    assert codes & {"duplicate_execution_blocked", "pending_attempt_exists"}
    assert approvals_in(database) == 1
    # No second instruction reached the bank.
    assert len(orchestrator.execution.bank.ledger()) == 1


# ---------------------------------------------------------------------------
# 9. Payment records survive an application restart
# ---------------------------------------------------------------------------
def test_payment_records_survive_restart(workspace_tmp_path):
    orchestrator, database = make_orchestrator(workspace_tmp_path, "restart")
    case = open_case(orchestrator)
    result = orchestrator.approve(case.case_id, approved_by="alice@example.test")
    reference = result.payment.provider_reference

    # Simulate a restart: fresh repository objects over the same file.
    fresh_repository = WorkflowRepository(database)
    fresh = WorkflowOrchestrator(fresh_repository)
    store = PaymentRecordStore(database)

    attempts = store.list_for_invoice(INVOICE)
    assert len(attempts) == 1
    assert attempts[0].provider_reference == reference
    assert attempts[0].verified is True

    reloaded = fresh.get(case.case_id)
    assert reloaded is not None
    assert reloaded.payment is not None
    assert reloaded.receipt is not None
    assert_settled_after_reconciliation(reloaded)
    # The receipt is still retrievable by invoice.
    assert ReceiptStore(database).get_for_invoice(INVOICE) is not None


# ---------------------------------------------------------------------------
# 10. Agent 1 recognises an invoice paid through Agent 5
# ---------------------------------------------------------------------------
def test_agent1_sees_invoice_paid_by_agent5(workspace_tmp_path):
    orchestrator, database = make_orchestrator(workspace_tmp_path, "agent1sees")
    case = open_case(orchestrator)
    result = orchestrator.approve(case.case_id, approved_by="alice@example.test")
    assert result.payment.verified is True

    # Agent 1 compares against recorded invoice history, so the paid invoice
    # must be on file first (Agent 3 records it on analysis).
    from test_agent1 import seed_invoice_history

    seed_invoice_history(
        database,
        [
            {
                "invoice_number": INVOICE,
                "vendor": VENDOR,
                "customer": CUSTOMER,
                "amount": AMOUNT,
                "issue_date": "2026-03-01",
                "description": "Office supplies",
            }
        ],
    )

    reupload = open_case(orchestrator)
    investigations = [r for r in reupload.reports if r.agent == AgentId.agent_1]
    assert investigations, "Agent 1 should run for a duplicate submission"

    investigation = investigations[-1].data["investigation"]
    assert investigation["previous_payment_status"] == "ALREADY_PAID"
    assert investigation["blocks_payment"] is True


def test_agent1_does_not_count_failed_attempts_as_paid(workspace_tmp_path):
    orchestrator, database = make_orchestrator(workspace_tmp_path, "agent1failed", outcome="FAILED")
    case = open_case(orchestrator)
    result = orchestrator.approve(
        case.case_id, approved_by="alice@example.test", simulate="FAILED"
    )
    assert result.payment.status == "FAILED"

    # Agent 1's payment resolver must not treat a failed attempt as settlement.
    attempts = [a.model_dump(mode="json") for a in orchestrator.execution.store.list_for_invoice(INVOICE)]
    state = resolve_payment_state(
        invoice_amount=Decimal(AMOUNT),
        currency="USD",
        payment_attempts=attempts,
        bank_transactions=[],
    )
    assert state.status == PreviousPaymentStatus.payment_failed
    assert state.status != PreviousPaymentStatus.already_paid


# ---------------------------------------------------------------------------
# 11. Concurrent execution cannot create duplicate transfers
# ---------------------------------------------------------------------------
def test_concurrent_execution_creates_one_transfer(workspace_tmp_path):
    database = workspace_tmp_path / "concurrent.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    WorkflowRepository(database)
    store = PaymentRecordStore(database)

    results: list[bool] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def attempt(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            claim = store.claim(
                PaymentAttempt(
                    idempotency_key="shared-key",
                    invoice_number=INVOICE,
                    payee=VENDOR,
                    amount=Decimal(AMOUNT),
                    currency="USD",
                    case_id=f"case-{index}",
                )
            )
            results.append(claim.created)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    assert sum(1 for created in results if created) == 1, "exactly one request may execute"
    assert len(store.list_for_invoice(INVOICE)) == 1


def test_provider_refuses_second_transfer_for_same_key():
    provider = SyntheticPaymentProvider(default_outcome="SETTLED")
    first = provider.submit(
        idempotency_key="same", amount=Decimal("10.00"), currency="USD",
        payee=VENDOR, invoice_number=INVOICE,
    )
    second = provider.submit(
        idempotency_key="same", amount=Decimal("10.00"), currency="USD",
        payee=VENDOR, invoice_number=INVOICE,
    )
    assert first is second
    assert first.provider_reference == second.provider_reference


# ---------------------------------------------------------------------------
# 12. Approval for a different invoice / amount / currency / payee is rejected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("invoice_number", "INV-OTHER"),
        ("amount", Decimal("999.99")),
        ("currency", "EUR"),
        ("payee", "A Different Vendor"),
    ],
)
def test_mismatched_approval_is_rejected(workspace_tmp_path, field, value):
    orchestrator, database = make_orchestrator(workspace_tmp_path, f"mismatch-{field}")
    case = open_case(orchestrator)
    terms = {
        "approved_by": "alice@example.test",
        "invoice_number": INVOICE,
        "payee": VENDOR,
        "amount": Decimal(AMOUNT),
        "currency": "USD",
    }
    terms[field] = value
    case.approval = ApprovalRecord(**terms)

    report = orchestrator.simulator.execute(
        case,
        approval=case.approval,
        invoice_number=INVOICE,
        payee=VENDOR,
        amount=Decimal(AMOUNT),
        currency="USD",
    )
    codes = {f.code for f in report.findings}
    assert "approval_mismatch" in codes
    assert report.data.get("executed") is False
    assert approvals_in(database) == 0


def test_revoked_and_expired_approvals_are_rejected(workspace_tmp_path):
    orchestrator, database = make_orchestrator(workspace_tmp_path, "revoked")
    case = open_case(orchestrator)
    base = {
        "approved_by": "alice@example.test",
        "invoice_number": INVOICE,
        "payee": VENDOR,
        "amount": Decimal(AMOUNT),
        "currency": "USD",
    }

    case.approval = ApprovalRecord(**base, revoked=True)
    revoked = orchestrator.simulator.execute(
        case, approval=case.approval, invoice_number=INVOICE, payee=VENDOR,
        amount=Decimal(AMOUNT), currency="USD",
    )
    assert any(f.code == "approval_revoked" for f in revoked.findings)

    case.approval = ApprovalRecord(**base, expires_at="2020-01-01T00:00:00+00:00")
    expired = orchestrator.simulator.execute(
        case, approval=case.approval, invoice_number=INVOICE, payee=VENDOR,
        amount=Decimal(AMOUNT), currency="USD",
    )
    assert any(f.code == "approval_expired" for f in expired.findings)
    assert approvals_in(database) == 0


# ---------------------------------------------------------------------------
# Provider semantics: initiation is not settlement
# ---------------------------------------------------------------------------
def test_successful_submission_is_not_automatically_settled():
    """A pending instruction is not money received."""

    provider_dir = Path(__file__).resolve().parents[1] / ".test-scratch"
    provider_dir.mkdir(exist_ok=True)
    bank = MockBank(provider_dir / f"mb-pending-{id(object())}.db")
    result = bank.submit_instruction(
        idempotency_key=f"k-{id(bank)}", amount=Decimal("1.00"), currency="USD",
        payee=VENDOR, invoice_number=INVOICE, scenario="PENDING",
    )
    assert result["outcome"] == "PENDING"
    entries = bank.credits_for(result["payment_reference"])
    assert entries and entries[0]["status"] == "PENDING"


def test_pending_transaction_can_be_verified_later(workspace_tmp_path):
    orchestrator, database = make_orchestrator(
        workspace_tmp_path, "verifylater", outcome="PENDING"
    )
    case = open_case(orchestrator)
    result = orchestrator.approve(
        case.case_id, approved_by="alice@example.test", simulate="PENDING"
    )
    assert result.payment.status == "PENDING"

    # The bank eventually clears the transfer.
    settled = orchestrator.execution.bank.settle(result.payment.provider_reference)
    assert settled is not None and settled["status"] == "SETTLED"

    # Re-running reconciliation now finds the settled credit.
    from workflow.bank_reconciliation import ReconciliationService

    recommendation = ReconciliationService(database).recommendation()
    assert recommendation["verdict"] == "MATCHED"
    assert recommendation["independent_evidence"] is True


