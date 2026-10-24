"""Deterministic financial validation for extracted document information.

The LLM reports what the document *says*. This module decides what the numbers
*mean*: it recomputes totals with ``Decimal`` arithmetic, compares expectations
against reported claims, detects overpayments, and classifies payment
provenance. Nothing here trusts the model's own conclusion about consistency.

The central distinction is that a payment the customer mentions is a **claim**.
It becomes ``document_supported`` only when the document carries a payment
artefact, and ``verified`` only against an independent source. Until then the
payment cannot clear a balance, so the case is routed to reconciliation rather
than being reported as settled.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Literal

from .schemas import (
    AnalysisResult,
    CalculatedValues,
    DocumentType,
    DuplicateMatch,
    FinancialStatus,
    PaymentClaim,
    PaymentStatus,
    PrimaryProblem,
    ProblemType,
    RecommendedAgent,
    RoutingDecision,
    ValidationCheck,
    ValidationFinding,
    ValidationResult,
)


REQUIRED_FIELDS: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.invoice: ("document_id", "vendor.vendor_name", "issue_date", "financial_information.total_amount", "currency"),
    DocumentType.receipt: ("vendor.vendor_name", "issue_date", "financial_information.total_amount", "currency"),
    DocumentType.purchase_order: ("document_id", "vendor.vendor_name", "issue_date", "financial_information.total_amount", "currency"),
    DocumentType.contract: ("document_id", "issue_date"),
    DocumentType.refund_request: ("document_id", "customer.customer_name", "financial_information.total_amount", "currency"),
    DocumentType.payment_record: ("transaction_information.transaction_id", "transaction_information.transaction_date", "transaction_information.transaction_amount"),
    DocumentType.bank_statement: ("issue_date", "currency"),
    DocumentType.financial_report: ("issue_date", "currency"),
    DocumentType.other: (),
}

# Fields whose absence makes a financial pass meaningless. A missing invoice
# total must never be reported as "passed".
CORE_FINANCIAL_FIELDS = (
    "financial_information.total_amount",
    "currency",
)

_CURRENCY_PRECISION = {"JPY": 0, "KRW": 0, "VND": 0, "BHD": 3, "JOD": 3, "KWD": 3, "OMR": 3, "TND": 3}

_DATE_PATTERNS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%d/%b/%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%m/%d/%Y",
)


def _get_value(data: object, dotted_path: str) -> object:
    """Read a nested attribute by dotted path."""

    value = data
    for part in dotted_path.split("."):
        value = getattr(value, part, None)
    return value


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    candidate = " ".join(value.replace("\u00a0", " ").split()).strip(" .,;:")
    for abbreviation in ("Jan.", "Feb.", "Mar.", "Apr.", "Jun.", "Jul.", "Aug.", "Sep.",
                         "Sept.", "Oct.", "Nov.", "Dec."):
        if abbreviation in candidate:
            candidate = candidate.replace(abbreviation, abbreviation.rstrip("."))
            break
    for pattern in _DATE_PATTERNS:
        try:
            return datetime.strptime(candidate, pattern).date()
        except ValueError:
            continue
    return None


def _tolerance(currency: str | None) -> Decimal:
    places = _CURRENCY_PRECISION.get((currency or "").upper(), 2)
    return Decimal("0.5").scaleb(-places)


def _different(left: Decimal, right: Decimal, currency: str | None) -> bool:
    return abs(left - right) > _tolerance(currency)


def _money(value: Decimal | None) -> str:
    return f"{value:.2f}" if value is not None else "not available"


def _finding(code: str, field: str | None, message: str, severity: str = "requires_review") -> ValidationFinding:
    return ValidationFinding(code=code, field=field, message=message, severity=severity)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Payment provenance
# ---------------------------------------------------------------------------
# Words that indicate the customer merely *says* they paid.
_REPORTED_CUES = re.compile(
    r"\b(i have paid|i already paid|i paid|we have paid|we already paid|already paid|"
    r"payment (?:was )?(?:sent|made|reported|claimed)|i sent|we sent|has paid|"
    r"my payment|our payment|i transferred|we transferred)\b",
    re.IGNORECASE,
)
# Artefacts that make a payment document-supported rather than merely reported.
_DOCUMENT_SUPPORT_CUES = re.compile(
    r"\b(receipt|remittance advice|payment confirmation|paid stamp|"
    r"transfer confirmation|bank confirmation|proof of payment|payment slip)\b",
    re.IGNORECASE,
)
_VERIFIED_CUES = re.compile(
    r"\b(verified against|matched to bank|confirmed by bank|bank confirmed|"
    r"cleared in bank|settled in bank)\b",
    re.IGNORECASE,
)
_REFERENCE_PATTERN = re.compile(
    r"\b(?:ref(?:erence)?|transfer (?:ref|reference)|txn|transaction id)\b[^A-Za-z0-9]{0,3}"
    r"(?P<ref>[A-Z0-9][A-Z0-9\-/]{3,30})",
    re.IGNORECASE,
)


def assess_payment_claim(result: AnalysisResult, source_text: str) -> PaymentClaim:
    """Classify how strongly the document supports the claimed payment.

    Only an independent source can make a payment ``verified``; this component
    has no access to one, so in practice a payment tops out at
    ``document_supported`` and always requires reconciliation before it may
    clear a balance.
    """

    amount = result.financial_information.amount_paid
    if amount is None or amount == 0:
        return PaymentClaim(
            claimed_amount=amount,
            status=PaymentStatus.none_claimed,
            requires_reconciliation=False,
            note="No payment is claimed in this document.",
        )

    status = PaymentStatus.reported
    note = "The payment is asserted by the document but is not independently confirmed."
    if _VERIFIED_CUES.search(source_text):
        status = PaymentStatus.document_supported
        note = "The document claims independent confirmation; a bank record is still required."
    elif _DOCUMENT_SUPPORT_CUES.search(source_text):
        status = PaymentStatus.document_supported
        note = "A payment artefact (receipt or remittance advice) is referenced in the document."
    elif _REPORTED_CUES.search(source_text):
        status = PaymentStatus.reported
        note = "The customer reports having paid; no payment artefact supports the claim."

    reference = None
    for match in _REFERENCE_PATTERN.finditer(source_text):
        candidate = match.group("ref")
        if result.document_id and candidate.casefold() == result.document_id.casefold():
            continue
        reference = candidate
        break

    return PaymentClaim(
        claimed_amount=amount,
        status=status,
        reference=reference,
        claimed_date=result.transaction_information.transaction_date,
        requires_reconciliation=status != PaymentStatus.verified,
        note=note,
    )


AGENT_NAMES = {
    RecommendedAgent.agent_1: "Agent 1 (Duplicate Invoice Investigator)",
    RecommendedAgent.agent_2: "Agent 2 (Bank Reconciliation Specialist)",
    RecommendedAgent.agent_3: "Agent 3 (Vendor Verification Specialist)",
    RecommendedAgent.agent_4: "Agent 4 (Payment Failure Specialist)",
    RecommendedAgent.agent_5: "Agent 5 (Payment Execution)",
    RecommendedAgent.human_approval: "Human approval required",
    RecommendedAgent.human_review: "Human review / clarification required",
    RecommendedAgent.none: "No further agent required",
}

# The approved routing table. The primary reported problem selects the first
# specialized agent; arithmetic problems never override it.
PRIMARY_AGENT_RULES: dict[PrimaryProblem, RecommendedAgent] = {
    PrimaryProblem.duplicate_invoice: RecommendedAgent.agent_1,
    PrimaryProblem.duplicate_payment: RecommendedAgent.agent_1,
    PrimaryProblem.bank_reconciliation: RecommendedAgent.agent_2,
    PrimaryProblem.payment_failure: RecommendedAgent.agent_4,
    # A payment request must never execute without a human saying yes.
    PrimaryProblem.payment_request: RecommendedAgent.human_approval,
    PrimaryProblem.unknown: RecommendedAgent.human_review,
}

# Language cues used only when the model omits primary_problem, or provides a
# value the enum does not recognise. Order matters: a duplicate payment is the
# most specific and is tested before generic reconciliation wording.
# Language cues used only when the model omits primary_problem, or supplies a
# value the enum does not recognise. Order matters: the most specific and most
# consequential intents are tested first, so a document that mentions both a
# duplicate and a payment status is classified as a duplicate.
PRIMARY_PROBLEM_CUES: tuple[tuple[PrimaryProblem, tuple[str, ...]], ...] = (
    (
        PrimaryProblem.duplicate_payment,
        (
            "paid twice", "paid this invoice twice", "pay this invoice twice",
            "duplicate payment", "double payment", "two payments",
            "two separate payments", "both payments", "both transfers",
            "paid the same invoice twice", "same invoice twice",
            "second payment", "duplicate transfer", "charged twice",
        ),
    ),
    (
        PrimaryProblem.duplicate_invoice,
        (
            "duplicate invoice", "same invoice number", "invoice twice",
            "received this invoice twice", "two invoices", "second invoice",
            "duplicate bill", "billed twice",
        ),
    ),
    (
        PrimaryProblem.payment_failure,
        (
            "payment failed", "transfer failed", "payment did not go through",
            "did not go through", "declined", "rejected", "bounced",
            "failed transfer", "unsuccessful payment",
        ),
    ),
    (
        # A request for GhostLedger to pay. These are deliberately broad: an
        # invoice submitted "for approval" or "for processing" is a payment
        # request, and phrasing varies widely between senders.
        PrimaryProblem.payment_request,
        (
            "please pay", "please process", "process this invoice", "process the invoice",
            "process this bill", "request payment", "payment request",
            "initiate payment", "make a payment", "make payment",
            "send the funds", "remit payment", "remit the", "please transfer",
            "for approval", "for payment", "for your approval", "for your review",
            "awaiting approval", "awaiting payment", "pending approval",
            "submit for payment", "submit this bill", "submit this invoice",
            "please review this", "kindly process", "kindly review",
            "approve this invoice", "approve the invoice", "payment is due",
            "please arrange payment", "arrange payment", "please settle",
            "new invoice", "this is a new invoice", "this invoice is new",
            "not paid yet", "no payment has been", "has not been paid",
            "have not paid", "we have not paid", "payment has not been made",
        ),
    ),
    (
        PrimaryProblem.bank_reconciliation,
        (
            "already paid", "have paid", "payment was made", "money left my account",
            "still shows unpaid", "still says unpaid", "applied to a different account",
            "cannot trace", "not credited", "payment status", "shows an outstanding balance",
            "portal still shows", "reminder saying",
            "payment reported", "payment received", "amount paid", "paid the invoice",
            "paid this invoice", "paid in full",
        ),
    ),
)

# Narrower than the reconciliation cue set: these are phrases where the
# customer is *arguing* that money moved, rather than a neutral status label.
PAYMENT_DISPUTE_CUES = (
    "already paid", "have paid", "has paid", "i paid", "we paid",
    "paid this invoice", "paid the invoice", "paid in full",
    "still shows unpaid", "still says unpaid", "shows as unpaid",
    "applied to a different account", "cannot trace", "not credited",
    "money left my account", "portal still shows", "payment was made",
    "reminder saying", "dispute", "disputed",
)


# Language that says the customer explicitly does NOT want a payment yet. This
# protects purely informational submissions from being classified as payment
# requests, even when the document looks like an invoice.
# Only explicit instructions NOT to pay belong here. Statements that merely
# describe the invoice state ("no payment has been made yet") are the customer
# asking for payment, not refusing it, so they are deliberately excluded.
PAYMENT_REQUEST_SUPPRESSORS = (
    "do not process", "do not pay", "please do not pay", "do not initiate",
    "do not make payment", "do not remit", "not for payment",
    "for recordkeeping", "for record keeping", "for record-keeping",
    "for investigation", "for your information", "for reference only",
    "fyi only", "no payment is due", "no payment due", "nothing to pay",
)


def _has_suppressor(haystack: str) -> bool:
    """True when the text explicitly says no payment should be processed."""

    return any(phrase in haystack for phrase in PAYMENT_REQUEST_SUPPRESSORS)


def is_unpaid_invoice(result: AnalysisResult) -> bool:
    """True when the document is an invoice with money still outstanding.

    Used as corroboration: payment-request wording only wins when there is
    actually an amount to pay.
    """

    financial = result.financial_information
    if result.document_type not in {DocumentType.invoice, DocumentType.receipt}:
        return False
    total = financial.total_amount
    if total is None or total <= 0:
        return False
    paid = financial.amount_paid
    if paid is None:
        # No payment recorded against the invoice: still payable.
        return True
    return paid < total


def infer_primary_problem(
    result: AnalysisResult, source_text: str
) -> tuple[PrimaryProblem, bool]:
    """Return the primary problem and whether it was inferred rather than supplied.

    The model's own answer always wins. Inference only runs when the model
    supplied no classification at all, and it is deterministic so the same
    document always routes the same way.
    """

    supplied = result.detected_problem.primary_problem
    if supplied != PrimaryProblem.unknown:
        return supplied, False

    # An explicit "unknown" from the model is a deliberate answer and is
    # respected: guessing from wording here would silently bypass the human
    # escalation the operator asked for. Inference applies only when the model
    # supplied no classification at all.
    if (result.detected_problem.raw_primary_problem or "").strip():
        return PrimaryProblem.unknown, False

    haystack = " ".join(
        part
        for part in (
            result.detected_problem.description,
            result.detected_problem.source_evidence,
            source_text,
        )
        if part
    ).casefold()

    # A document that says "do not pay this" or "for record only" must never be
    # treated as a payment request, whatever else it contains.
    suppressed = _has_suppressor(haystack)

    # Specific complaints always win over generic dispute wording:
    # a duplicate or a failed payment is more actionable than "we paid".
    for problem in (
        PrimaryProblem.duplicate_payment,
        PrimaryProblem.duplicate_invoice,
        PrimaryProblem.payment_failure,
    ):
        cues = dict(PRIMARY_PROBLEM_CUES)[problem]
        if any(cue in haystack for cue in cues):
            return problem, True

    # A *contested* payment claim ("I already paid", "still shows unpaid")
    # outranks a request to pay. A neutral label such as "Payment Status:
    # Unpaid" is not a dispute and must not suppress a payment request.
    negations = (
        "not paid", "not yet paid", "have not paid", "haven't paid",
        "no payment has been made", "never paid", "unpaid yet",
    )
    negated = any(phrase in haystack for phrase in negations)
    if not negated and any(cue in haystack for cue in PAYMENT_DISPUTE_CUES):
        return PrimaryProblem.bank_reconciliation, True

    for problem, cues in PRIMARY_PROBLEM_CUES:
        if not any(cue in haystack for cue in cues):
            continue
        if problem == PrimaryProblem.payment_request:
            if suppressed:
                # The customer asked for no payment; fall through to review
                # rather than inventing a payment request.
                continue
            # A payment request needs something to pay. An invoice with no
            # outstanding amount is not a request to move money.
            if not is_unpaid_invoice(result):
                continue
        return problem, True

    # --- Fallback on the document-level problem type ----------------------
    problem_type = result.detected_problem.type
    if problem_type == ProblemType.unpaid_invoice and not suppressed and is_unpaid_invoice(result):
        # An unpaid invoice with no counter-evidence is a payment awaiting
        # approval. But if the text argues about money that moved ("already
        # paid", "still shows unpaid"), reconciliation wins: the customer is
        # disputing a payment, not asking for a new one.
        reconciliation_cues = dict(PRIMARY_PROBLEM_CUES)[PrimaryProblem.bank_reconciliation]
        if any(cue in haystack for cue in reconciliation_cues):
            return PrimaryProblem.bank_reconciliation, True
        return PrimaryProblem.payment_request, True
    mapping = {
        ProblemType.payment_status_discrepancy: PrimaryProblem.bank_reconciliation,
        ProblemType.incorrect_transaction: PrimaryProblem.bank_reconciliation,
        ProblemType.refund_request: PrimaryProblem.bank_reconciliation,
    }
    if problem_type in mapping:
        return mapping[problem_type], True
    return PrimaryProblem.unknown, True


def _payment_evidence_count(source_text: str) -> int:
    """Count distinct payment references printed in the document."""

    references = {
        match.group("ref").upper()
        for match in _REFERENCE_PATTERN.finditer(source_text)
        if match.group("ref")
    }
    return len(references)


def detect_duplicate_payments(
    result: AnalysisResult, source_text: str, validation: ValidationResult
) -> tuple[Decimal | None, list[str]]:
    """Detect the same invoice being paid more than once.

    A duplicate is suggested when either the document reports several payment
    references, the model says so, or the reported amount is an exact multiple
    of the invoice total. It is only ever a suspicion: nothing is confirmed
    without bank records.
    """

    notes: list[str] = []
    financial = result.financial_information
    total = financial.total_amount
    paid = financial.amount_paid
    declared = result.detected_problem.reported_payment_count
    reference_count = _payment_evidence_count(source_text)

    mentioned_twice = "paid twice" in source_text.casefold() or "two payments" in source_text.casefold()

    count = max(
        [value for value in (declared, reference_count) if isinstance(value, int)] or [0]
    )
    if mentioned_twice and count < 2:
        count = 2

    duplicate_amount: Decimal | None = None
    if total is not None and paid is not None and total > 0:
        multiple = paid / total
        if multiple >= 2 and paid == total * int(multiple):
            duplicate_amount = total * (int(multiple) - 1)
            notes.append(
                f"the reported payment {_money(paid)} is {int(multiple)}x the invoice total "
                f"{_money(total)}, indicating repeated payment of the same invoice"
            )
    if count >= 2:
        if duplicate_amount is None and total is not None:
            duplicate_amount = total * (count - 1)
        notes.append(f"the document references {count} separate payments for invoice {result.document_id or '(unspecified)'}")
        notes.append("the duplicate is reported by the customer and is not independently confirmed")
    return duplicate_amount, notes


def decide_routing(
    result: AnalysisResult,
    validation: ValidationResult,
    payment_claim: PaymentClaim,
    source_text: str = "",
) -> RoutingDecision:
    """Choose the specialized agent that should continue this case.

    The customer's primary reported problem selects the agent. Overpayment
    amounts and failed arithmetic are *evidence*, not routing keys: they raise
    priority and add a secondary Agent 2 step, but they never displace the
    primary agent. Agent 3 still approves nothing.
    """

    primary, inferred = infer_primary_problem(result, source_text)
    if inferred:
        # Keep the problem record consistent with the routing decision, so the
        # API never reports "unknown" in one place and a routed intent in
        # another.
        result.detected_problem.primary_problem = primary
    duplicate_amount, duplicate_notes = detect_duplicate_payments(result, source_text, validation)
    if duplicate_amount is not None:
        validation.calculated.potential_duplicate_amount = duplicate_amount
        validation.calculated.calculations.append(
            f"potential duplicate payment {_money(duplicate_amount)} reported on invoice "
            f"{result.document_id or '(unspecified)'}"
        )
        validation.validation_findings.append(
            ValidationCheck(
                code="duplicate_payment_reported",
                label="Duplicate payment check",
                status="unverified",
                explanation=(
                    f"Possible duplicate payment of {_money(duplicate_amount)} on invoice "
                    f"{result.document_id or '(unspecified)'}: " + "; ".join(duplicate_notes) + "."
                ),
                reported=_money(duplicate_amount),
            )
        )
        if "duplicate_payment_reported" not in {item.code for item in validation.inconsistencies}:
            validation.inconsistencies.append(
                _finding(
                    "duplicate_payment_reported",
                    "financial_information.amount_paid",
                    "The same invoice appears to have been paid more than once; this must be investigated.",
                )
            )
        # A suspected duplicate always blocks until someone investigates it.
        validation.requires_reconciliation = True
        validation.requires_review = True

    overpayment = validation.calculated.potential_overpayment
    failed = validation.financial_consistency == FinancialStatus.failed
    agent = PRIMARY_AGENT_RULES[primary]

    # --- Compose the user-facing reason -----------------------------------
    if primary == PrimaryProblem.duplicate_payment:
        reason = "The customer reports making two separate payments for the same invoice."
        if result.document_id:
            reason += f" (invoice {result.document_id})"
        if duplicate_amount is not None:
            reason += f" Potential duplicate amount: {_money(duplicate_amount)}."
        reason += " The duplicate is alleged, not confirmed."
    elif primary == PrimaryProblem.duplicate_invoice:
        reason = "The customer reports being billed more than once for the same charges."
        reason += " The duplicate is alleged, not confirmed."
    elif primary == PrimaryProblem.bank_reconciliation:
        pieces = []
        if payment_claim.requires_reconciliation:
            pieces.append(
                f"the reported payment of {_money(payment_claim.claimed_amount)} is "
                f"{payment_claim.status.value.replace('_', ' ')}"
            )
        if overpayment is not None and overpayment > 0:
            pieces.append(f"the reported payment exceeds the invoice total by {_money(overpayment)}")
        if failed:
            pieces.append("the reported figures do not reconcile arithmetically")
        reason = "The customer reports a payment-status or reconciliation problem"
        reason += (" because " + "; ".join(pieces) + ".") if pieces else "."
    elif primary == PrimaryProblem.payment_failure:
        reason = "The customer reports that a payment attempt failed and needs investigation."
    elif primary == PrimaryProblem.payment_request:
        reason = (
            "The document requests that a payment be made. Money must not move without "
            "explicit human approval."
        )
    else:
        reason = (
            "The customer's primary problem could not be classified with confidence, "
            "so a human must clarify it before any agent acts."
        )

    if inferred and primary != PrimaryProblem.unknown:
        reason += " (Agent 3 inferred the primary problem because the model did not classify it.)"

    # --- Secondary investigation -----------------------------------------
    secondary: RecommendedAgent | None = None
    secondary_reason: str | None = None
    if agent == RecommendedAgent.agent_1 and (
        payment_claim.requires_reconciliation or duplicate_amount is not None
    ):
        secondary = RecommendedAgent.agent_2
        secondary_reason = (
            "Agent 2 should verify the transactions against bank records once the "
            "duplicate has been investigated."
        )

    is_duplicate = primary in {PrimaryProblem.duplicate_payment, PrimaryProblem.duplicate_invoice}
    priority: Literal["normal", "high"] = (
        "high"
        if (is_duplicate or overpayment is not None or failed or agent == RecommendedAgent.human_approval)
        else "normal"
    )
    blocking = bool(
        is_duplicate
        or validation.requires_reconciliation
        or failed
        or agent in {RecommendedAgent.human_approval, RecommendedAgent.human_review}
    )

    return RoutingDecision(
        recommended_agent=agent,
        agent_name=AGENT_NAMES[agent],
        reason=reason,
        priority=priority,
        blocking=blocking,
        primary_problem=primary,
        secondary_agent=secondary,
        secondary_reason=secondary_reason,
        overridden=inferred,
        requires_human_approval=agent == RecommendedAgent.human_approval,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def validate_document(
    result: AnalysisResult,
    duplicate_matches: Iterable[DuplicateMatch] = (),
    extraction_failed: bool = False,
    customer_verification: str | None = None,
    unverified_information: Iterable[str] = (),
    source_text: str = "",
) -> ValidationResult:
    """Validate only stated values; absence never becomes a guessed value."""

    if extraction_failed:
        failed_check = ValidationCheck(
            code="extraction_failed",
            label="Document extraction",
            status="failed",
            explanation="Document extraction could not be completed. Manual review is required.",
        )
        return ValidationResult(
            status="extraction_failed",
            requires_review=True,
            financial_consistency=FinancialStatus.not_available,
            customer_verification="not_available",
            inconsistencies=[
                _finding("extraction_failed", None, failed_check.explanation)
            ],
            validation_findings=[failed_check],
        )

    missing = [
        field
        for field in REQUIRED_FIELDS[result.document_type]
        if _get_value(result, field) is None or _get_value(result, field) == ""
    ]
    missing_core = [
        field for field in CORE_FINANCIAL_FIELDS
        if _get_value(result, field) is None or _get_value(result, field) == ""
    ]
    # Only documents that are supposed to carry financial terms can fail for
    # lacking them. A service report with no money on it is simply not
    # available for financial checks.
    expects_financial_detail = result.document_type not in {DocumentType.other, DocumentType.contract}
    if not expects_financial_detail:
        missing_core = []
    findings: list[ValidationFinding] = []
    checks: list[ValidationCheck] = []
    calculated = CalculatedValues()
    financial = result.financial_information
    currency = result.currency or financial.currency

    if result.document_type == DocumentType.other:
        findings.append(
            _finding(
                "unclassified_document",
                "document_type",
                "The document could not be classified reliably and was labeled other.",
                "warning",
            )
        )

    # --- Dates --------------------------------------------------------------
    if result.issue_date and not _parse_date(result.issue_date):
        findings.append(_finding("invalid_issue_date", "issue_date", "Issue date is not a recognized calendar date."))
        checks.append(
            ValidationCheck(
                code="invalid_issue_date",
                label="Issue date",
                status="failed",
                explanation="The issue date is not a recognized calendar date.",
                reported=result.issue_date,
            )
        )
    if result.due_date and not _parse_date(result.due_date):
        findings.append(_finding("invalid_due_date", "due_date", "Due date is not a recognized calendar date."))
        checks.append(
            ValidationCheck(
                code="invalid_due_date",
                label="Due date",
                status="failed",
                explanation="The due date is not a recognized calendar date.",
                reported=result.due_date,
            )
        )
    if result.transaction_information.transaction_date and not _parse_date(result.transaction_information.transaction_date):
        findings.append(
            _finding(
                "invalid_transaction_date",
                "transaction_information.transaction_date",
                "Transaction date is not a recognized calendar date.",
            )
        )

    issue_date = _parse_date(result.issue_date)
    due_date = _parse_date(result.due_date)
    if issue_date and due_date and due_date < issue_date:
        findings.append(
            _finding("due_before_issue", "due_date", "Due date is earlier than issue date; review the document dates.")
        )
        checks.append(
            ValidationCheck(
                code="due_before_issue",
                label="Date ordering",
                status="failed",
                explanation=f"The due date ({result.due_date}) is earlier than the issue date ({result.issue_date}).",
                reported=result.due_date,
            )
        )
    elif issue_date and due_date:
        checks.append(
            ValidationCheck(
                code="date_ordering",
                label="Date ordering",
                status="passed",
                explanation="The due date is after the issue date.",
            )
        )

    # --- 1. Subtotal + Tax = Total -----------------------------------------
    subtotal, tax, discount, total = (
        financial.subtotal, financial.tax_amount, financial.discount, financial.total_amount
    )
    if subtotal is not None and total is not None:
        discount_value = discount if discount is not None else Decimal("0")
        tax_value = tax if tax is not None else Decimal("0")
        expected_total = subtotal - discount_value + tax_value
        calculated.expected_total = expected_total
        parts = [f"subtotal {_money(subtotal)}"]
        if discount_value:
            parts.append(f"minus discount {_money(discount_value)}")
        if tax_value:
            parts.append(f"plus tax {_money(tax_value)}")
        calculated.calculations.append(
            f"{' '.join(parts)} = {_money(expected_total)}; document states total {_money(total)}"
        )
        if _different(expected_total, total, currency):
            variance = total - expected_total
            findings.append(
                _finding(
                    "financial_total_mismatch",
                    "financial_information.total_amount",
                    "Subtotal minus discount plus tax does not equal the stated total amount.",
                )
            )
            checks.append(
                ValidationCheck(
                    code="subtotal_tax_total",
                    label="Subtotal + tax = invoice total",
                    status="failed",
                    explanation=(
                        f"Subtotal {_money(subtotal)} plus tax {_money(tax_value)}"
                        f"{f' minus discount {_money(discount_value)}' if discount_value else ''} "
                        f"equals {_money(expected_total)}, but the invoice states {_money(total)}. "
                        f"Difference: {_money(variance)}."
                    ),
                    expected=_money(expected_total),
                    reported=_money(total),
                    variance=_money(variance),
                )
            )
        else:
            checks.append(
                ValidationCheck(
                    code="subtotal_tax_total",
                    label="Subtotal + tax = invoice total",
                    status="passed",
                    explanation=(
                        f"Subtotal {_money(subtotal)}"
                        f"{f' minus discount {_money(discount_value)}' if discount_value else ''}"
                        f" plus tax {_money(tax_value)} equals the stated total {_money(total)}."
                    ),
                    expected=_money(expected_total),
                    reported=_money(total),
                    variance="0.00",
                )
            )
    elif total is None:
        checks.append(
            ValidationCheck(
                code="subtotal_tax_total",
                label="Subtotal + tax = invoice total",
                status="not_applicable",
                explanation="The invoice total could not be extracted, so this check could not run.",
            )
        )

    # --- 2/3. Payments, expected balance, overpayment ----------------------
    paid = financial.amount_paid
    reported_balance = financial.outstanding_balance
    payment_claim = assess_payment_claim(result, source_text)

    if total is not None and paid is not None:
        expected_balance = total - paid
        calculated.expected_outstanding_balance = expected_balance
        calculated.calculations.append(
            f"invoice total {_money(total)} minus reported payment {_money(paid)} = {_money(expected_balance)}"
        )

        if expected_balance < 0:
            overpayment = -expected_balance
            calculated.potential_overpayment = overpayment
            findings.append(
                _finding(
                    "potential_overpayment",
                    "financial_information.amount_paid",
                    "The reported payment exceeds the invoice total, indicating a possible overpayment.",
                )
            )
            checks.append(
                ValidationCheck(
                    code="overpayment_detected",
                    label="Overpayment check",
                    status="failed",
                    explanation=(
                        f"The reported payment {_money(paid)} exceeds the invoice total {_money(total)} "
                        f"by {_money(overpayment)}. This payment cannot be confirmed by Agent 3 and must "
                        "be reconciled against bank records before any refund or credit."
                    ),
                    expected=_money(total),
                    reported=_money(paid),
                    variance=_money(overpayment),
                )
            )

        if reported_balance is not None:
            variance = reported_balance - expected_balance
            calculated.variance_from_reported = variance
            if _different(expected_balance, reported_balance, currency):
                findings.append(
                    _finding(
                        "outstanding_balance_mismatch",
                        "financial_information.outstanding_balance",
                        "Invoice total minus the reported payment does not equal the stated outstanding balance.",
                    )
                )
                checks.append(
                    ValidationCheck(
                        code="outstanding_balance_consistent",
                        label="Total - payments = outstanding balance",
                        status="failed",
                        explanation=(
                            f"Invoice total {_money(total)} minus the reported payment {_money(paid)} "
                            f"equals {_money(expected_balance)}, but the document states {_money(reported_balance)}. "
                            f"Difference: {_money(variance)}."
                        ),
                        expected=_money(expected_balance),
                        reported=_money(reported_balance),
                        variance=_money(variance),
                    )
                )
            else:
                checks.append(
                    ValidationCheck(
                        code="outstanding_balance_consistent",
                        label="Total - payments = outstanding balance",
                        status="passed",
                        explanation=(
                            f"Invoice total {_money(total)} minus the reported payment {_money(paid)} "
                            f"equals the stated balance {_money(reported_balance)}."
                        ),
                        expected=_money(expected_balance),
                        reported=_money(reported_balance),
                        variance="0.00",
                    )
                )

    # --- Payment provenance / reconciliation -------------------------------
    if paid is not None and paid > 0:
        if payment_claim.status == PaymentStatus.verified:
            checks.append(
                ValidationCheck(
                    code="payment_verified",
                    label="Payment verification",
                    status="passed",
                    explanation="The payment has been confirmed against an independent source.",
                )
            )
        else:
            findings.append(
                _finding(
                    "payment_not_verified",
                    "financial_information.amount_paid",
                    "The reported payment is not independently verified and requires reconciliation.",
                )
            )
            checks.append(
                ValidationCheck(
                    code="payment_verification",
                    label="Payment verification",
                    status="unverified",
                    explanation=(
                        f"The payment of {_money(paid)} is {payment_claim.status.value.replace('_', ' ')}. "
                        f"{payment_claim.note or ''} It is treated as a customer claim, not as a "
                        "confirmed bank transaction, and must be reconciled.".strip()
                    ),
                    reported=_money(paid),
                )
            )

    # --- Line items --------------------------------------------------------
    for index, item in enumerate(result.line_items):
        if item.quantity is not None and item.unit_price is not None and item.total_price is not None:
            expected_line_total = item.quantity * item.unit_price
            if _different(expected_line_total, item.total_price, currency):
                findings.append(
                    _finding(
                        "line_item_mismatch",
                        f"line_items[{index}].total_price",
                        f"Line item {index + 1} quantity times unit price does not equal its total price.",
                    )
                )

    # --- Missing information ----------------------------------------------
    if missing_core:
        checks.append(
            ValidationCheck(
                code="required_financial_fields",
                label="Required financial fields",
                status="failed",
                explanation=(
                    "These required financial fields are missing, so financial consistency "
                    f"cannot be confirmed: {', '.join(missing_core)}."
                ),
            )
        )

    matches = list(duplicate_matches)
    if matches:
        findings.append(
            _finding(
                "possible_duplicate",
                "document_id",
                "A matching file or matching document metadata was previously processed.",
            )
        )

    # --- Overall determination --------------------------------------------
    failed_checks = [check for check in checks if check.status == "failed"]
    # Payment provenance is a separate axis from arithmetic consistency: an
    # unconfirmed payment does not make the invoice's own numbers wrong.
    arithmetic_failures = [
        check
        for check in failed_checks
        if check.code in {"subtotal_tax_total", "outstanding_balance_consistent", "overpayment_detected"}
    ]
    has_financial_data = any(
        value is not None
        for value in (subtotal, tax, discount, total, paid, reported_balance)
    )

    if missing_core:
        # A missing total or currency can never be a financial pass, even when
        # there is no arithmetic to run at all.
        financial_consistency = FinancialStatus.failed
    elif failed_checks:
        financial_consistency = FinancialStatus.failed
    elif not has_financial_data:
        financial_consistency = FinancialStatus.not_available
    elif paid is not None and paid > 0 and payment_claim.status != PaymentStatus.verified:
        # The arithmetic reconciles, but it rests on an unconfirmed payment.
        financial_consistency = FinancialStatus.unverified
    else:
        financial_consistency = FinancialStatus.passed

    if customer_verification is None:
        customer_verification = "unverified" if any(
            (result.customer.customer_name, result.customer.customer_id, result.customer.email)
        ) else "not_available"

    collected_unverified = list(unverified_information)
    if customer_verification != "matched" and result.customer.customer_name and "customer identity" not in collected_unverified:
        collected_unverified.append("customer identity")
    if paid is not None and paid > 0 and payment_claim.status != PaymentStatus.verified:
        label = "payment confirmation"
        if label not in collected_unverified:
            collected_unverified.append(label)

    requires_reconciliation = bool(
        payment_claim.requires_reconciliation
        or calculated.potential_overpayment is not None
        or financial_consistency == FinancialStatus.failed
    )
    needs_review = bool(
        missing
        or missing_core
        or matches
        or financial_consistency in {FinancialStatus.failed, FinancialStatus.unverified}
        or any(finding.severity == "requires_review" for finding in findings)
    )
    status = "requires_review" if needs_review else ("warning" if findings else "passed")

    validation = ValidationResult(
        status=status,
        missing_fields=missing,
        inconsistencies=findings,
        possible_duplicate=bool(matches),
        duplicate_matches=matches,
        requires_review=needs_review,
        financial_consistency=financial_consistency,
        customer_verification=customer_verification,  # type: ignore[arg-type]
        unverified_information=collected_unverified,
        validation_findings=checks,
        calculated=calculated,
        payment_provenance=payment_claim.status,
        requires_reconciliation=requires_reconciliation,
    )
    result.financial_information.payment_claim = payment_claim
    return validation


def build_reasoning(
    result: AnalysisResult,
    validation: ValidationResult,
    routing: RoutingDecision,
) -> list[str]:
    """Explain, in plain language, how Agent 3 reached its conclusion."""

    financial = result.financial_information
    steps: list[str] = []
    steps.append(
        f"Read {result.source.page_count or 'an unknown number of'} page(s) and classified the "
        f"document as {result.document_type.value.replace('_', ' ')}."
    )
    if financial.total_amount is not None:
        steps.append(f"The invoice total is {_money(financial.total_amount)} {result.currency or ''}".strip() + ".")
    else:
        steps.append("No invoice total could be extracted from the document.")

    if financial.amount_paid is not None:
        steps.append(
            f"The document reports a payment of {_money(financial.amount_paid)}; this is treated as a "
            f"{validation.payment_provenance.value.replace('_', ' ')} claim, not a confirmed transaction."
        )
    if financial.outstanding_balance is not None:
        steps.append(f"The document states an outstanding balance of {_money(financial.outstanding_balance)}.")

    if validation.calculated.calculations:
        steps.append("Deterministic calculation: " + "; ".join(validation.calculated.calculations) + ".")

    overpayment = validation.calculated.potential_overpayment
    if overpayment is not None:
        steps.append(
            f"The reported payment exceeds the invoice total by {_money(overpayment)}. "
            "This is a potential overpayment that Agent 3 cannot confirm on its own."
        )
    if validation.calculated.variance_from_reported is not None:
        variance = validation.calculated.variance_from_reported
        if variance:
            steps.append(
                f"The calculated balance differs from the reported balance by {_money(variance)}."
            )

    steps.append(f"Financial consistency was determined in Python as {validation.financial_consistency.value.upper()}.")
    steps.append(
        f"Customer verification is {validation.customer_verification.replace('_', ' ').upper()}."
    )
    if validation.missing_fields:
        steps.append("Missing required information: " + ", ".join(validation.missing_fields) + ".")
    if routing.reason:
        steps.append(f"Routing: {routing.reason}")
    steps.append("Agent 3 does not approve payments or execute transfers; a human must confirm the outcome.")
    return steps
