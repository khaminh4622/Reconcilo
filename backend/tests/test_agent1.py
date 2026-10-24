"""Tests for Agent 1 - the Duplicate Invoice Investigator.

Covers the ten required scenarios. The invoice history is built inside each
test so the expected matches are explicit and nothing depends on the
developer's local database.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from agent3.schemas import AnalysisResult, PrimaryProblem, ProblemType
from workflow.agent1_duplicates import DuplicateInvestigator
from workflow.agent1_engine import (
    BAND_HIGH,
    is_recurring_pattern,
    score_candidate,
)
from workflow.agent1_history import (
    InvoiceHistoryRepository,
    normalize_invoice_number,
    normalize_name,
)
from workflow.agent1_schemas import InvestigationStatus
from workflow.orchestrator import WorkflowError, WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, CaseStatus, Intent, SyntheticTransaction
from workflow.seed import synthetic_bank_seed

from test_workflow import build_analysis


# ---------------------------------------------------------------------------
# Fixtures: an invoice history to compare against
# ---------------------------------------------------------------------------
def seed_invoice_history(
    database: Path,
    records: list[dict],
) -> None:
    """Insert prior invoices into ``document_records`` (the Agent 3 table)."""

    conn = sqlite3.connect(database)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS document_records (
            record_id TEXT PRIMARY KEY,
            file_hash TEXT NOT NULL,
            document_identifier TEXT,
            document_type TEXT NOT NULL,
            vendor_name TEXT,
            customer_name TEXT,
            total_amount TEXT,
            issue_date TEXT,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for index, record in enumerate(records):
        result = {
            "currency": record.get("currency", "USD"),
            "financial_information": {"currency": record.get("currency", "USD")},
            "line_items": [{"description": record["description"]}] if record.get("description") else [],
            "vendor": {"vendor_name": record.get("vendor")},
        }
        conn.execute(
            """
            INSERT INTO document_records (
                record_id, file_hash, document_identifier, document_type, vendor_name,
                customer_name, total_amount, issue_date, result_json
            ) VALUES (?, ?, ?, 'invoice', ?, ?, ?, ?, ?)
            """,
            (
                record.get("record_id") or f"rec-{index:03d}",
                f"hash-{index}",
                record.get("invoice_number"),
                record.get("vendor"),
                record.get("customer"),
                str(record["amount"]) if record.get("amount") is not None else None,
                record.get("issue_date"),
                json.dumps(result),
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def setup(workspace_tmp_path):
    """Return a helper that builds an orchestrator over a given invoice history."""

    def _build(history: list[dict], transactions=None):
        database = workspace_tmp_path / f"a1-{len(history)}-{id(history)}.db"
        WorkflowRepository(database)  # creates the workflow tables
        seed_invoice_history(database, history)
        repository = WorkflowRepository(database)
        repository.seed_bank_transactions(transactions or [])
        return WorkflowOrchestrator(repository), database

    return _build


def investigate(orchestrator, **kwargs) -> object:
    """Run Agent 1 directly on an analysis built from ``kwargs``."""

    analysis = build_analysis(**kwargs)
    case = orchestrator.open_case(analysis, "invoice.pdf")
    return orchestrator.duplicates.build_investigation(case, analysis)


# ---------------------------------------------------------------------------
# 1. Two identical invoice submissions
# ---------------------------------------------------------------------------
def test_identical_invoice_is_a_confirmed_duplicate(setup):
    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-500",
                "vendor": "Northstar Office Supplies LLC",
                "customer": "Blue Harbor Technologies Inc.",
                "amount": "1200.00",
                "issue_date": "2026-03-01",
                "description": "Managed print services March",
            }
        ]
    )
    result = investigate(
        orchestrator,
        invoice_number="INV-500",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="1200.00",
        subtotal="1200.00",
        tax="0.00",
    )

    assert result.status == InvestigationStatus.confirmed_duplicate_invoice
    assert result.history_available is True
    assert any(c.is_exact_duplicate for c in result.candidates)
    assert "duplicate" in result.summary.lower()
    # The original invoice is unpaid, so a repeat submission is NOT blocked;
    # it reuses the existing obligation and awaits approval.
    assert result.previous_payment_status is not None
    assert result.previous_payment_status.value == "UNPAID_ELIGIBLE_FOR_APPROVAL"
    assert result.blocks_payment is False
    assert result.obligation_id is not None


# ---------------------------------------------------------------------------
# 2. Same invoice with slightly different formatting
# ---------------------------------------------------------------------------
def test_different_formatting_still_matches(setup):
    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-2026-1042",
                "vendor": "Northstar Office Supplies LLC",
                "customer": "Blue Harbor Technologies Inc.",
                "amount": "1944.00",
                "issue_date": "2026-02-04",
                "description": "Managed print services February",
            }
        ]
    )
    result = investigate(
        orchestrator,
        invoice_number="inv 2026 1042",              # spacing and case differ
        vendor="NORTHSTAR OFFICE SUPPLIES, L.L.C.",  # punctuation and suffix differ
        customer="blue harbor technologies inc",
        total="1944.00",
        subtotal="1800.00",
        tax="144.00",
    )

    assert result.status == InvestigationStatus.confirmed_duplicate_invoice
    # Normalisation is what makes this work.
    assert normalize_invoice_number("inv 2026 1042") == normalize_invoice_number("INV-2026-1042")
    assert normalize_name("NORTHSTAR OFFICE SUPPLIES, L.L.C.") == normalize_name("Northstar Office Supplies LLC")


# ---------------------------------------------------------------------------
# 3. Different invoice numbers, same vendor / amount / description
# ---------------------------------------------------------------------------
def test_same_vendor_amount_description_different_number(setup):
    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-700",
                "vendor": "Northstar Office Supplies LLC",
                "customer": "Blue Harbor Technologies Inc.",
                "amount": "1944.00",
                "issue_date": "2026-02-04",
                "description": "Managed print services monthly retainer",
            }
        ]
    )
    result = investigate(
        orchestrator,
        invoice_number="INV-701",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="1944.00",
        subtotal="1800.00",
        tax="144.00",
        issue_date="2026-02-04",
    )

    assert result.best_match is not None
    assert result.best_match.match_score >= BAND_HIGH
    assert result.best_match.is_exact_duplicate is False
    assert result.status == InvestigationStatus.possible_duplicate
    # Still unpaid, so the obligation is reused for approval rather than blocked.
    assert result.previous_payment_status.value == "UNPAID_ELIGIBLE_FOR_APPROVAL"
    assert result.blocks_payment is False
    # Individual signals are reported, not just a bare score.
    names = {signal.signal for signal in result.best_match.signals}
    assert {"vendor", "amount", "currency", "invoice_date"} <= names


def test_same_amount_alone_is_not_a_duplicate(setup):
    """Amount is not sufficient evidence on its own."""

    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-800",
                "vendor": "A Completely Different Vendor",
                "customer": "Blue Harbor Technologies Inc.",
                "amount": "1944.00",
                "issue_date": "2025-01-01",
                "description": "Office furniture delivery",
            }
        ]
    )
    result = investigate(
        orchestrator,
        invoice_number="INV-801",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="1944.00",
        subtotal="1800.00",
        tax="144.00",
        issue_date="2026-06-01",
    )
    assert result.status != InvestigationStatus.confirmed_duplicate_invoice
    assert result.history_available is True


# ---------------------------------------------------------------------------
# 4. Two legitimate recurring monthly invoices
# ---------------------------------------------------------------------------
def test_recurring_monthly_invoice_is_not_a_duplicate(setup):
    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-JAN",
                "vendor": "Northstar Office Supplies LLC",
                "customer": "Blue Harbor Technologies Inc.",
                "amount": "1944.00",
                "issue_date": "2026-01-04",
                "description": "Managed print services monthly retainer",
            }
        ]
    )
    result = investigate(
        orchestrator,
        invoice_number="INV-FEB",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="1944.00",
        subtotal="1800.00",
        tax="144.00",
        issue_date="2026-02-04",  # exactly one month later
    )

    assert result.best_match is not None
    assert is_recurring_pattern(result.best_match) is True
    assert result.status == InvestigationStatus.no_duplicate_found
    assert result.blocks_payment is False


# ---------------------------------------------------------------------------
# 5. One invoice with two successful synthetic payment records
# ---------------------------------------------------------------------------
def test_two_successful_settlements_confirm_duplicate_payment(setup):
    orchestrator, _ = setup(
        [],
        transactions=[
            SyntheticTransaction(
                reference="SETTLE-1", amount=Decimal("500.00"), currency="USD",
                direction="credit", counterparty="Northstar Office Supplies LLC",
                status="settled", value_date="2026-03-01",
            ),
            SyntheticTransaction(
                reference="SETTLE-2", amount=Decimal("500.00"), currency="USD",
                direction="credit", counterparty="Northstar Office Supplies LLC",
                status="settled", value_date="2026-03-05",
            ),
        ],
    )
    result = investigate(
        orchestrator,
        invoice_number="INV-DUP-PAY",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="500.00",
        subtotal="500.00",
        tax="0.00",
        paid="1000.00",
        payment_count=2,
    )

    payment = result.payment_analysis
    assert payment.verified_settlements == 2
    assert payment.confirmed_duplicate_payment is True
    assert result.status == InvestigationStatus.confirmed_duplicate_payment
    assert result.blocks_payment is True


# ---------------------------------------------------------------------------
# 6. One failed attempt followed by one successful payment
# ---------------------------------------------------------------------------
def test_failed_attempt_then_success_is_not_a_duplicate(setup):
    orchestrator, database = setup([])
    repository = orchestrator.repository
    # Record one failed and one successful attempt for the same invoice.
    from workflow.schemas import PaymentAttempt

    repository.save_attempt(
        "case-x",
        PaymentAttempt(
            idempotency_key="key-failed", invoice_number="INV-RETRY",
            payee="Northstar Office Supplies LLC", amount=Decimal("300.00"),
            currency="USD", status="FAILED", failure_reason="insufficient funds",
        ),
    )
    repository.save_attempt(
        "case-x",
        PaymentAttempt(
            idempotency_key="key-success", invoice_number="INV-RETRY",
            payee="Northstar Office Supplies LLC", amount=Decimal("300.00"),
            currency="USD", status="SUCCESS",
        ),
    )

    analysis = build_analysis(
        invoice_number="INV-RETRY", vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.", total="300.00",
        subtotal="300.00", tax="0.00",
    )
    case = orchestrator.open_case(analysis, "retry.pdf")
    result = orchestrator.duplicates.build_investigation(case, analysis)

    payment = result.payment_analysis
    assert payment.failed_attempts == 1
    assert payment.successful_settlements == 1
    # One failed attempt does not mean money moved twice.
    assert payment.confirmed_duplicate_payment is False
    assert result.status != InvestigationStatus.confirmed_duplicate_payment


# ---------------------------------------------------------------------------
# 7. Two customer-reported but unverified payments
# ---------------------------------------------------------------------------
def test_two_reported_payments_are_only_suspected(setup):
    orchestrator, _ = setup([])  # no bank records at all
    result = investigate(
        orchestrator,
        invoice_number="INV-REPORTED",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="1944.00",
        subtotal="1800.00",
        tax="144.00",
        paid="3888.00",
        payment_count=2,
    )

    payment = result.payment_analysis
    assert payment.customer_reported == 2
    assert payment.verified_settlements == 0
    assert payment.duplicate_payment_suspected is True
    # Reports are claims, so this cannot be confirmed.
    assert payment.confirmed_duplicate_payment is False
    assert result.status == InvestigationStatus.suspected_duplicate_payment
    assert result.needs_agent_2 is True
    assert result.blocks_payment is True


# ---------------------------------------------------------------------------
# 8. No matching invoice in the database
# ---------------------------------------------------------------------------
def test_no_match_reports_no_duplicate(setup):
    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-OTHER",
                "vendor": "Totally Different Supplier",
                "customer": "Blue Harbor Technologies Inc.",
                "amount": "77.00",
                "issue_date": "2026-01-01",
                "description": "Stationery",
            }
        ]
    )
    result = investigate(
        orchestrator,
        invoice_number="INV-BRAND-NEW",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="5000.00",
        subtotal="5000.00",
        tax="0.00",
        issue_date="2026-07-15",
    )

    assert result.status == InvestigationStatus.no_duplicate_found
    assert result.history_available is True
    assert result.records_compared >= 1
    assert result.blocks_payment is False


def test_empty_history_is_not_proof_of_uniqueness(setup):
    """An empty database must never yield NO_DUPLICATE_FOUND."""

    orchestrator, _ = setup([])
    result = investigate(
        orchestrator,
        invoice_number="INV-UNIQUE",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="100.00",
        subtotal="100.00",
        tax="0.00",
    )

    assert result.history_available is False
    assert result.status == InvestigationStatus.insufficient_information
    assert result.status != InvestigationStatus.no_duplicate_found
    assert "no comparison records" in result.summary.lower()
    assert result.needs_human_review is True


# ---------------------------------------------------------------------------
# 9. Same invoice number used by two different vendors
# ---------------------------------------------------------------------------
def test_same_number_different_vendor_is_not_a_confirmed_duplicate(setup):
    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-SHARED-1",
                "vendor": "Northstar Office Supplies LLC",
                "customer": "Blue Harbor Technologies Inc.",
                "amount": "1200.00",
                "issue_date": "2026-03-01",
                "description": "Print services",
            }
        ]
    )
    result = investigate(
        orchestrator,
        invoice_number="INV-SHARED-1",
        vendor="A Totally Different Vendor Ltd",
        customer="Blue Harbor Technologies Inc.",
        total="900.00",
        subtotal="900.00",
        tax="0.00",
        issue_date="2026-05-01",
    )

    assert result.status != InvestigationStatus.confirmed_duplicate_invoice
    # The reused number is still surfaced as evidence worth a look.
    best = result.best_match
    assert best is not None
    assert any("already on record" in item for item in best.evidence) or best.match_score < BAND_HIGH


# ---------------------------------------------------------------------------
# 10. Missing vendor or invoice information
# ---------------------------------------------------------------------------
def test_missing_information_is_insufficient_not_unique(setup):
    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-1",
                "vendor": "Northstar Office Supplies LLC",
                "customer": "Blue Harbor Technologies Inc.",
                "amount": "100.00",
                "issue_date": "2026-01-01",
            }
        ]
    )
    analysis = build_analysis(invoice_number="", vendor=None, customer="Blue Harbor Technologies Inc.")
    case = orchestrator.open_case(analysis, "blank.pdf")
    result = orchestrator.duplicates.build_investigation(case, analysis)

    assert result.status == InvestigationStatus.insufficient_information
    assert result.blocks_payment is True
    assert result.needs_human_review is True


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------
def test_matching_is_scoped_to_the_same_customer(setup):
    """Another customer's identical invoice must not count as a duplicate."""

    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-SAME",
                "vendor": "Northstar Office Supplies LLC",
                "customer": "A Different Customer Inc.",
                "amount": "1200.00",
                "issue_date": "2026-03-01",
                "description": "Managed print services",
            }
        ]
    )
    result = investigate(
        orchestrator,
        invoice_number="INV-SAME",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="1200.00",
        subtotal="1200.00",
        tax="0.00",
        issue_date="2026-03-01",
    )

    assert result.records_compared == 0
    assert result.status == InvestigationStatus.insufficient_information
    assert result.status != InvestigationStatus.confirmed_duplicate_invoice


