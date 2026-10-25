"""Typed schemas for the five-agent GhostLedger workflow.

Agent 3's document analysis already lives in ``agent3.schemas``. This module adds
the shared vocabulary the agents and orchestrator exchange: intents, case
statuses, structured agent reports, approvals, synthetic transactions, and audit
records. Every handoff between agents is one of these typed objects rather than
free text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Intents and routing targets
# ---------------------------------------------------------------------------
class Intent(str, Enum):
    """What the customer is asking GhostLedger to do."""

    new_payment_request = "new_payment_request"
    duplicate_invoice = "duplicate_invoice"
    duplicate_payment = "duplicate_payment"
    bank_reconciliation = "bank_reconciliation"
    payment_failure = "payment_failure"
    payment_status_dispute = "payment_status_dispute"
    # "Did my earlier payment arrive?" - answered from the Mock Bank ledger.
    payment_status_check = "PAYMENT_STATUS_CHECK"
    unknown = "unknown"


class WorkflowType(str, Enum):
    """The two top-level workflows Agent 3 can open."""

    new_payment = "new_payment"          # Workflow A: pay a valid invoice
    existing_problem = "existing_problem"  # Workflow B: investigate a reported issue
    human_review = "human_review"        # Could not be understood


class AgentId(str, Enum):
    """Every participant in the workflow, including the human step."""

    agent_1 = "agent_1"
    agent_2 = "agent_2"
    agent_3 = "agent_3"
    agent_4 = "agent_4"
    agent_5 = "agent_5"
    # The payment rail is a service, not an agent: it moves fictional money and
    # reports what the bank did. Separate from Agent 5's final confirmation.
    payment_simulator = "payment_simulator"
    human_approval = "human_approval"
    human_review = "human_review"
    orchestrator = "orchestrator"


AGENT_LABELS: dict[AgentId, str] = {
    AgentId.agent_1: "Agent 1 · Duplicate Invoice Investigator",
    AgentId.agent_2: "Agent 2 · Bank Reconciliation Specialist",
    AgentId.agent_3: "Agent 3 · Financial Intake & Verification Agent",
    AgentId.agent_4: "Agent 4 · Payment Exception Specialist",
    AgentId.agent_5: "Agent 5 · Final Confirmation & Customer Response",
    AgentId.payment_simulator: "Payment Simulator",
    AgentId.human_approval: "Human Approval",
    AgentId.human_review: "Human Review",
    AgentId.orchestrator: "Workflow Orchestrator",
}


class CaseStatus(str, Enum):
    received = "RECEIVED"
    analyzing = "ANALYZING"
    validating = "VALIDATING"
    routed = "ROUTED"
    investigating = "INVESTIGATING"
    awaiting_approval = "AWAITING_APPROVAL"
    approved = "APPROVED"
    executing = "EXECUTING"
    pending = "PENDING"
    completed = "COMPLETED"
    failed = "FAILED"
    requires_review = "REQUIRES_REVIEW"


class AgentRunStatus(str, Enum):
    """Per-agent status shown in the workflow visualisation."""

    waiting = "waiting"
    running = "running"
    completed = "completed"
    blocked = "blocked"
    requires_human = "requires_human"
    skipped = "skipped"


TERMINAL_CASE_STATUSES = {
    CaseStatus.completed,
    CaseStatus.failed,
    CaseStatus.requires_review,
}


# ---------------------------------------------------------------------------
# Agent reports
# ---------------------------------------------------------------------------
class Finding(WorkflowModel):
    """One observation an agent made, with an explicit outcome."""

    code: str
    label: str
    status: Literal["passed", "failed", "unverified", "info"]
    detail: str


class AgentReport(WorkflowModel):
    """The structured result of one agent's work.

    Every agent returns this shape, so the orchestrator and UI never need to
    special-case any individual agent.
    """

    agent: AgentId
    agent_name: str
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    recommended_next: AgentId | None = None
    next_action: str | None = None
    requires_human: bool = False
    blocking: bool = False
    resolved: bool = False
    data: dict = Field(default_factory=dict)
    started_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None


class RoutingDecisionView(WorkflowModel):
    """Agent 3's routing advice, validated by the orchestrator."""

    primary_agent: AgentId
    supporting_agent: AgentId | None = None
    intent: Intent
    reason: str
    priority: Literal["normal", "high"] = "normal"
    validation_status: str
    blocking: bool = False
    required_next_action: str
    requires_human_approval: bool = False


