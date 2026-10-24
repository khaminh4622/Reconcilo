"""Pydantic models for Agent 3 PDF extraction and deterministic validation."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DocumentType(str, Enum):
    invoice = "invoice"
    receipt = "receipt"
    purchase_order = "purchase_order"
    contract = "contract"
    refund_request = "refund_request"
    payment_record = "payment_record"
    bank_statement = "bank_statement"
    financial_report = "financial_report"
    other = "other"


class AnalysisSource(str, Enum):
    live_llm = "live_llm"
    synthetic_fixture = "synthetic_fixture"
    unavailable = "unavailable"


class ProblemType(str, Enum):
    payment_status_discrepancy = "payment_status_discrepancy"
    unpaid_invoice = "unpaid_invoice"
    refund_request = "refund_request"
    incorrect_transaction = "incorrect_transaction"
    financial_document_question = "financial_document_question"
    other_financial_issue = "other_financial_issue"
    none_identified = "none_identified"


class ProblemStatus(str, Enum):
    reported = "reported"
    requires_clarification = "requires_clarification"
    unavailable = "unavailable"


class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PaymentStatus(str, Enum):
    """Provenance of a claimed payment. Only ``verified`` is confirmed money."""

    # Stated by the customer or a note in the document; not evidenced.
    reported = "reported"
    # Evidenced by a document artefact (receipt, remittance advice, transfer ref).
    document_supported = "document_supported"
    # Confirmed against an independent source such as a bank record.
    verified = "verified"
    # No payment is claimed at all.
    none_claimed = "none_claimed"


class FinancialStatus(str, Enum):
    """Deterministic outcome of the arithmetic checks.

    ``unverified`` means the payment claim cannot be confirmed yet; it is never
    a pass, because an unconfirmed payment must go to reconciliation.
    """

    passed = "passed"
    failed = "failed"
    unverified = "unverified"
    requires_review = "requires_review"
    not_available = "not_available"


class PaymentClaim(APIModel):
    """A payment the document mentions, with how strongly it is supported."""

    claimed_amount: Decimal | None = None
    status: PaymentStatus = PaymentStatus.none_claimed
    reference: str | None = None
    claimed_date: str | None = None
    requires_reconciliation: bool = True
    note: str | None = None


class PrimaryProblem(str, Enum):
    """The customer's primary reported problem, which drives first routing.

    Deliberately distinct from ``ProblemType``: ProblemType describes what the
    document shows, while PrimaryProblem describes what the customer is asking
    someone to resolve. Routing follows the latter.
    """

    duplicate_invoice = "duplicate_invoice"
    duplicate_payment = "duplicate_payment"
    bank_reconciliation = "bank_reconciliation"
    payment_failure = "payment_failure"
    payment_request = "payment_request"
    # A claimed payment the ledger still shows as unpaid. Distinct from a
    # payment_request, which asks GhostLedger to make a new payment.
    payment_status_dispute = "payment_status_dispute"
    unknown = "unknown"


class CalculatedValues(APIModel):
    """Figures Agent 3 computed itself. Never taken from the model."""

    expected_total: Decimal | None = None
    expected_outstanding_balance: Decimal | None = None
    variance_from_reported: Decimal | None = None
    potential_overpayment: Decimal | None = None
    # Amount at risk when the same invoice appears paid more than once.
    potential_duplicate_amount: Decimal | None = None
    calculations: list[str] = Field(default_factory=list)


class DocumentInformation(APIModel):
    document_type: DocumentType = DocumentType.other
    document_number: str | None = None
    issue_date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    payment_terms: str | None = None


class RecommendedAgent(str, Enum):
    agent_1 = "agent_1"
    agent_2 = "agent_2"
    agent_3 = "agent_3"
    agent_4 = "agent_4"
    agent_5 = "agent_5"
    human_approval = "human_approval"
    human_review = "human_review"
    none = "none"


class RoutingDecision(APIModel):
    """Which specialized agent should continue, and why."""

    recommended_agent: RecommendedAgent = RecommendedAgent.none
    agent_name: str | None = None
    reason: str | None = None
    priority: Literal["normal", "high"] = "normal"
    blocking: bool = False
    # The customer's primary reported problem, as classified then validated.
    primary_problem: PrimaryProblem = PrimaryProblem.unknown
    # A follow-up step Agent 2 must take after the primary agent, if any.
    secondary_agent: RecommendedAgent | None = None
    secondary_reason: str | None = None
    # True when the LLM's classification was overridden by deterministic checks.
    overridden: bool = False
    # True when a human must approve before any money moves.
    requires_human_approval: bool = False


class CustomerInformation(APIModel):
    customer_name: str | None = None
    customer_id: str | None = None
    company_name: str | None = None
    email: str | None = None
    address: str | None = None


class VendorInformation(APIModel):
    vendor_name: str | None = None
    vendor_id: str | None = None
    vendor_address: str | None = None
    vendor_email: str | None = None


class FinancialInformation(APIModel):
    currency: str | None = None
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    discount: Decimal | None = None
    total_amount: Decimal | None = None
    # ``amount_paid`` is the payment the document *reports*. It is a claim, not
    # a confirmed transaction; ``payment_claim`` records how strongly it is
    # supported and whether reconciliation is still required.
    amount_paid: Decimal | None = None
    outstanding_balance: Decimal | None = None
    payment_status: str | None = None
    payment_claim: PaymentClaim = Field(default_factory=PaymentClaim)


class TransactionInformation(APIModel):
    transaction_id: str | None = None
    transaction_date: str | None = None
    transaction_amount: Decimal | None = None
    sender: str | None = None
    receiver: str | None = None
    reference_number: str | None = None


class LineItem(APIModel):
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    total_price: Decimal | None = None


class FieldEvidence(APIModel):
    field: str
    value: str | None = None
    source_text: str | None = None
    page: int | None = None
    available: bool = False


class DetectedProblem(APIModel):
    """A document-reported issue, never a confirmed financial finding."""

    type: ProblemType = ProblemType.none_identified
    description: str | None = None
    source_evidence: str | None = None
    page: int | None = None
    status: ProblemStatus = ProblemStatus.requires_clarification
    # The customer's primary ask, which determines first routing. Defaults to
    # ``unknown`` so an absent classification is escalated, never guessed.
    primary_problem: PrimaryProblem = PrimaryProblem.unknown
    # Exactly what the model returned, so a deliberate "unknown" can be told
    # apart from no answer at all.
    raw_primary_problem: str | None = None
    # How many separate payments the document reports for this invoice.
    reported_payment_count: int | None = None


class ValidationFinding(APIModel):
    code: str
    field: str | None = None
    message: str
    severity: Literal["warning", "requires_review"] = "warning"


class DuplicateMatch(APIModel):
    record_id: str
    reason: Literal["file_hash", "metadata"]


class ValidationCheck(APIModel):
    """One deterministic check with an explicit, displayable outcome."""

    code: str
    label: str
    status: Literal["passed", "failed", "unverified", "not_applicable"]
    explanation: str
    expected: str | None = None
    reported: str | None = None
    variance: str | None = None
    page: int | None = None
    evidence: str | None = None


class ValidationResult(APIModel):
    status: Literal["passed", "warning", "requires_review", "extraction_failed"]
    missing_fields: list[str] = Field(default_factory=list)
    inconsistencies: list[ValidationFinding] = Field(default_factory=list)
    possible_duplicate: bool = False
    duplicate_matches: list[DuplicateMatch] = Field(default_factory=list)
    requires_review: bool = False
    financial_consistency: FinancialStatus = FinancialStatus.not_available
    customer_verification: Literal["matched", "unverified", "mismatch", "not_available"] = "not_available"
    unverified_information: list[str] = Field(default_factory=list)
    # Structured, UI-friendly findings, each carrying its own status.
    validation_findings: list[ValidationCheck] = Field(default_factory=list)
    calculated: CalculatedValues = Field(default_factory=CalculatedValues)
    payment_provenance: PaymentStatus = PaymentStatus.none_claimed
    requires_reconciliation: bool = False


class SourceMetadata(APIModel):
    filename: str | None = None
    media_type: str | None = None
    page_count: int | None = None
    file_hash: str | None = None


class LLMCallMetadata(APIModel):
    """Audit trail proving whether a real model request produced this result."""

    invoked: bool = False
    provider_response_id: str | None = None
    model: str | None = None
    usage: dict[str, int] | None = None


class ExtractionPayload(APIModel):
    """Facts requested from the model before Agent 3 validation is applied."""

    document_type: DocumentType = DocumentType.other
    document_id: str | None = None
    issue_date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    payment_terms: str | None = None
    customer: CustomerInformation = Field(default_factory=CustomerInformation)
    vendor: VendorInformation = Field(default_factory=VendorInformation)
    financial_information: FinancialInformation = Field(default_factory=FinancialInformation)
    transaction_information: TransactionInformation = Field(default_factory=TransactionInformation)
    line_items: list[LineItem] = Field(default_factory=list)
    evidence: list[FieldEvidence] = Field(default_factory=list)
    detected_problem: DetectedProblem = Field(default_factory=DetectedProblem)
    missing_information: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    information_requiring_verification: list[str] = Field(default_factory=list)


class AnalysisResult(APIModel):
    """Grounded LLM extraction plus independent validation for one customer PDF."""

    record_id: str | None = None
    document_type: DocumentType = DocumentType.other
    document_id: str | None = None
    issue_date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    payment_terms: str | None = None
    customer: CustomerInformation = Field(default_factory=CustomerInformation)
    # Kept as a clear, task-facing name in the public JSON contract.
    customer_information: CustomerInformation = Field(default_factory=CustomerInformation)
    vendor: VendorInformation = Field(default_factory=VendorInformation)
    financial_information: FinancialInformation = Field(default_factory=FinancialInformation)
    transaction_information: TransactionInformation = Field(default_factory=TransactionInformation)
    line_items: list[LineItem] = Field(default_factory=list)
    evidence: list[FieldEvidence] = Field(default_factory=list)
    detected_problem: DetectedProblem = Field(default_factory=DetectedProblem)
    missing_information: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    information_requiring_verification: list[str] = Field(default_factory=list)
    validation: ValidationResult = Field(
        default_factory=lambda: ValidationResult(status="requires_review", requires_review=True)
    )
    # Task-facing report sections.
    document_information: DocumentInformation = Field(default_factory=DocumentInformation)
    vendor_information: VendorInformation = Field(default_factory=VendorInformation)
    validation_findings: list[ValidationCheck] = Field(default_factory=list)
    routing: RoutingDecision = Field(default_factory=RoutingDecision)
    reasoning: list[str] = Field(default_factory=list)
    processing_errors: list[str] = Field(default_factory=list)
    source: SourceMetadata = Field(default_factory=SourceMetadata)
    llm_call: LLMCallMetadata = Field(default_factory=LLMCallMetadata)
    analysis_source: AnalysisSource = AnalysisSource.unavailable
    requires_review: bool = True


class CustomerType(str, Enum):
    individual = "individual"
    business = "business"


class CustomerProfile(APIModel):
    """A pre-existing fictional reference record, used only for matching."""

    id: str
    customer_id: str
    customer_name: str
    customer_type: CustomerType
    company_name: str | None = None
    email: str
    phone: str | None = None
    address: str | None = None
    onboarding_status: str
    verification_status: str
    created_at: str | None = None
    updated_at: str | None = None