# ---------------------------------------------------------------------------
# Integration with the orchestrator
# ---------------------------------------------------------------------------
def test_agent_3_routes_duplicate_invoice_to_agent_1(setup):
    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-2026-0441",
                "vendor": "Harborline Office Supply",
                "customer": "Avery Nguyen",
                "amount": "410.40",
                "issue_date": "2026-03-02",
                "description": "Ergonomic desk chair",
            }
        ]
    )
    analysis = build_analysis(
        invoice_number="INV-2026-0441",
        vendor="Harborline Office Supply",
        customer="Avery Nguyen",
        total="410.40",
        subtotal="400.00",
        tax="10.40",
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.duplicate_invoice,
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "dup.pdf"))

    assert case.routing.primary_agent == AgentId.agent_1
    assert any(report.agent == AgentId.agent_1 for report in case.reports)
    agent1 = next(r for r in case.reports if r.agent == AgentId.agent_1)
    assert agent1.data["investigation"]["status"] == "CONFIRMED_DUPLICATE_INVOICE"


def test_suspected_duplicate_payment_requests_agent_2(setup):
    orchestrator, _ = setup([])
    analysis = build_analysis(
        invoice_number="INV-SUSPECT",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="1944.00",
        subtotal="1800.00",
        tax="144.00",
        paid="3888.00",
        payment_count=2,
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.duplicate_payment,
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "suspect.pdf"))

    path = [run.agent for run in sorted(case.agent_runs, key=lambda r: r.order)]
    assert path[:3] == [AgentId.agent_3, AgentId.agent_1, AgentId.agent_2]
    # Agent 1 produces the final report after Agent 2 returns.
    agent1_reports = [r for r in case.reports if r.agent == AgentId.agent_1]
    assert len(agent1_reports) >= 1


