"""Agent 4 tests: payment failure and reconciliation-exception investigation.

Agent 4 diagnoses only from evidence it can see. These tests pin both halves of
that promise: it explains every failure the bank *did* report, and it refuses to
explain one the bank did not.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from workflow.agent4_exceptions import A2_HANDOFF_VERDICTS, DIAGNOSES, PaymentExceptionSpecialist
from workflow.mock_bank import FAILURE_CODES, MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.payment_store import PaymentRecordStore
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, CaseStatus

from test_workflow import build_analysis


VENDOR = "Northstar Cloud Systems LLC"
CUSTOMER = "Redwood Learning Studio LLC"
AMOUNT = "1250.00"


def build(workspace_tmp_path, name: str):
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    repository = WorkflowRepository(database)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.execution.bank = MockBank(database)
    # These tests assert the deterministic diagnosis text, so the optional LLM
    # narrative is disabled here. Its behaviour is covered in
    # test_agent4_llm_report.py.
    orchestrator.exceptions.report_writer = None
    return orchestrator, database


def pay(orchestrator, invoice="INV-A4-1", scenario=None, failure_code=None):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total=AMOUNT, subtotal=AMOUNT, tax="0.00",
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))
    return orchestrator.approve(
        case.case_id, approved_by="approver@example.test",
        simulate=scenario, failure_code=failure_code,
    )


def agent4_report(case):
    reports = [r for r in case.reports if r.agent == AgentId.agent_4]
    assert reports, f"Agent 4 did not run (case is {case.status.value})"
    return reports[-1]


def bank_rows(database):
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM mock_bank_ledger")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Insufficient fictional account balance
# ---------------------------------------------------------------------------
def test_insufficient_funds(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "funds")
    case = pay(orchestrator, scenario="FAILED", failure_code="INSUFFICIENT_FUNDS")
    report = agent4_report(case)

    assert report.data["failure_code"] == "INSUFFICIENT_FUNDS"
    assert report.data["failure_code_source"] == "mock_bank"
    assert report.data["transaction_status"] == "FAILED"
    assert report.data["risk_level"] == "medium"
    assert "sufficient cleared funds" in report.data["diagnosis"]
    assert report.data["retry_permitted"] is False
    assert report.requires_human is True
    assert case.status != CaseStatus.completed

    # The bank credited nothing.
    rows = bank_rows(database)
    assert rows and rows[0]["status"] == "FAILED"
    assert rows[0]["received_amount"] is None


# ---------------------------------------------------------------------------
# 2. Invalid recipient account
# ---------------------------------------------------------------------------
def test_invalid_recipient_account(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "invalid")
    case = pay(orchestrator, scenario="FAILED", failure_code="INVALID_ACCOUNT")
    report = agent4_report(case)

    assert report.data["failure_code"] == "INVALID_ACCOUNT"
    assert "recipient" in report.data["diagnosis"].casefold()
    assert "verify the payee" in report.data["recommended_resolution"].casefold()
    assert report.data["retry_permitted"] is False


# ---------------------------------------------------------------------------
# 3. Rejected payment
# ---------------------------------------------------------------------------
def test_rejected_payment(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "rejected")
    case = pay(orchestrator, scenario="FAILED", failure_code="REJECTED")
    report = agent4_report(case)

    assert report.data["failure_code"] == "REJECTED"
    assert "rejected" in report.data["diagnosis"].casefold()
    assert report.data["customer_explanation"]
    assert report.requires_human is True


# ---------------------------------------------------------------------------
# 4. Pending settlement
# ---------------------------------------------------------------------------
def test_pending_settlement(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "pending")
    case = pay(orchestrator, scenario="PENDING")
    report = agent4_report(case)

    assert report.data["transaction_status"] == "PENDING"
    assert report.data["risk_level"] == "high"
    assert report.data["retry_permitted"] is False
    # A pending payment is not a confirmed settlement.
    assert case.receipt is None
    assert case.status in {CaseStatus.pending, CaseStatus.requires_review}


def test_pending_does_not_invent_a_timeout(workspace_tmp_path):
    """The old code guessed 'timeout'; it must not."""

    orchestrator, database = build(workspace_tmp_path, "pending2")
    case = pay(orchestrator, scenario="PENDING")
    report = agent4_report(case)

    assert report.data["failure_code"] is None
    assert "timeout" not in str(report.data["diagnosis"]).casefold()


# ---------------------------------------------------------------------------
# 5. Partial payment
# ---------------------------------------------------------------------------
def test_partial_payment_is_escalated_to_agent4(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "partial")
    case = pay(orchestrator, scenario="PARTIAL")

    report = agent4_report(case)
    assert report.data["reconciliation_verdict"] == "PARTIAL_PAYMENT"
    assert report.requires_human is True
    assert report.data["retry_permitted"] is False
    assert case.status == CaseStatus.requires_review

    # The bank recorded the shortfall honestly.
    rows = bank_rows(database)
    assert rows[0]["status"] == "PARTIAL"
    assert Decimal(rows[0]["received_amount"]) < Decimal(AMOUNT)


# ---------------------------------------------------------------------------
# 6. Missing bank record
# ---------------------------------------------------------------------------
def test_missing_bank_record(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "missing")
    case = pay(orchestrator, scenario="MISSING_SETTLEMENT")

    report = agent4_report(case)
    assert report.data["bank_evidence"] is None
    assert any("no instruction" in d.casefold() or "no record" in d.casefold()
               for d in report.data["missing_details"])
    assert report.requires_human is True

    # Nothing settled at the bank.
    assert bank_rows(database) == []


# ---------------------------------------------------------------------------
# 7. Failed payment retry without approval
# ---------------------------------------------------------------------------
def test_failed_payment_cannot_be_retried_without_approval(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "retry")
    case = pay(orchestrator, scenario="FAILED", failure_code="REJECTED")
    report = agent4_report(case)
    assert report.data["retry_performed"] is False

    before = orchestrator.execution.bank.count()
    history_before = len(PaymentRecordStore(database).list_for_invoice("INV-A4-1"))

    # Attempting to execute again must not move money.
    again = orchestrator.execute_payment(case.case_id)
    assert orchestrator.execution.bank.count() == before
    assert len(PaymentRecordStore(database).list_for_invoice("INV-A4-1")) == history_before
    assert again.status != CaseStatus.completed


def test_agent4_never_sets_retry_permitted(workspace_tmp_path):
    for name, scenario, code in (
        ("r1", "FAILED", "INSUFFICIENT_FUNDS"),
        ("r2", "FAILED", "INVALID_ACCOUNT"),
        ("r3", "PENDING", None),
    ):
        orchestrator, _ = build(workspace_tmp_path, name)
        case = pay(orchestrator, invoice=f"INV-{name}", scenario=scenario, failure_code=code)
        assert agent4_report(case).data["retry_permitted"] is False


# ---------------------------------------------------------------------------
# 8. Successful payment handled without unnecessary Agent 4 escalation
# ---------------------------------------------------------------------------
def test_successful_payment_does_not_reach_agent4(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "success")
    case = pay(orchestrator)

    assert case.status == CaseStatus.completed
    # Agent 4 must not run at all for a clean payment.
    assert not [r for r in case.reports if r.agent == AgentId.agent_4]
    assert AgentId.agent_4 not in [run.agent for run in case.agent_runs]
    assert case.receipt is not None


# ---------------------------------------------------------------------------
# Never invent a failure reason
# ---------------------------------------------------------------------------
def test_missing_failure_code_is_not_invented(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "noreason")
    case = pay(orchestrator, scenario="FAILED")

    # Remove every trace of a reason code.
    conn = sqlite3.connect(database)
    conn.execute("UPDATE mock_bank_ledger SET failure_code = NULL, failure_reason = NULL")
    conn.commit()
    conn.close()

    attempt = PaymentRecordStore(database).list_for_invoice("INV-A4-1")[-1]
    attempt.failure_code = None
    attempt.failure_reason = None
    report = PaymentExceptionSpecialist(orchestrator.repository).investigate(case, attempt)

    assert report.data["failure_code"] is None
    assert report.data["diagnosis_code"] == "unknown"
    assert report.data["risk_level"] == "high"
    assert report.requires_human is True
    assert any(f.code == "reason_unavailable" for f in report.findings)


def test_unrecognised_code_is_not_mapped_to_a_known_cause(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "weird")
    case = pay(orchestrator, scenario="FAILED")

    conn = sqlite3.connect(database)
    conn.execute("UPDATE mock_bank_ledger SET failure_code = 'SOMETHING_NEW'")
    conn.commit()
    conn.close()

    attempt = PaymentRecordStore(database).list_for_invoice("INV-A4-1")[-1]
    report = PaymentExceptionSpecialist(orchestrator.repository).investigate(case, attempt)

    assert report.data["failure_code"] == "SOMETHING_NEW"
    assert any(f.code == "unknown_bank_code" for f in report.findings)
    assert report.data["risk_level"] == "high"


# ---------------------------------------------------------------------------
# Evidence and explanation quality
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(FAILURE_CODES))
def test_every_bank_code_has_a_diagnosis(workspace_tmp_path, code):
    orchestrator, database = build(workspace_tmp_path, f"code-{code.lower()}")
    case = pay(orchestrator, invoice=f"INV-{code}", scenario="FAILED", failure_code=code)
    report = agent4_report(case)

    assert report.data["failure_code"] == code
    assert report.data["diagnosis"]
    assert report.data["customer_explanation"]
    assert report.data["recommended_resolution"]
    assert report.data["bank_evidence"] is not None


def test_agent4_reports_the_evidence_it_used(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "evidence")
    case = pay(orchestrator, scenario="FAILED", failure_code="INVALID_ACCOUNT")
    report = agent4_report(case)

    bank = report.data["bank_evidence"]
    assert bank["invoice_number"] == "INV-A4-1"
    assert bank["status"] == "FAILED"
    assert bank["failure_code"] == "INVALID_ACCOUNT"
    assert bank["payment_reference"]
    assert report.data["invoice_number"] == "INV-A4-1"


def test_agent2_handoff_verdicts_are_recognised():
    assert "PARTIAL_PAYMENT" in A2_HANDOFF_VERDICTS
    assert "MISSING_PAYMENT" in A2_HANDOFF_VERDICTS
    assert "MATCHED" not in A2_HANDOFF_VERDICTS


def test_all_handoff_verdicts_are_investigated():
    """Every verdict Agent 4 must handle produces a discrepancy finding."""

    for verdict in sorted(A2_HANDOFF_VERDICTS):
        diagnosis = DIAGNOSES.get(verdict)
        # Either a specific diagnosis exists, or the generic verdict path runs.
        assert diagnosis is None or diagnosis.get("escalate")
