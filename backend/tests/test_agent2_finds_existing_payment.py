"""Regression: Agent 2 must find a Mock Bank settlement for an existing invoice.

The reported bug: a settled Mock Bank transaction existed for
INV-GL-NEW-20261024-9017 (MB-01DFC8E527A7, 1200.00 USD), yet a payment-status
inquiry returned "No matching bank transaction found".

Root cause: Agent 2's payment-status path queried only the legacy
``synthetic_bank_transactions`` table and never the ``mock_bank_ledger`` the
payment simulator writes to. The records were correct; the query was wrong.

These tests build the exact relationship the live database had, then prove
Agent 2 resolves it by stable identifiers.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from agent3.schemas import AnalysisResult, PrimaryProblem, ProblemType
from workflow.mock_bank import MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId

from test_workflow import build_analysis


VENDOR = "Northstar Cloud Systems LLC"
CUSTOMER = "Redwood Learning Studio LLC"
INVOICE = "INV-GL-NEW-20261024-9017"
AMOUNT = "1200.00"


def build(workspace_tmp_path, name: str):
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    repository = WorkflowRepository(database)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.execution.bank = MockBank(database)
    return orchestrator, database


def pay_invoice(orchestrator):
    """Create the original payment, as the live database has it."""

    analysis = build_analysis(
        invoice_number=INVOICE, vendor=VENDOR, customer=CUSTOMER,
        total=AMOUNT, subtotal=AMOUNT, tax="0.00",
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))
    return orchestrator.approve(case.case_id, approved_by="approver@example.test")


def inquiry(orchestrator, invoice=INVOICE, paid=AMOUNT, reference=None):
    """A customer asking whether the payment arrived."""

    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total=AMOUNT, subtotal=AMOUNT, tax="0.00",
        paid=paid, payment_reference=reference,
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.payment_status_dispute,
    )
    return orchestrator.route(orchestrator.open_case(analysis, "inquiry.pdf"))


def bank_counts(database) -> dict:
    conn = sqlite3.connect(database)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "mock_bank_ledger",
                "mock_bank_instructions",
                "payment_attempts",
                "synthetic_bank_transactions",
            )
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The reported scenario
# ---------------------------------------------------------------------------
def test_existing_mock_bank_payment_is_found_by_agent2(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "found")
    paid = pay_invoice(orchestrator)
    reference = paid.payment.provider_reference
    assert reference and reference.startswith("MB-")

    case = inquiry(orchestrator)

    reports = [r for r in case.reports if r.agent == AgentId.agent_2]
    assert reports, "Agent 2 must run for a payment-status inquiry"
    report = reports[-1]
    assert report.data.get("matched") is True, "the settled Mock Bank credit must be found"
    assert report.resolved is True
    assert any(f.code == "reference_matched" for f in report.findings)


def test_agent2_finds_the_payment_by_reference(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "byref")
    paid = pay_invoice(orchestrator)

    evidence = orchestrator.reconciliation._bank_evidence(
        reference=paid.payment.provider_reference, invoice_number=INVOICE
    )
    assert len(evidence) == 1
    assert evidence[0]["payment_reference"] == paid.payment.provider_reference
    assert Decimal(evidence[0]["amount"]) == Decimal(AMOUNT)


def test_agent2_finds_the_payment_by_invoice_number(workspace_tmp_path):
    """Even without a quoted reference, the invoice number resolves the payment."""

    orchestrator, database = build(workspace_tmp_path, "byinv")
    pay_invoice(orchestrator)

    evidence = orchestrator.reconciliation._bank_evidence(invoice_number=INVOICE)
    assert len(evidence) == 1
    assert evidence[0]["invoice_number"] == INVOICE


def test_inquiry_resolves_the_invoice_to_its_recorded_reference(workspace_tmp_path):
    """A PDF quoting no reference still resolves through GhostLedger's record."""

    orchestrator, database = build(workspace_tmp_path, "resolve")
    paid = pay_invoice(orchestrator)

    case = inquiry(orchestrator, reference=None)
    report = [r for r in case.reports if r.agent == AgentId.agent_2][-1]
    assert report.data.get("matched") is True
    assert orchestrator.reconciliation._invoice_reference(INVOICE) == paid.payment.provider_reference


# ---------------------------------------------------------------------------
# The fix must not weaken verification
# ---------------------------------------------------------------------------
def test_no_new_payment_is_created_by_the_inquiry(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "nonew")
    pay_invoice(orchestrator)
    before = bank_counts(database)

    inquiry(orchestrator)
    inquiry(orchestrator, reference="MB-DOES-NOT-EXIST")

    after = bank_counts(database)
    assert after["mock_bank_ledger"] == before["mock_bank_ledger"]
    assert after["mock_bank_instructions"] == before["mock_bank_instructions"]
    assert after["payment_attempts"] == before["payment_attempts"]


