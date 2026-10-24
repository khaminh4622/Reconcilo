"""Schemas specific to Agent 1 - the Duplicate Invoice Investigator.

The shared agent vocabulary lives in ``workflow.schemas``. This module adds the
investigation-shaped types Agent 1 produces: match candidates, individual
matching signals, and a payment analysis. Everything here is built by
deterministic Python; the LLM never fills these in.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Agent1Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class InvestigationStatus(str, Enum):
    """Outcome of a duplicate investigation."""

    no_duplicate_found = "NO_DUPLICATE_FOUND"
    possible_duplicate = "POSSIBLE_DUPLICATE"
    confirmed_duplicate_invoice = "CONFIRMED_DUPLICATE_INVOICE"
    suspected_duplicate_payment = "SUSPECTED_DUPLICATE_PAYMENT"
    # Bank records prove more than one settlement for the same invoice.
    confirmed_duplicate_payment = "CONFIRMED_DUPLICATE_PAYMENT"
    requires_bank_verification = "REQUIRES_BANK_VERIFICATION"
    requires_human_review = "REQUIRES_HUMAN_REVIEW"
    insufficient_information = "INSUFFICIENT_INFORMATION"


class PreviousPaymentStatus(str, Enum):
    """Payment state of the *original* invoice, which decides what happens next.

    A duplicate submission is not the same as an unpaid one, and it is not the
    same as a paid one either. This is the distinction Agent 1 now makes before
    it decides whether to block a payment.
    """

    already_paid = "ALREADY_PAID"
    unpaid_eligible_for_approval = "UNPAID_ELIGIBLE_FOR_APPROVAL"
    payment_pending = "PAYMENT_PENDING"
    payment_failed = "PAYMENT_FAILED"
    payment_status_unknown = "PAYMENT_STATUS_UNKNOWN"
    duplicate_payment_detected = "DUPLICATE_PAYMENT_DETECTED"


class ObligationState(str, Enum):
    """Lifecycle of a payable obligation."""

    open = "OPEN"            # not yet paid, eligible for approval
    pending = "PENDING"      # a payment attempt is in flight
    paid = "PAID"            # settled and covered in full
    failed = "FAILED"        # last attempt failed, nothing settled
    unknown = "UNKNOWN"      # cannot be determined


class MatchSignal(Agent1Model):
    """One comparison between the invoice under investigation and a prior one.

    ``weight`` is the maximum contribution to the score and ``score`` is what
    was actually awarded, so every point in the total can be explained.
    """

    signal: str
    matched: bool
    weight: int
    score: int
    detail: str


class InvoiceCandidate(Agent1Model):
    """A previously recorded invoice that resembles the one being investigated."""

    record_id: str
    invoice_number: str | None = None
    vendor: str | None = None
    customer: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    issue_date: str | None = None
    description: str | None = None
    match_score: int = 0
    match_band: Literal["exact", "high", "medium", "low"] = "low"
    is_exact_duplicate: bool = False
    signals: list[MatchSignal] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class PaymentRecordView(Agent1Model):
    """One payment record considered during the investigation."""

    source: Literal["payment_attempt", "bank_transaction", "customer_report"]
    reference: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    status: str
    verified: bool = False
    counted_as_settlement: bool = False
    detail: str


class PaymentAnalysis(Agent1Model):
    """Result of Check C - are there multiple payments for this invoice?"""

    invoice_number: str | None = None
    records: list[PaymentRecordView] = Field(default_factory=list)
    successful_settlements: int = 0
    failed_attempts: int = 0
    pending_transactions: int = 0
    customer_reported: int = 0
    verified_settlements: int = 0
    duplicate_payment_suspected: bool = False
    confirmed_duplicate_payment: bool = False
    needs_bank_verification: bool = False
    conclusion: str = ""


class DuplicateInvestigation(Agent1Model):
    """The complete Agent 1 result."""

    status: InvestigationStatus = InvestigationStatus.insufficient_information
    invoice_number: str | None = None
    vendor: str | None = None
    customer: str | None = None
    amount: Decimal | None = None
    currency: str | None = None

    # Check A and B
    candidates: list[InvoiceCandidate] = Field(default_factory=list)
    best_match: InvoiceCandidate | None = None
    highest_score: int = 0
    records_compared: int = 0
    history_available: bool = False

    # Check C
    payment_analysis: PaymentAnalysis = Field(default_factory=PaymentAnalysis)

    # --- Previous invoice payment check (Cases A-F) ------------------------
    previous_payment_status: PreviousPaymentStatus | None = None
    original_invoice: dict | None = None          # the invoice this one duplicates
    original_payment_reference: str | None = None
    obligation_id: str | None = None              # reused payable obligation
    obligation_reused: bool = False               # True when no new obligation was made
    submission_is_duplicate: bool = False         # True when this upload repeats an invoice
    prior_submission_count: int = 0               # how many times this invoice was submitted
    blocks_payment: bool = True
    decision_reason: str = ""

    # Guidance
    needs_agent_2: bool = False
    needs_agent_4: bool = False
    remaining_balance: Decimal | None = None
    needs_human_review: bool = False
    summary: str = ""
    recommended_next_action: str = ""
    evidence_count: int = 0
