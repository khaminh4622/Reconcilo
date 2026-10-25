"""Agent 2 reconciliation tests.

These pin the guarantees that matter for the user's existing data:

* the live database is never reset or rewritten;
* Agent 5's own settlement rows are never accepted as independent bank proof;
* demo fixtures never influence a real invoice;
* an invoice is only reported PAID when evidence Agent 5 did not author confirms it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from workflow.agent2_reconciliation_engine import (
    ReconciliationAgent,
    ReconciliationOutcome,
    ReconciliationRepository,
)
from workflow.migrations import migrate
from workflow.bank_importer import BankTransactionStore
from workflow.bank_reconciliation import ReconciliationService
from workflow.mock_bank import MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.payment_provider import SyntheticPaymentProvider, reset_provider
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, CaseStatus

from test_workflow import build_analysis


INVOICE = "INV-A2-TEST"
VENDOR = "Northstar Office Supplies LLC"
CUSTOMER = "Blue Harbor Technologies Inc."


def fresh(workspace_tmp_path, name: str, outcome: str = "SETTLED"):
    """Isolated orchestrator with its own Mock Bank."""

    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    WorkflowRepository(database)
    migrate(database, take_backup=False)
    orchestrator = WorkflowOrchestrator(WorkflowRepository(database))
    orchestrator.execution.bank = MockBank(database)
    return orchestrator, database


def pay_orchestrator_case(orchestrator, invoice=INVOICE, amount="500.00", simulate=None):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total=amount, subtotal=amount, tax="0.00",
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


def pay(orchestrator, invoice=INVOICE, amount="500.00", simulate=None):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total=amount, subtotal=amount, tax="0.00",
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))
    return case, orchestrator.approve(
        case.case_id, approved_by="alice@example.test", simulate=simulate
    )


def insert_bank_row(database, reference, amount, currency="USD", status="settled", is_demo=0, invoice=None):
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            """
            INSERT INTO synthetic_bank_transactions
                (transaction_id, reference, amount, currency, direction, counterparty, status,
                 value_date, source, transaction_json, invoice_number, is_demo)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"txn-{reference}", reference, amount, currency, "debit", VENDOR, status,
                "2026-10-24", "synthetic_bank", "{}", invoice, is_demo,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Data safety
# ---------------------------------------------------------------------------
def test_migration_preserves_all_business_rows(workspace_tmp_path):
    database = workspace_tmp_path / "preserve.db"
    repository = WorkflowRepository(database)
    repository.seed_bank_transactions([])
    orchestrator = WorkflowOrchestrator(repository)
    case, paid = pay(orchestrator)

    # Only count tables that actually exist: Agent 3's tables are absent until
    # Agent 3 has analysed a document in this database.
    conn = sqlite3.connect(database)
    present = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    tables = [
        t for t in (
            "document_records", "payable_obligations", "payment_attempts",
            "payment_receipts", "workflow_cases",
        ) if t in present
    ]
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    conn.close()
    assert before, "at least one business table must exist"

    migrate(database, take_backup=False)

    conn = sqlite3.connect(database)
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    conn.close()
    assert before == after
    assert after["payment_attempts"] >= 1


def test_migration_is_idempotent(workspace_tmp_path):
    database = workspace_tmp_path / "idem.db"
    WorkflowRepository(database)
    first = migrate(database, take_backup=False)
    second = migrate(database, take_backup=False)
    assert second["columns_added"] == []
    assert second["tables_created"] == []
    # The migration now also creates Agent 2's bank evidence tables.
    assert "reconciliation_results" in first["tables_created"]
    assert "bank_transactions" in first["tables_created"]
    assert "bank_imports" in first["tables_created"]


