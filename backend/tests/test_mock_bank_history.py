"""End-to-end tests: automatic Mock Bank history and reconciliation.

Covers the required scenarios A-I. The organising idea is:

    the first payment automatically creates a Mock Bank record;
    when the customer later asks whether the money arrived, Agent 2 checks
    that existing record;
    Agent 5 then confirms the final result.

No manual bank-statement upload is involved anywhere in this file.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from workflow.accounts import ROLE_BANK_ADMIN
from workflow.mock_bank import SCENARIOS, MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, CaseStatus

from test_workflow import build_analysis
from test_agent1 import seed_invoice_history


VENDOR = "Summit Cloud Services LLC"
CUSTOMER = "Cedar Valley Design LLC"
AMOUNT = "1250.00"


def admin_headers(app) -> dict:
    """Sign in as a bank administrator for the internal ledger endpoints."""

    from workflow.accounts import ROLE_BANK_ADMIN

    email = "admin@bank.example"
    password = "adminpass123"
    try:
        app.state.accounts.sign_up(email=email, password=password, role=ROLE_BANK_ADMIN)
    except Exception:  # noqa: BLE001 - already created by an earlier test
        pass
    _account, token = app.state.accounts.sign_in(email=email, password=password)
    return {"Authorization": f"Bearer {token}"}


def build(workspace_tmp_path, name: str):
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    repository = WorkflowRepository(database)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.execution.bank = MockBank(database)
    return orchestrator, database


def open_case(orchestrator, invoice="INV-MB-1", amount=AMOUNT, **analysis):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total=amount, subtotal=amount, tax="0.00", **analysis,
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


def pay(orchestrator, invoice="INV-MB-1", amount=AMOUNT, scenario=None):
    case = open_case(orchestrator, invoice=invoice, amount=amount)
    return orchestrator.approve(
        case.case_id, approved_by="approver@example.test", simulate=scenario
    )


# ---------------------------------------------------------------------------
# A. New payment -> approved -> settlement appears in the Mock Bank Ledger
# ---------------------------------------------------------------------------
def test_new_payment_automatically_creates_a_bank_record(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "auto")
    case = open_case(orchestrator)

    bank = orchestrator.execution.bank
    assert bank.count() == 0, "no bank record exists before the payment"

    paid = orchestrator.approve(case.case_id, approved_by="approver@example.test")

    assert paid.status == CaseStatus.completed
    entries = bank.ledger()
    assert len(entries) == 1, "the simulator created exactly one bank transaction"
    entry = entries[0]
    assert entry["invoice_number"] == "INV-MB-1"
    assert entry["customer_name"] == CUSTOMER
    assert entry["vendor_name"] == VENDOR
    assert entry["payment_reference"] == paid.payment.provider_reference
    assert entry["expected_amount"] == "1250.00"
    assert entry["received_amount"] == "1250.00"
    assert entry["currency"] == "USD"
    assert entry["status"] == "SETTLED"
    assert entry["sender_account"] and entry["receiver_account"]
    assert entry["transaction_date"] and entry["settlement_date"]
    assert entry["scenario"] == "SUCCESS"
    assert entry["audit_timestamp"]


def test_bank_record_holds_every_required_field(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "fields")
    pay(orchestrator)
    entry = orchestrator.simulator.bank.ledger()[0]

    for field in (
        "entry_id", "invoice_number", "customer_name", "vendor_name",
        "payment_reference", "expected_amount", "received_amount", "currency",
        "sender_account", "receiver_account", "transaction_date",
        "settlement_date", "status", "scenario", "audit_timestamp",
    ):
        assert entry.get(field), f"bank record is missing {field}"


def test_bank_ledger_is_a_separate_system_from_agent5(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "separate")
    pay(orchestrator)

    conn = sqlite3.connect(database)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "mock_bank_ledger" in tables
    assert "mock_bank_instructions" in tables
    # The bank created the row, not the payment agent's own attempt log.
    assert orchestrator.simulator.bank.count() == 1


# ---------------------------------------------------------------------------
# B. Customer later asks about the payment -> Agent 3 -> Agent 2 -> Agent 5
# ---------------------------------------------------------------------------
def test_later_payment_status_check_is_answered_from_bank_history(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "later")
    paid = pay(orchestrator)
    assert paid.status == CaseStatus.completed

    # The customer later asks whether their payment arrived.
    check = open_case(
        orchestrator,
        problem="payment_status_discrepancy",
        primary="bank_reconciliation",
    )

    assert check.intent.value in {"bank_reconciliation", "PAYMENT_STATUS_CHECK"}
    agents = [run.agent.value for run in sorted(check.agent_runs, key=lambda r: r.order)]
    assert agents[:2] == ["agent_3", "agent_2"]
    assert "agent_5" in agents
    # No human approval is required to answer a status question.
    assert "human_approval" not in agents


def test_status_check_uses_the_existing_bank_record(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "reuse")
    pay(orchestrator)
    before = orchestrator.simulator.bank.count()

    open_case(orchestrator, problem="payment_status_discrepancy", primary="bank_reconciliation")

    # Checking a payment never creates a new bank transaction.
    assert orchestrator.simulator.bank.count() == before


def test_no_manual_statement_upload_is_needed(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "noupload")
    pay(orchestrator)

    from workflow.bank_importer import BankTransactionStore

    # The optional importer has nothing in it, yet reconciliation still works.
    assert BankTransactionStore(database).list_transactions() == []

    from workflow.bank_reconciliation import ReconciliationService

    recommendation = ReconciliationService(database).recommendation()
    assert recommendation["verdict"] == "MATCHED"
    assert recommendation["independent_evidence"] is True


# ---------------------------------------------------------------------------
# C. Partial payment -> Agent 2 identifies the shortfall
# ---------------------------------------------------------------------------
def test_partial_payment_reports_the_shortfall(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "partial")
    paid = pay(orchestrator, scenario="PARTIAL")

    assert paid.status != CaseStatus.completed
    assert paid.receipt is None
    recommendation = paid.reconciliation
    assert recommendation["verdict"] == "PARTIAL_PAYMENT"

    from workflow.bank_reconciliation import ReconciliationService

    report = ReconciliationService(database).reconcile()
    row = next(r for r in report.rows if r.invoice_number == "INV-MB-1")
    assert row.expected_amount == Decimal("1250.00")
    assert row.bank_received == Decimal("625.00")
    assert row.difference == Decimal("-625.00")
    assert any(f.code == "partial_payment" for f in row.findings)


# ---------------------------------------------------------------------------
# D. Failed payment -> no fictional money arrives
# ---------------------------------------------------------------------------
def test_failed_payment_credits_nothing(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "failed")
    paid = pay(orchestrator, scenario="FAILED")

    entries = orchestrator.simulator.bank.ledger()
    assert len(entries) == 1
    assert entries[0]["status"] == "FAILED"
    assert entries[0]["received_amount"] is None
    # A failed instruction leaves the settled balance at zero.
    assert orchestrator.simulator.bank.balances() == {}
    assert paid.status != CaseStatus.completed
    assert paid.receipt is None


# ---------------------------------------------------------------------------
# E. Pending payment -> Agent 5 does not falsely confirm
# ---------------------------------------------------------------------------
def test_pending_payment_is_not_confirmed(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "pending")
    paid = pay(orchestrator, scenario="PENDING")

    assert paid.payment.status == "PENDING"
    assert paid.receipt is None
    assert paid.status == CaseStatus.pending
    assert paid.reconciliation["verdict"] != "MATCHED"
    entry = orchestrator.simulator.bank.ledger()[0]
    assert entry["status"] == "PENDING"
    assert entry["settlement_date"] is None


def test_missing_settlement_is_not_confirmed(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "missing")
    paid = pay(orchestrator, scenario="MISSING_SETTLEMENT")

    # The instruction was accepted but nothing settled.
    assert orchestrator.simulator.bank.ledger() == []
    assert orchestrator.simulator.bank.instruction(paid.payment.idempotency_key) is not None
    assert paid.receipt is None
    assert paid.reconciliation["verdict"] != "MATCHED"


def test_duplicate_settlement_is_flagged_and_not_confirmed(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "dupsettle")
    paid = pay(orchestrator, scenario="DUPLICATE_SETTLEMENT")

    entries = orchestrator.simulator.bank.ledger()
    assert len(entries) == 2, "two distinct settlements were simulated"
    assert all(e["is_anomaly"] == 1 for e in entries)
    assert paid.receipt is None
    assert paid.reconciliation["verdict"] != "MATCHED"


# ---------------------------------------------------------------------------
# F. Uploading the same invoice again -> Agent 1 prevents duplicate execution
# ---------------------------------------------------------------------------
def test_reupload_after_payment_is_blocked_by_agent1(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "reupload")
    seed_invoice_history(
        database,
        [{
            "invoice_number": "INV-MB-1", "vendor": VENDOR, "customer": CUSTOMER,
            "amount": AMOUNT, "issue_date": "2026-01-01", "description": "cloud",
        }],
    )
    paid = pay(orchestrator)
    assert paid.status == CaseStatus.completed
    assert orchestrator.simulator.bank.count() == 1

    # A re-upload is either blocked immediately or verified against the bank;
    # either way no second payment is ever made.
    reupload = open_case(orchestrator)
    if reupload.status == CaseStatus.awaiting_approval:
        try:
            orchestrator.approve(reupload.case_id, approved_by="second@example.test")
        except Exception:  # noqa: BLE001 - blocking at approval is correct
            pass
    assert orchestrator.simulator.bank.count() == 1


# ---------------------------------------------------------------------------
# G. Restart the application -> bank history still exists
# ---------------------------------------------------------------------------
def test_bank_history_survives_restart(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "restart")
    paid = pay(orchestrator)
    reference = paid.payment.provider_reference

    # Simulate a restart with fresh objects over the same file.
    fresh_bank = MockBank(database)
    entries = fresh_bank.ledger()
    assert len(entries) == 1
    assert entries[0]["payment_reference"] == reference
    assert entries[0]["status"] == "SETTLED"

    # A fresh orchestrator can reconcile from the persisted history.
    from workflow.bank_reconciliation import ReconciliationService

    recommendation = ReconciliationService(database).recommendation()
    assert recommendation["verdict"] == "MATCHED"


# ---------------------------------------------------------------------------
# H. Repeated execution request -> no additional debit or credit
# ---------------------------------------------------------------------------
def test_repeated_execution_creates_no_extra_bank_entry(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "repeat")
    paid = pay(orchestrator)
    before = orchestrator.simulator.bank.count()
    balance_before = orchestrator.simulator.bank.balances()

    again = orchestrator.execute_payment(paid.case_id)
    assert again.last_payment_report.data.get("executed") is False

    assert orchestrator.simulator.bank.count() == before
    assert orchestrator.simulator.bank.balances() == balance_before


def test_bank_instruction_is_idempotent_at_the_bank(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "idem")
    paid = pay(orchestrator)
    bank = orchestrator.execution.bank

    repeat = bank.submit_instruction(
        idempotency_key=paid.payment.idempotency_key,
        amount=Decimal(AMOUNT), currency="USD", payee=VENDOR,
        invoice_number="INV-MB-1", scenario="SUCCESS",
    )
    assert repeat["payment_reference"] == paid.payment.provider_reference
    assert bank.count() == 1


# ---------------------------------------------------------------------------
# I. Customer-reported payment with no bank record stays UNVERIFIED
# ---------------------------------------------------------------------------
def test_customer_claim_without_bank_record_is_unverified(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "claim")
    pay(orchestrator, scenario="MISSING_SETTLEMENT")
    # The bank holds no settlement at all for this invoice.
    assert orchestrator.simulator.bank.ledger() == []

    from workflow.bank_reconciliation import ReconciliationService

    recommendation = ReconciliationService(database).recommendation()
    assert recommendation["verdict"] != "MATCHED"
    assert recommendation["independent_evidence"] is False


def test_claim_on_a_completely_unknown_invoice_is_unverified(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "unknowninv")
    from workflow.bank_reconciliation import ReconciliationService

    recommendation = ReconciliationService(database).recommendation()
    assert recommendation["verdict"] != "MATCHED"


# ---------------------------------------------------------------------------
# Scenario matrix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_is_auditable(workspace_tmp_path, scenario):
    orchestrator, database = build(workspace_tmp_path, f"sc-{scenario.lower()}")
    paid = pay(orchestrator, invoice=f"INV-{scenario[:6]}", scenario=scenario)

    instruction = orchestrator.simulator.bank.instruction(paid.payment.idempotency_key)
    assert instruction is not None
    assert instruction["outcome"] == scenario
    assert instruction["simulated"] is True

    entries = orchestrator.simulator.bank.ledger()
    assert all(e["scenario"] == scenario for e in entries)
    # Money is only reported as received where the bank settled something.
    for entry in entries:
        if entry["status"] == "SETTLED":
            assert entry["received_amount"] == entry["amount"]
        if entry["status"] in {"FAILED", "PENDING"}:
            assert entry["received_amount"] is None


def test_api_ledger_endpoint_lists_transactions(workspace_tmp_path):
    from fastapi.testclient import TestClient
    from main import create_app

    orchestrator, database = build(workspace_tmp_path, "api")
    pay(orchestrator)

    app = create_app()
    app.state.workflow_repository = WorkflowRepository(database)
    app.state.orchestrator = orchestrator
    client = TestClient(app)

    payload = client.get("/bank/ledger", headers=admin_headers(app)).json()
    assert payload["simulated"] is True
    assert "NO REAL MONEY" in payload["disclaimer"]
    assert payload["count"] == 1
    entry = payload["transactions"][0]
    assert entry["invoice_number"] == "INV-MB-1"

    # Detail view returns the settlement history.
    detail = client.get(f"/bank/ledger/{entry['entry_id']}", headers=admin_headers(app)).json()
    assert detail["history_count"] == 1

    # Search filters.
    assert client.get("/bank/ledger?search=INV-MB-1", headers=admin_headers(app)).json()["count"] == 1
    assert client.get("/bank/ledger?search=nothing-like-this", headers=admin_headers(app)).json()["count"] == 0
    assert client.get("/bank/ledger?status=SETTLED", headers=admin_headers(app)).json()["count"] == 1
    assert client.get("/bank/ledger?status=FAILED", headers=admin_headers(app)).json()["count"] == 0
