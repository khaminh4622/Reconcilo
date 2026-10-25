"""Agent 1 only counts bank evidence that belongs to the invoice it checks.

Regression tests: Agent 1 used to add up every settled credit in the Mock Bank
(and any legacy row with the same amount, whoever it was from) as payments of
the invoice under review, and counted the just-uploaded document as its own
history. Unpaid invoices were then blocked as "already paid".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from workflow.mock_bank import MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, CaseStatus, SyntheticTransaction
from workflow.seed import synthetic_bank_seed

from http_helpers import auth, invoice_text, make_admin, make_app, reply, signup, upload
from test_agent1 import seed_invoice_history
from test_workflow import build_analysis

VENDOR = "Northstar Office Supplies LLC"
CUSTOMER = "Blue Harbor Technologies Inc."


def build(workspace_tmp_path, name):
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    orchestrator = WorkflowOrchestrator(WorkflowRepository(database))
    orchestrator.simulator.bank = MockBank(database)
    orchestrator.execution.bank = orchestrator.simulator.bank
    return orchestrator, database


def open_case(orchestrator, invoice, amount):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER, total=amount, subtotal=amount, tax="0.00"
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


def history(database, *invoices):
    seed_invoice_history(database, [
        {"invoice_number": number, "vendor": VENDOR, "customer": CUSTOMER, "amount": amount,
         "issue_date": "2026-03-01", "description": "x"}
        for number, amount in invoices
    ])


def test_another_invoices_settlement_does_not_pay_this_one(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "other-settlement")
    history(database, ("INV-SC-1", "1280.00"), ("INV-SC-2", "1250.00"))
    first = orchestrator.approve(open_case(orchestrator, "INV-SC-1", "1280.00").case_id, approved_by="admin")
    assert first.status == CaseStatus.completed

    second = open_case(orchestrator, "INV-SC-2", "1250.00")

    assert second.status == CaseStatus.awaiting_approval, second.pending_reason
    paid = orchestrator.approve(second.case_id, approved_by="admin")
    assert paid.status == CaseStatus.completed


def test_same_amount_from_another_counterparty_is_not_a_payment(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "seed-250")
    orchestrator.repository.seed_bank_transactions(synthetic_bank_seed())  # holds Kestrel's 250.00
    history(database, ("INV-SC-3", "250.00"))

    case = open_case(orchestrator, "INV-SC-3", "250.00")

    assert case.status == CaseStatus.awaiting_approval, case.pending_reason


def test_matching_vendor_and_amount_still_counts_as_paid(workspace_tmp_path):
    """The established amount + counterparty rule for unlabelled rows is kept."""

    orchestrator, database = build(workspace_tmp_path, "vendor-match")
    history(database, ("INV-SC-4", "640.00"))
    orchestrator.repository.seed_bank_transactions([SyntheticTransaction(
        reference="PAID-ELSEWHERE", amount=Decimal("640.00"), currency="USD", direction="credit",
        counterparty=VENDOR, status="settled", value_date="2026-03-05",
    )])

    case = open_case(orchestrator, "INV-SC-4", "640.00")

    assert case.status == CaseStatus.requires_review
    assert "already paid" in case.pending_reason


def test_two_unrelated_settlements_are_not_a_duplicate_payment(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "two-unrelated")
    history(database, ("INV-SC-5", "111.00"), ("INV-SC-6", "222.00"), ("INV-SC-7", "333.00"))
    for number, amount in (("INV-SC-5", "111.00"), ("INV-SC-6", "222.00")):
        orchestrator.approve(open_case(orchestrator, number, amount).case_id, approved_by="admin")

    case = open_case(orchestrator, "INV-SC-7", "333.00")

    report = next(r for r in case.reports if r.agent == AgentId.agent_1)
    assert report.data["status"] != "CONFIRMED_DUPLICATE_PAYMENT"
    assert report.data["investigation"]["payment_analysis"]["verified_settlements"] == 0
    assert case.status == CaseStatus.awaiting_approval


def test_first_upload_is_not_its_own_history(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "self-history")
    customer, _ = signup(client, "c@example.test")
    admin, _ = make_admin(app, client, "bank@example.test")
    # A settled payment elsewhere in the bank, larger than the new invoice.
    paid = upload(app, client, customer, invoice_text("INV-SC-8", "980.00"), reply("INV-SC-8", "980.00")).json()
    client.post(f"/workflow/cases/{paid['case_id']}/approve", json={}, headers=auth(admin))

    case = upload(app, client, customer, invoice_text("INV-SC-9", "530.00"), reply("INV-SC-9", "530.00")).json()

    report = next(r for r in case["reports"] if r["agent"] == "agent_1")
    best = report["data"]["investigation"]["best_match"]
    assert best is None or best["invoice_number"] != "INV-SC-9"
    assert case["status"] == "AWAITING_APPROVAL", case["pending_reason"]


def test_first_submission_with_a_matching_settlement_is_held(workspace_tmp_path):
    """Has it been paid? A settled bank payment tied to the invoice blocks approval,
    even when GhostLedger has never seen the invoice before."""

    orchestrator, _ = build(workspace_tmp_path, "first-paid")
    orchestrator.repository.seed_bank_transactions([SyntheticTransaction(
        reference="TRF-FIRST-1", amount=Decimal("715.00"), currency="USD", direction="credit",
        counterparty=CUSTOMER, status="settled", value_date="2026-03-05",
    )])

    case = open_case(orchestrator, "INV-SC-10", "715.00")

    assert case.status == CaseStatus.requires_review
    assert "settled payment" in case.pending_reason
    assert case.customer_response.outcome == "BLOCKED_DUPLICATE"
    assert "already shows a settled payment" in case.customer_response.answer
    assert orchestrator.simulator.bank.count() == 0
