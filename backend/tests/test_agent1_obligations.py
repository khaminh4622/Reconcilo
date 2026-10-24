"""Regression tests: obligation reuse and settlement classification.

Two guarantees are pinned here:

1. Uploading the same invoice again must never create a second payable
   obligation. It links to the original invoice and reuses the existing one.
2. A completed payment attempt is not a verified settlement. Two payments may
   only be called settled when both have trusted settlement evidence from the
   bank - never because GhostLedger recorded two successful executions.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from workflow.agent1_history import normalize_invoice_number
from workflow.agent1_payment_state import resolve_payment_state
from workflow.agent1_schemas import PreviousPaymentStatus
from workflow.obligations import ObligationRepository, obligation_key
from workflow.orchestrator import WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import PaymentAttempt, SyntheticTransaction

from test_agent1 import seed_invoice_history
from test_workflow import build_analysis


INVOICE = {
    "invoice_number": "INV-2026-5814",
    "vendor": "Silverline Network Solutions LLC",
    "customer": "Redwood Learning Studio LLC",
    "amount": "918.00",
    "issue_date": "2026-10-19",
    "description": "Network installation and configuration",
}


def build(workspace_tmp_path, name, *, transactions=None, attempts=None, history=True):
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
        repository.save_attempt("earlier", attempt)
    return WorkflowOrchestrator(repository), database


def upload(orchestrator, amount="918.00"):
    analysis = build_analysis(
        invoice_number=INVOICE["invoice_number"],
        vendor=INVOICE["vendor"],
        customer=INVOICE["customer"],
        total=amount,
        subtotal=amount,
        tax="0.00",
        issue_date=INVOICE["issue_date"],
        problem="payment_status_discrepancy",
        primary="duplicate_invoice",
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


def investigation_of(case) -> dict:
    from workflow.schemas import AgentId

    return [r for r in case.reports if r.agent == AgentId.agent_1][-1].data["investigation"]


def count_obligations(database) -> int:
    conn = sqlite3.connect(database)
    try:
        return conn.execute("SELECT COUNT(*) FROM payable_obligations").fetchone()[0]
    finally:
        conn.close()


def settled(reference: str, amount: str = "918.00", date: str = "2026-10-20"):
    return SyntheticTransaction(
        reference=reference,
        amount=Decimal(amount),
        currency="USD",
        direction="credit",
        counterparty=INVOICE["vendor"],
        status="settled",
        value_date=date,
    )


def success_attempt(key: str, amount: str = "918.00"):
    return PaymentAttempt(
        idempotency_key=key,
        invoice_number=INVOICE["invoice_number"],
        payee=INVOICE["vendor"],
        amount=Decimal(amount),
        currency="USD",
        status="SUCCESS",
    )


# ---------------------------------------------------------------------------
# 1. Obligation reuse
# ---------------------------------------------------------------------------
def test_duplicate_upload_reuses_the_same_obligation(workspace_tmp_path):
    """Uploading twice must leave exactly one payable obligation."""

    orchestrator, database = build(workspace_tmp_path, "reuse")

    first = upload(orchestrator)
    second = upload(orchestrator)

    first_inv = investigation_of(first)
    second_inv = investigation_of(second)

    # Same obligation, and the database holds exactly one row.
    assert first_inv["obligation_id"] == second_inv["obligation_id"]
    assert count_obligations(database) == 1
    assert second_inv["obligation_reused"] is True


def test_eight_uploads_still_create_one_obligation(workspace_tmp_path):
    """The reported scenario: an invoice re-uploaded many times."""

    orchestrator, database = build(workspace_tmp_path, "many")
    obligation_ids = set()
    for _ in range(8):
        obligation_ids.add(investigation_of(upload(orchestrator))["obligation_id"])

    assert len(obligation_ids) == 1
    assert count_obligations(database) == 1

    obligations = ObligationRepository(database)
    submissions = obligations.list_submissions(next(iter(obligation_ids)))
    assert len(submissions) == 8
    # The invoice was already on record before the first upload (the history
    # seed), so every submission is a duplicate of it - including the first.
    assert all(s["is_duplicate"] == 1 for s in submissions)
    # Whatever the flag says, there is still only one payable obligation.
    assert count_obligations(database) == 1


def test_duplicate_submission_links_back_to_the_original_invoice(workspace_tmp_path):
    orchestrator, _ = build(workspace_tmp_path, "link")
    upload(orchestrator)
    second = investigation_of(upload(orchestrator))

    assert second["submission_is_duplicate"] is True
    original = second["original_invoice"]
    assert original is not None
    assert normalize_invoice_number(original["invoice_number"]) == normalize_invoice_number(
        INVOICE["invoice_number"]
    )


def test_first_submission_for_an_existing_invoice_is_flagged_duplicate(workspace_tmp_path):
    """The reported bug: the invoice was already on record but read "newly created".

    The invoice record existed before any obligation did, so a submission that
    merely creates the obligation is still a duplicate submission.
    """

    orchestrator, database = build(workspace_tmp_path, "flagbug")
    case = upload(orchestrator)
    investigation = investigation_of(case)

    # A first-time obligation is created...
    assert investigation["obligation_reused"] is False
    # ...but the invoice was already on record, so the submission is a duplicate.
    assert investigation["original_invoice"] is not None
    assert investigation["submission_is_duplicate"] is True


def test_obligation_is_keyed_by_identity_not_amount(workspace_tmp_path):
    """A corrected amount on the same invoice reuses the same obligation."""

    orchestrator, database = build(workspace_tmp_path, "amount")
    first = upload(orchestrator, amount="918.00")
    second = upload(orchestrator, amount="900.00")

    assert investigation_of(first)["obligation_id"] == investigation_of(second)["obligation_id"]
    assert count_obligations(database) == 1


def test_obligation_key_ignores_formatting():
    assert obligation_key("INV-2026-5814", "Silverline Network Solutions LLC", "USD") == obligation_key(
        "inv 2026 5814", "SILVERLINE NETWORK SOLUTIONS, L.L.C.", "usd"
    )


# ---------------------------------------------------------------------------
# 2. Settlements vs completed attempts
# ---------------------------------------------------------------------------
def test_two_successful_attempts_are_not_two_settlements():
    """The core rule: execution records are not bank settlement evidence."""

    state = resolve_payment_state(
        invoice_amount=Decimal("918.00"),
        currency="USD",
        payment_attempts=[
            {"attempt_id": "a1", "status": "SUCCESS", "amount": "918.00", "currency": "USD"},
            {"attempt_id": "a2", "status": "SUCCESS", "amount": "918.00", "currency": "USD"},
        ],
        bank_transactions=[],
    )

    assert state.status != PreviousPaymentStatus.duplicate_payment_detected
    assert state.status == PreviousPaymentStatus.payment_status_unknown
    assert state.needs_agent_2 is True
    assert state.blocks_payment is True
    # Nothing is counted as verified.
    assert state.settlements == []


def test_single_successful_attempt_is_not_verified_payment():
    state = resolve_payment_state(
        invoice_amount=Decimal("918.00"),
        currency="USD",
        payment_attempts=[
            {"attempt_id": "a1", "status": "SUCCESS", "amount": "918.00", "currency": "USD"}
        ],
        bank_transactions=[],
    )
    assert state.status != PreviousPaymentStatus.already_paid
    assert state.status == PreviousPaymentStatus.payment_status_unknown


def test_two_verified_settlements_are_a_confirmed_duplicate_payment():
    state = resolve_payment_state(
        invoice_amount=Decimal("918.00"),
        currency="USD",
        payment_attempts=[],
        bank_transactions=[settled("PMT-A", date="2026-10-20"), settled("PMT-B", date="2026-10-26")],
    )
    assert state.status == PreviousPaymentStatus.duplicate_payment_detected
    assert len(state.settlements) == 2
    assert state.blocks_payment is True


def test_one_verified_settlement_covers_the_invoice():
    state = resolve_payment_state(
        invoice_amount=Decimal("918.00"),
        currency="USD",
        payment_attempts=[],
        bank_transactions=[settled("PMT-A")],
    )
    assert state.status == PreviousPaymentStatus.already_paid


def test_duplicate_reference_counted_once():
    """Two rows for the same reference are one transfer, not two."""

    state = resolve_payment_state(
        invoice_amount=Decimal("918.00"),
        currency="USD",
        payment_attempts=[],
        bank_transactions=[settled("PMT-A"), settled("PMT-A")],
    )
    assert len(state.settlements) == 1
    assert state.status == PreviousPaymentStatus.already_paid


def test_pending_settlement_is_not_treated_as_complete():
    pending = SyntheticTransaction(
        reference="PMT-P", amount=Decimal("918.00"), currency="USD",
        direction="credit", counterparty=INVOICE["vendor"], status="pending", value_date="2026-10-20",
    )
    state = resolve_payment_state(
        invoice_amount=Decimal("918.00"), currency="USD",
        payment_attempts=[{"attempt_id": "a1", "status": "PENDING", "amount": "918.00"}],
        bank_transactions=[pending],
    )
    assert state.status != PreviousPaymentStatus.already_paid
    assert state.status != PreviousPaymentStatus.duplicate_payment_detected


def test_end_to_end_two_attempts_request_reconciliation(workspace_tmp_path):
    """An invoice with two completed attempts is escalated, not called settled."""

    orchestrator, _ = build(
        workspace_tmp_path,
        "twoattempts",
        attempts=[success_attempt("k1"), success_attempt("k2")],
    )
    case = upload(orchestrator)
    investigation = investigation_of(case)

    assert investigation["status"] != "CONFIRMED_DUPLICATE_PAYMENT"
    assert investigation["previous_payment_status"] == "PAYMENT_STATUS_UNKNOWN"
    investigations = [
        r.data["investigation"] for r in case.reports
        if r.agent.value == "agent_1" and r.data.get("investigation")
    ]
    assert any(i.get("needs_agent_2") for i in investigations)
    assert investigation["blocks_payment"] is True


def test_payment_state_reports_attempts_and_settlements_separately():
    """The result must expose the distinction, not just the verdict."""

    state = resolve_payment_state(
        invoice_amount=Decimal("918.00"), currency="USD",
        payment_attempts=[
            {"attempt_id": "a1", "status": "SUCCESS", "amount": "918.00"},
            {"attempt_id": "a2", "status": "SUCCESS", "amount": "918.00"},
        ],
        bank_transactions=[settled("PMT-A")],
    )
    # One bank settlement exists; the attempts are not counted as settlements.
    assert len(state.settlements) == 1