# ---------------------------------------------------------------------------
# Documents and cases
# ---------------------------------------------------------------------------
class DocumentRef(WorkflowModel):
    """A document attached to a case."""

    document_id: str = Field(default_factory=lambda: str(uuid4()))
    filename: str
    kind: Literal["invoice", "payment_receipt", "payment_failure_notice", "customer_message", "other"] = "invoice"
    amount: Decimal | None = None
    currency: str | None = None
    vendor_name: str | None = None
    invoice_number: str | None = None


class ApprovalRecord(WorkflowModel):
    """A human authorisation, bound to the exact payment it permits."""

    approval_id: str = Field(default_factory=lambda: str(uuid4()))
    approved_by: str
    approved_at: str = Field(default_factory=utc_now)
    # Binding terms: an approval for a different amount or payee is not valid.
    invoice_number: str | None = None
    payee: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    note: str | None = None
    revoked: bool = False
    # Optional expiry; None means the approval does not expire.
    expires_at: str | None = None

    def is_expired(self, now: str | None = None) -> bool:
        if not self.expires_at:
            return False
        return (now or utc_now()) > self.expires_at


class PaymentAttempt(WorkflowModel):
    """One execution attempt by Agent 5, recorded for auditing and idempotency."""

    attempt_id: str = Field(default_factory=lambda: str(uuid4()))
    idempotency_key: str
    approval_id: str | None = None
    payee: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    invoice_number: str | None = None
    case_id: str | None = None
    # The raw provider view: INITIATED / PROCESSING / SETTLED / FAILED / UNKNOWN.
    provider_status: Literal[
        "INITIATED", "PROCESSING", "SETTLED", "FAILED", "UNKNOWN",
        "PENDING", "PARTIAL", "MISSING_SETTLEMENT",
    ] = "INITIATED"
    # The settled business outcome used by the workflow.
    status: Literal["SUCCESS", "FAILED", "PENDING"] = "PENDING"
    failure_reason: str | None = None
    failure_code: str | None = None
    transaction_id: str | None = None
    # Synthetic provider reference, e.g. SP-1A2B3C4D5E6F.
    provider_reference: str | None = None
    settlement_reference: str | None = None
    settled_at: str | None = None
    verified: bool = False
    attempted_at: str = Field(default_factory=utc_now)


class PaymentReceipt(WorkflowModel):
    """Proof of a verified synthetic settlement, shown to the user."""

    receipt_id: str = Field(default_factory=lambda: str(uuid4()))
    case_id: str
    invoice_number: str | None = None
    vendor_name: str | None = None
    customer_name: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    provider_reference: str | None = None
    settlement_reference: str | None = None
    transaction_id: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    settled_at: str | None = None
    outcome: Literal["SETTLED", "FAILED", "PENDING"] = "SETTLED"
    # Stated plainly on every receipt so nobody mistakes it for real money.
    synthetic: bool = True
    disclaimer: str = (
        "SIMULATED PAYMENT - no real funds were transferred. This receipt is generated "
        "by GhostLedger's synthetic payment provider for testing."
    )
    verification: list[str] = Field(default_factory=list)