def test_migration_removes_only_duplicate_seed_fixtures(workspace_tmp_path):
    database = workspace_tmp_path / "dedupe.db"
    WorkflowRepository(database)
    conn = sqlite3.connect(database)
    for index in range(50):
        conn.execute(
            """
            INSERT INTO synthetic_bank_transactions
                (transaction_id, reference, amount, currency, direction, counterparty,
                 status, value_date, source, transaction_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (f"dup-{index}", "TRF-SEED", "10.00", "USD", "debit", "X", "settled",
             "2026-10-01", "synthetic_bank", "{}"),
        )
    conn.commit()
    conn.close()

    report = migrate(database, take_backup=False)
    assert report["seed_rows_removed"] == 49

    conn = sqlite3.connect(database)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM synthetic_bank_transactions WHERE reference='TRF-SEED'"
    ).fetchone()[0]
    conn.close()
    assert remaining == 1


# ---------------------------------------------------------------------------
# The central trust rule
# ---------------------------------------------------------------------------
def test_mock_bank_is_independent_of_agent5(workspace_tmp_path):
    """Agent 5 no longer writes settlement rows; the Mock Bank does."""

    orchestrator, database = fresh(workspace_tmp_path, "trust")
    case, paid = pay(orchestrator)

    assert paid.payment.status == "SUCCESS"
    reference = paid.payment.provider_reference
    assert reference and reference.startswith("MB-")

    # The settlement lives in the Mock Bank's own ledger.
    entries = orchestrator.execution.bank.credits_for(reference)
    assert len(entries) == 1
    assert entries[0]["status"] == "SETTLED"

    # Agent 5 did not write it into the imported-statement table.
    importer = BankTransactionStore(database)
    assert importer.list_transactions() == []


def test_verified_settlement_completes_the_case(workspace_tmp_path):
    orchestrator, _ = fresh(workspace_tmp_path, "review")
    _, paid = pay(orchestrator)

    assert paid.reconciliation is not None
    assert paid.reconciliation["verdict"] == "MATCHED"
    assert paid.reconciliation["independent_evidence"] is True
    assert paid.status == CaseStatus.completed
    assert paid.receipt is not None


def test_independent_bank_evidence_verifies_settlement(workspace_tmp_path):
    orchestrator, database = fresh(workspace_tmp_path, "independent")
    case, paid = pay(orchestrator)

    # A genuine bank statement row (not authored by Agent 5, not demo data)
    # sharing the settlement reference.
    insert_bank_row(
        database, paid.payment.provider_reference, "500.00",
        currency="USD", is_demo=0, invoice=INVOICE,
    )

    result = ReconciliationAgent(ReconciliationRepository(database)).reconcile(
        case.case_id, INVOICE
    )
    assert result.outcome == ReconciliationOutcome.settlement_verified
    assert result.independent_evidence is True
    assert result.settled_amount == Decimal("500.00")


def test_amount_mismatch_is_a_discrepancy(workspace_tmp_path):
    orchestrator, database = fresh(workspace_tmp_path, "mismatch")
    case, paid = pay(orchestrator)

    insert_bank_row(
        database, paid.payment.provider_reference, "450.00", is_demo=0, invoice=INVOICE,
    )
    result = ReconciliationAgent(ReconciliationRepository(database)).reconcile(
        case.case_id, INVOICE
    )
    assert result.outcome == ReconciliationOutcome.discrepancy_detected
    assert result.settled_amount == Decimal("450.00")
    assert any(f.code == "amount_mismatch" for f in result.findings)


# ---------------------------------------------------------------------------
# Demo data isolation
# ---------------------------------------------------------------------------
def test_demo_fixtures_are_ignored_for_real_invoices(workspace_tmp_path):
    orchestrator, database = fresh(workspace_tmp_path, "demoignore")
    case, paid = pay(orchestrator)

    # A matching demo row must not verify a real payment.
    insert_bank_row(
        database, paid.payment.provider_reference, "500.00", is_demo=1, invoice=None,
    )
    result = ReconciliationAgent(ReconciliationRepository(database)).reconcile(
        case.case_id, INVOICE
    )
    assert result.outcome != ReconciliationOutcome.settlement_verified
    assert result.independent_evidence is False


def test_demo_mode_flag_controls_fixture_usage(monkeypatch):
    from workflow.agent2_reconciliation_engine import demo_mode_enabled

    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert demo_mode_enabled() is False
    monkeypatch.setenv("DEMO_MODE", "1")
    assert demo_mode_enabled() is True
    monkeypatch.setenv("DEMO_MODE", "false")
    assert demo_mode_enabled() is False


def test_demo_rows_never_change_invoice_payment_status(workspace_tmp_path):
    from workflow.obligations import ObligationRepository, obligation_key

    orchestrator, database = fresh(workspace_tmp_path, "demostatus")
    _, paid = pay(orchestrator)
    state_before = ObligationRepository(database).get_obligation(
        obligation_key(INVOICE, VENDOR, "USD")
    )["state"]

    insert_bank_row(database, "TRF-DEMO-1", "500.00", is_demo=1, invoice=INVOICE)
    ReconciliationAgent(ReconciliationRepository(database)).reconcile(None, INVOICE)

    state_after = ObligationRepository(database).get_obligation(
        obligation_key(INVOICE, VENDOR, "USD")
    )["state"]
    assert state_before == state_after


# ---------------------------------------------------------------------------
# Priority 1: existing data
# ---------------------------------------------------------------------------
def test_invoice_without_provider_reference_requires_reconciliation(workspace_tmp_path):
    orchestrator, database = fresh(workspace_tmp_path, "legacy")
    case, paid = pay(orchestrator)

    # Simulate a payment recorded before references were captured.
    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE payment_attempts SET provider_reference=NULL, settlement_reference=NULL WHERE invoice_number=?",
        (INVOICE,),
    )
    conn.commit()
    conn.close()

    result = ReconciliationAgent(ReconciliationRepository(database)).reconcile(
        case.case_id, INVOICE
    )
    assert result.outcome == ReconciliationOutcome.reconciliation_required
    assert any(f.code == "no_provider_reference" for f in result.findings)


def test_no_payment_attempt_means_nothing_to_reconcile(workspace_tmp_path):
    _, database = fresh(workspace_tmp_path, "nopay")
    result = ReconciliationAgent(ReconciliationRepository(database)).reconcile(None, "INV-UNKNOWN")
    assert result.outcome == ReconciliationOutcome.reconciliation_required
    assert any(f.code == "no_payment" for f in result.findings)


def test_pending_payment_is_not_verified(workspace_tmp_path):
    orchestrator, database = fresh(workspace_tmp_path, "pendrec", outcome="PENDING")
    case, paid = pay(orchestrator, simulate="PENDING")

    assert paid.payment.status == "PENDING"
    recommendation = ReconciliationService(database).recommendation()
    assert recommendation["verdict"] != "MATCHED"
    assert recommendation["independent_evidence"] is False


def test_reconciliation_result_is_persisted(workspace_tmp_path):
    orchestrator, database = fresh(workspace_tmp_path, "persist")
    case, _ = pay(orchestrator)
    repository = ReconciliationRepository(database)
    ReconciliationAgent(repository).reconcile(case.case_id, INVOICE)

    stored = repository.latest_result(INVOICE)
    assert stored is not None
    assert stored["outcome"] == "RECONCILIATION_REQUIRED"
    assert stored["provider_reference"]


# ---------------------------------------------------------------------------
# Workflow integration
# ---------------------------------------------------------------------------
def test_agent2_runs_between_agent5_stages(workspace_tmp_path):
    """Agent 5 initiates, Agent 2 reconciles, then Agent 5 confirms."""

    orchestrator, _ = fresh(workspace_tmp_path, "chain")
    _, paid = pay(orchestrator)

    runs = sorted(paid.agent_runs, key=lambda r: r.order)
    simulation = next(r for r in runs if r.agent == AgentId.payment_simulator)
    reconciliation = next(r for r in runs if r.agent == AgentId.agent_2)
    confirmation = next(r for r in runs if r.agent == AgentId.agent_5)

    assert simulation.order < reconciliation.order < confirmation.order
    assert any(r.agent == AgentId.agent_2 for r in paid.reports)


def test_reconciliation_does_not_duplicate_payment_records(workspace_tmp_path):
    orchestrator, database = fresh(workspace_tmp_path, "nodup")
    _, paid = pay(orchestrator)

    conn = sqlite3.connect(database)
    attempts = conn.execute("SELECT COUNT(*) FROM payment_attempts").fetchone()[0]
    obligations = conn.execute("SELECT COUNT(*) FROM payable_obligations").fetchone()[0]
    conn.close()

    assert attempts == 1
    assert obligations == 1