def test_already_paid_duplicate_blocks_payment_execution(setup):
    """Case A: a duplicate of an invoice that was already paid is blocked."""

    orchestrator, _ = setup(
        [
            {
                "invoice_number": "INV-BLOCK",
                "vendor": "Northstar Office Supplies LLC",
                "customer": "Blue Harbor Technologies Inc.",
                "amount": "1200.00",
                "issue_date": "2026-03-01",
                "description": "Print services",
            }
        ],
        transactions=[
            SyntheticTransaction(
                reference="PAID-BLOCK", amount=Decimal("1200.00"), currency="USD",
                direction="credit", counterparty="Northstar Office Supplies LLC",
                status="settled", value_date="2026-03-05",
            )
        ],
    )
    analysis = build_analysis(
        invoice_number="INV-BLOCK",
        vendor="Northstar Office Supplies LLC",
        customer="Blue Harbor Technologies Inc.",
        total="1200.00",
        subtotal="1200.00",
        tax="0.00",
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.duplicate_invoice,
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "block.pdf"))

    from workflow.schemas import ApprovalRecord

    case.approval = ApprovalRecord(
        approved_by="tester", invoice_number="INV-BLOCK",
        payee="Northstar Office Supplies LLC", amount=Decimal("1200.00"), currency="USD",
    )
    with pytest.raises(WorkflowError, match="blocked|duplicate"):
        orchestrator.execute_payment(case.case_id)


