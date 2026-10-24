"""Agent 4's LLM investigation report.

The model writes the narrative; the database decides the facts. These tests pin
both halves: a report grounded in a real failure code must be accepted, and a
report that invents a cause when the bank has no record must be rejected.

No network is used: every "model" here is a stub that returns a fixed reply.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from agent3.extractor import LLMExtractor
from workflow.agent4_llm import (
    Agent4ReportWriter,
    InvestigationFacts,
    REQUIRED_KEYS,
    build_report_writer,
)
from workflow.failure_simulator import FailureSimulator, SIMULATABLE, failure_scenarios
from workflow.mock_bank import FAILURE_CODES, MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, CaseStatus

from test_workflow import build_analysis


VENDOR = "Northstar Cloud Systems LLC"
CUSTOMER = "Redwood Learning Studio LLC"
AMOUNT = "1250.00"


# ---------------------------------------------------------------------------
# Stub model
# ---------------------------------------------------------------------------
class StubClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.reply


def stub_extractor(reply: str) -> LLMExtractor:
    extractor = object.__new__(LLMExtractor)
    extractor.client = StubClient(reply)
    return extractor


def report_json(**overrides) -> str:
    base = {
        "customer_problem": "The customer reported that their payment failed.",
        "investigation_summary": (
            "GhostLedger checked the payment attempt, the Mock Bank ledger and the audit trail."
        ),
        "bank_findings": "The Mock Bank recorded a FAILED instruction with code INSUFFICIENT_FUNDS.",
        "root_cause": "The sender account did not hold sufficient cleared funds.",
        "recommended_next_steps": "Confirm the balance, then obtain fresh human approval.",
        "customer_explanation": "Your payment did not complete; no money left the account.",
        "human_review_required": True,
        "evidence_gaps": [],
    }
    base.update(overrides)
    return json.dumps(base)


def grounded_facts() -> InvestigationFacts:
    return InvestigationFacts(
        customer_claims={"reported_problem": "payment failed"},
        bank_facts={"bank_status": "FAILED", "bank_failure_code": "INSUFFICIENT_FUNDS"},
        failure_reason_code="INSUFFICIENT_FUNDS",
        settlement_status="FAILED",
        has_failed_bank_transaction=True,
        retry_permitted=False,
        requires_human=True,
    )


def evidence_free_facts() -> InvestigationFacts:
    return InvestigationFacts(
        customer_claims={"reported_problem": "payment failed"},
        bank_facts={"bank_status": None},
        unknown=["The Mock Bank holds no transaction for this payment."],
        failure_reason_code=None,
        settlement_status=None,
        has_failed_bank_transaction=False,
        retry_permitted=False,
        requires_human=True,
    )


# ---------------------------------------------------------------------------
# The writer accepts grounded reports
# ---------------------------------------------------------------------------
def test_grounded_report_is_accepted():
    writer = Agent4ReportWriter(stub_extractor(report_json()))
    report = writer.write(grounded_facts())

    assert report is not None
    for key in REQUIRED_KEYS:
        assert report[key], f"{key} must be populated"
    assert report["human_review_required"] is True


def test_prompt_separates_claims_from_facts():
    client = StubClient(report_json())
    extractor = object.__new__(LLMExtractor)
    extractor.client = client
    Agent4ReportWriter(extractor).write(grounded_facts())

    system, user = client.calls[0]
    assert "CUSTOMER CLAIMS" in system
    assert "VERIFIED BANK FACTS" in system
    assert "UNKNOWN" in system
    payload = json.loads(user)
    assert "customer_claims" in payload
    assert "verified_bank_facts" in payload
    assert "unknown" in payload


def test_json_wrapped_in_prose_is_recovered():
    reply = "Here is the report:\n" + report_json()
    report = Agent4ReportWriter(stub_extractor(reply)).write(grounded_facts())
    assert report is not None


def test_code_fenced_json_is_recovered():
    reply = "```json\n" + report_json() + "\n```"
    report = Agent4ReportWriter(stub_extractor(reply)).write(grounded_facts())
    assert report is not None


# ---------------------------------------------------------------------------
# ...and rejects unsafe ones
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "phrase",
    ["insufficient funds", "invalid account", "payment rejected", "bank timeout",
     "the bank declined", "account closed"],
)
def test_invented_reason_without_bank_evidence_is_rejected(phrase):
    reply = report_json(root_cause=f"The payment failed due to {phrase}.")
    report = Agent4ReportWriter(stub_extractor(reply)).write(evidence_free_facts())
    assert report is None, f"an invented reason ({phrase}) must be rejected"


def test_unknown_root_cause_is_accepted_without_bank_evidence():
    reply = report_json(
        bank_findings="The Mock Bank holds no record of this payment.",
        root_cause="Unknown - the records do not establish a cause",
        customer_explanation="We cannot yet confirm this payment.",
    )
    report = Agent4ReportWriter(stub_extractor(reply)).write(evidence_free_facts())
    assert report is not None
    assert "unknown" in report["root_cause"].casefold()


@pytest.mark.parametrize(
    "promise",
    ["We will retry the payment.", "We will refund you.", "Payment has been retried."],
)
def test_retry_and_refund_promises_are_rejected(promise):
    reply = report_json(recommended_next_steps=promise)
    report = Agent4ReportWriter(stub_extractor(reply)).write(grounded_facts())
    assert report is None


def test_negated_retry_statement_is_allowed():
    """'No automatic retry will occur' is correct and must survive."""

    reply = report_json(
        recommended_next_steps="No automatic retry will occur; a human must approve any new attempt."
    )
    report = Agent4ReportWriter(stub_extractor(reply)).write(grounded_facts())
    assert report is not None


def test_claiming_settlement_when_not_settled_is_rejected():
    reply = report_json(bank_findings="The payment settled successfully.")
    report = Agent4ReportWriter(stub_extractor(reply)).write(grounded_facts())
    assert report is None


def test_denying_human_review_when_required_is_rejected():
    reply = report_json(human_review_required=False)
    report = Agent4ReportWriter(stub_extractor(reply)).write(grounded_facts())
    assert report is None


def test_unusable_replies_are_rejected():
    for reply in ("", "not json", "{}", report_json(investigation_summary="")[:40]):
        assert Agent4ReportWriter(stub_extractor(reply)).write(grounded_facts()) is None


def test_writer_is_unavailable_without_a_live_model():
    class NotAModel:
        pass

    writer = build_report_writer(NotAModel())
    assert writer.available is False
    assert writer.write(grounded_facts()) is None


# ---------------------------------------------------------------------------
# End-to-end: Agent 4 uses the model with real records
# ---------------------------------------------------------------------------
def build(workspace_tmp_path, name: str):
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    repository = WorkflowRepository(database)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.execution.bank = MockBank(database)
    return orchestrator, database


def complaint(orchestrator, invoice):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total=AMOUNT, subtotal=AMOUNT, tax="0.00",
        problem="payment_status_discrepancy", primary="payment_failure",
    )
    return orchestrator.route(orchestrator.open_case(analysis, "complaint.pdf"))


def test_known_failure_reason_is_explained(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "known")
    orchestrator.exceptions.report_writer = Agent4ReportWriter(stub_extractor(report_json()))

    analysis = build_analysis(
        invoice_number="INV-E2E-1", vendor=VENDOR, customer=CUSTOMER,
        total=AMOUNT, subtotal=AMOUNT, tax="0.00",
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))
    orchestrator.approve(case.case_id, approved_by="approver@example.test",
                         simulate="FAILED", failure_code="INSUFFICIENT_FUNDS")

    enquiry = complaint(orchestrator, "INV-E2E-1")
    report = [r for r in enquiry.reports if r.agent == AgentId.agent_4][-1]

    assert report.data["failure_code"] == "INSUFFICIENT_FUNDS"
    assert report.data["llm_report"] is not None
    assert "INSUFFICIENT_FUNDS" in report.data["llm_report"]["bank_findings"]
    assert report.data["customer_claims"]
    assert report.requires_human is True


def test_missing_bank_evidence_stays_safe(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "nobank")
    orchestrator.exceptions.report_writer = Agent4ReportWriter(
        stub_extractor(report_json(
            bank_findings="The Mock Bank holds no record of this payment.",
            root_cause="Unknown - the records do not establish a cause",
        ))
    )
    enquiry = complaint(orchestrator, "INV-NEVER-PAID")
    report = [r for r in enquiry.reports if r.agent == AgentId.agent_4][-1]

    assert report.data["failure_code"] is None
    assert report.data["bank_evidence"] is None
    assert report.data["unknown"]
    assert report.requires_human is True


def test_model_failure_falls_back_to_deterministic_report(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "fallback")
    orchestrator.exceptions.report_writer = Agent4ReportWriter(stub_extractor("garbage"))
    enquiry = complaint(orchestrator, "INV-FALLBACK")
    report = [r for r in enquiry.reports if r.agent == AgentId.agent_4][-1]

    assert report.data["llm_report"] is None
    # The deterministic explanation is still present and useful.
    assert report.data["diagnosis"]
    assert report.data["customer_explanation"]


# ---------------------------------------------------------------------------
# Developer failure simulator
# ---------------------------------------------------------------------------
def test_simulator_covers_the_required_codes():
    codes = {row["code"] for row in failure_scenarios()}
    assert {"INSUFFICIENT_FUNDS", "INVALID_ACCOUNT", "PAYMENT_REJECTED", "BANK_TIMEOUT"} <= codes


def test_simulated_failure_does_not_change_settled_balance(workspace_tmp_path):
    database = workspace_tmp_path / "sim.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    WorkflowRepository(database)
    bank = MockBank(database)
    bank.submit_instruction(
        idempotency_key="seed", amount=Decimal("5000.00"), currency="USD",
        payee="Real Payee", invoice_number="INV-SEED", scenario="SUCCESS",
    )
    before = bank.balances()

    simulator = FailureSimulator(database)
    for code in sorted(SIMULATABLE):
        simulator.simulate(invoice_number=f"INV-{code}", amount=Decimal(AMOUNT), failure_code=code)

    assert bank.balances() == before, "failed simulations must never credit the account"


def test_simulator_is_idempotent(workspace_tmp_path):
    database = workspace_tmp_path / "simidem.db"
    WorkflowRepository(database)
    simulator = FailureSimulator(database)

    first = simulator.simulate(invoice_number="INV-SIM", amount=Decimal(AMOUNT),
                               failure_code="BANK_TIMEOUT")
    second = simulator.simulate(invoice_number="INV-SIM", amount=Decimal(AMOUNT),
                                failure_code="BANK_TIMEOUT")

    assert first["created"] is True
    assert second["created"] is False
    assert first["payment_reference"] == second["payment_reference"]
    assert MockBank(database).count() == 1


def test_simulator_rejects_unknown_codes(workspace_tmp_path):
    from workflow.failure_simulator import FailureSimulationError

    database = workspace_tmp_path / "simbad.db"
    WorkflowRepository(database)
    with pytest.raises(FailureSimulationError):
        FailureSimulator(database).simulate(
            invoice_number="INV-X", amount=Decimal("10.00"), failure_code="MADE_UP"
        )


def test_simulator_records_a_failed_attempt_for_agent4(workspace_tmp_path):
    database = workspace_tmp_path / "simattempt.db"
    WorkflowRepository(database)
    result = FailureSimulator(database).simulate(
        invoice_number="INV-SIMATT", amount=Decimal(AMOUNT), failure_code="INVALID_ACCOUNT"
    )
    from workflow.payment_store import PaymentRecordStore

    attempts = PaymentRecordStore(database).list_for_invoice("INV-SIMATT")
    assert attempts
    assert attempts[-1].status == "FAILED"
    assert attempts[-1].failure_code == "INVALID_ACCOUNT"
    assert result["bank_record"]["received_amount"] is None


def test_developer_endpoints_need_no_separate_key(workspace_tmp_path, monkeypatch):
    """The simulator is a local developer tool; it asks for no second secret."""

    from fastapi.testclient import TestClient
    from main import create_app

    database = workspace_tmp_path / "devep.db"
    WorkflowRepository(database)
    app = create_app()
    app.state.workflow_repository = WorkflowRepository(database)
    client = TestClient(app)

    # Available straight away: no key, no environment variable.
    monkeypatch.delenv("GHOSTLEDGER_DEVELOPER_KEY", raising=False)
    scenarios = client.get("/developer/failure-scenarios")
    assert scenarios.status_code == 200
    assert scenarios.json()["scenarios"]

    created = client.post(
        "/developer/simulate-failure",
        json={"invoice_number": "INV-API-SIM", "amount": "1250.00",
              "failure_code": "PAYMENT_REJECTED"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["simulated"] is True
    assert body["failure_code"] == "PAYMENT_REJECTED"
    # A simulated failure never credits the account.
    assert body["settled_balance"] == {}


def test_simulator_can_be_switched_off(workspace_tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from main import create_app

    database = workspace_tmp_path / "devoff.db"
    WorkflowRepository(database)
    app = create_app()
    app.state.workflow_repository = WorkflowRepository(database)
    client = TestClient(app)

    monkeypatch.setenv("GHOSTLEDGER_DISABLE_SIMULATOR", "1")
    assert client.get("/developer/failure-scenarios").status_code == 403
    assert client.post("/developer/simulate-failure", json={}).status_code == 403