def test_amount_alone_does_not_verify_another_invoice(workspace_tmp_path):
    """A same-amount credit for a different invoice must not verify this one."""

    orchestrator, database = build(workspace_tmp_path, "otherinv")
    # A different invoice is paid, same amount, same vendor.
    analysis = build_analysis(
        invoice_number="INV-OTHER-INVOICE", vendor=VENDOR, customer=CUSTOMER,
        total=AMOUNT, subtotal=AMOUNT, tax="0.00",
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))
    orchestrator.approve(case.case_id, approved_by="approver@example.test")

    # Enquire about an invoice the bank has never seen.
    enquiry = inquiry(orchestrator, invoice="INV-UNRELATED-0001", paid=AMOUNT)
    report = [r for r in enquiry.reports if r.agent == AgentId.agent_2][-1]

    assert report.data.get("matched") is not True
    assert report.resolved is False

    # The engine must not return the other invoice's credit.
    evidence = orchestrator.reconciliation._bank_evidence(
        invoice_number="INV-UNRELATED-0001"
    )
    assert evidence == []


def test_unpaid_invoice_with_a_guessed_reference_is_not_verified(workspace_tmp_path):
    """A reference the bank never issued cannot verify a different invoice.

    A genuinely paid invoice still verifies through its own stored reference;
    the check that matters is that a *bogus* reference cannot conjure evidence
    for an invoice the bank knows nothing about.
    """

    orchestrator, database = build(workspace_tmp_path, "badref")
    pay_invoice(orchestrator)  # pays INVOICE

    # Enquire about a different, never-paid invoice, quoting a made-up reference.
    case = inquiry(orchestrator, invoice="INV-NEVER-PAID-0002",
                   paid=AMOUNT, reference="MB-GUESSED-REFERENCE")
    report = [r for r in case.reports if r.agent == AgentId.agent_2][-1]

    assert report.data.get("matched") is not True
    assert report.resolved is False
    assert "reference_not_found" in {f.code for f in report.findings}


def test_recorded_reference_wins_over_a_wrong_quoted_reference(workspace_tmp_path):
    """If GhostLedger knows the real reference, a typo in the PDF is not fatal."""

    orchestrator, database = build(workspace_tmp_path, "typo")
    paid = pay_invoice(orchestrator)

    case = inquiry(orchestrator, reference="MB-TYPO-IN-THE-PDF")
    report = [r for r in case.reports if r.agent == AgentId.agent_2][-1]

    assert report.data.get("matched") is True
    evidence = report.data.get("matched_transactions") or []
    assert evidence and evidence[0]["payment_reference"] == paid.payment.provider_reference


def test_failed_bank_record_does_not_verify(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "failedrec")
    analysis = build_analysis(
        invoice_number=INVOICE, vendor=VENDOR, customer=CUSTOMER,
        total=AMOUNT, subtotal=AMOUNT, tax="0.00",
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))
    orchestrator.approve(case.case_id, approved_by="approver@example.test", simulate="FAILED")

    enquiry = inquiry(orchestrator)
    report = [r for r in enquiry.reports if r.agent == AgentId.agent_2][-1]
    # A failed instruction is not money received.
    assert report.data.get("matched") is not True


def test_pending_bank_record_does_not_verify(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "pendingrec")
    analysis = build_analysis(
        invoice_number=INVOICE, vendor=VENDOR, customer=CUSTOMER,
        total=AMOUNT, subtotal=AMOUNT, tax="0.00",
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))
    orchestrator.approve(case.case_id, approved_by="approver@example.test", simulate="PENDING")

    enquiry = inquiry(orchestrator)
    report = [r for r in enquiry.reports if r.agent == AgentId.agent_2][-1]
    assert report.data.get("matched") is not True


# ---------------------------------------------------------------------------
# The failure reason must be visible
# ---------------------------------------------------------------------------
def test_failure_reason_is_reported(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "reason")
    # Nothing was ever paid for this invoice.
    case = inquiry(orchestrator, invoice="INV-NEVER-PAID-0001", paid=AMOUNT)

    report = [r for r in case.reports if r.agent == AgentId.agent_2][-1]
    assert report.resolved is False
    detail = " ".join(f.detail for f in report.findings)
    assert "1200.00" in detail or "no bank transaction" in detail.lower()