def test_agent_1_never_refunds_or_pays(setup):
    orchestrator, _ = setup([])
    analysis = build_analysis(
        invoice_number="INV-X", vendor="V", customer="C", total="10.00",
        subtotal="10.00", tax="0.00",
        problem=ProblemType.payment_status_discrepancy,
        primary=PrimaryProblem.duplicate_payment,
    )
    case = orchestrator.open_case(analysis, "x.pdf")
    report = orchestrator.duplicates.investigate(case, analysis)
    assert report.agent == AgentId.agent_1
    # Agent 1 produces no payment attempt and no bank transaction.
    assert orchestrator.repository.list_attempts(case.case_id) == []
    assert case.payment is None


# ---------------------------------------------------------------------------
# Scoring unit behaviour
# ---------------------------------------------------------------------------
def test_score_components_sum_to_total(setup):
    orchestrator, _ = setup([])
    history = InvoiceHistoryRepository(Path(".test-scratch"))  # unused; structural check only
    from workflow.agent1_history import InvoiceHistoryRecord

    prior = InvoiceHistoryRecord(
        record_id="r1", invoice_number="INV-1", vendor="Acme LLC",
        customer="Cust", amount=Decimal("100.00"), currency="USD",
        issue_date="2026-01-01", description="Consulting services",
        normalized_vendor="acme", normalized_customer="cust", normalized_number="inv1",
    )
    candidate = score_candidate(
        vendor="Acme LLC", customer="Cust", amount=Decimal("100.00"), currency="USD",
        issue_date="2026-01-01", description="Consulting services", prior=prior,
    )
    assert candidate.match_score == sum(signal.score for signal in candidate.signals)
    assert candidate.match_score == 100  # everything matches