class SyntheticTransaction(WorkflowModel):
    """A simulated bank transaction. No real money moves."""

    # Deterministic so re-seeding is idempotent: the same reference, amount and
    # date always produce the same id, which keeps INSERT OR IGNORE honest.
    transaction_id: str | None = None
    reference: str
    amount: Decimal
    currency: str
    direction: Literal["credit", "debit"]
    counterparty: str | None = None
    status: Literal["settled", "pending", "failed", "returned"] = "settled"
    value_date: str | None = None
    # "agent5_settlement" marks a row Agent 5 wrote itself, which Agent 2 must
    # never treat as independent bank evidence.
    source: Literal["customer_report", "synthetic_bank", "agent5_settlement"] = "synthetic_bank"


class AuditEntry(WorkflowModel):
    """One immutable line in the case audit trail."""

    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    at: str = Field(default_factory=utc_now)
    actor: AgentId
    action: str
    detail: str
    status_before: str | None = None
    status_after: str | None = None


class AgentRun(WorkflowModel):
    """A single agent's participation in a case, for the workflow graph."""

    agent: AgentId
    agent_name: str
    status: AgentRunStatus = AgentRunStatus.waiting
    order: int = 0
    # Distinguishes Agent 5's two responsibilities in the same case.
    phase: str | None = None
    summary: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class WorkflowCase(WorkflowModel):
    """The unit of work the orchestrator coordinates."""

    case_id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    status: CaseStatus = CaseStatus.received
    workflow: WorkflowType | None = None
    intent: Intent = Intent.unknown

    customer_name: str | None = None
    vendor_name: str | None = None
    invoice_number: str | None = None
    amount: Decimal | None = None
    currency: str | None = None

    documents: list[DocumentRef] = Field(default_factory=list)
    analysis: dict | None = None
    # The account that owns this case. None for records predating accounts,
    # which are kept in a restricted legacy state rather than auto-assigned.
    owner_account_id: str | None = None
    routing: RoutingDecisionView | None = None
    agent_runs: list[AgentRun] = Field(default_factory=list)
    reports: list[AgentReport] = Field(default_factory=list)
    approval: ApprovalRecord | None = None
    payment: PaymentAttempt | None = None
    # Agent 2's post-payment reconciliation verdict (dict for forward-compat).
    reconciliation: dict[str, Any] | None = None
    # Issued only when a synthetic settlement has been verified.
    receipt: PaymentReceipt | None = None
    # Every attempt for this invoice, retained for audit (never deleted).
    payment_history: list[PaymentAttempt] = Field(default_factory=list)
    # The most recent Agent 5 report, so callers can inspect the execution
    # outcome even after a later agent (e.g. Agent 4) appended its own report.
    last_payment_report: AgentReport | None = None
    audit: list[AuditEntry] = Field(default_factory=list)

    outcome: str | None = None
    requires_human: bool = False
    pending_reason: str | None = None

    def add_audit(self, actor: AgentId, action: str, detail: str, status_after: str | None = None) -> None:
        self.audit.append(
            AuditEntry(
                actor=actor,
                action=action,
                detail=detail,
                status_before=self.status.value,
                status_after=status_after or self.status.value,
            )
        )
        self.updated_at = utc_now()

    def run_for(self, agent: AgentId, phase: str | None = None) -> AgentRun | None:
        """First run for an agent, optionally filtered by phase.

        Agent 5 appears twice (initiation, then confirmation), so callers that
        care about one phase must say which.
        """

        if phase is None:
            return next((run for run in self.agent_runs if run.agent == agent), None)
        return next(
            (run for run in self.agent_runs if run.agent == agent and run.phase == phase),
            None,
        )


class CaseSummary(WorkflowModel):
    """Lightweight case listing for the dashboard."""

    case_id: str
    created_at: str
    updated_at: str
    status: CaseStatus
    workflow: WorkflowType | None = None
    intent: Intent
    customer_name: str | None = None
    invoice_number: str | None = None
    vendor_name: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    outcome: str | None = None
    requires_human: bool = False
    path: list[str] = Field(default_factory=list)
    # Triage fields, so a human can act without opening the case.
    why: str | None = None
    problems: list[str] = Field(default_factory=list)
    next_action: str | None = None
    owner_account_id: str | None = None

