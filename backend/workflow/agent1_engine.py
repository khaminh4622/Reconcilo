"""Agent 1 duplicate detection engine.

Deterministic Python only: no LLM participates in matching, scoring or any
financial comparison. The LLM may later explain the result, but it cannot
change it.

Three checks:
  A. Exact duplicate      - same normalised invoice number, same vendor, same amount
  B. Possible duplicate   - different invoice numbers, weighted similarity score
  C. Duplicate payment    - multiple payment records against one invoice
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from .agent1_history import InvoiceHistoryRecord, normalize_name
from .agent1_schemas import (
    Agent1Model,
    DuplicateInvestigation,
    InvestigationStatus,
    InvoiceCandidate,
    MatchSignal,
    PaymentAnalysis,
    PaymentRecordView,
)

# --- Check B weighting ------------------------------------------------------
# Total is 100. The bands below decide how strong a score must be to matter.
W_VENDOR = 25
W_CUSTOMER = 10
W_AMOUNT = 30
W_CURRENCY = 10
W_DATE = 15
W_DESCRIPTION = 10
SCORE_TOTAL = W_VENDOR + W_CUSTOMER + W_AMOUNT + W_CURRENCY + W_DATE + W_DESCRIPTION

BAND_EXACT = 100
BAND_HIGH = 75
BAND_MEDIUM = 55

# Two invoices for the same vendor and amount on the same day are suspicious.
# Recurring monthly invoices differ by date, which is why the date signal exists.
RECURRING_MIN_DAYS = 25
RECURRING_MAX_DAYS = 35


def _money(value: Decimal | None, currency: str | None = None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f} {currency or ''}".strip()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    candidate = " ".join(value.replace("\u00a0", " ").split()).strip(" .,;:")
    for pattern in (
        "%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%m/%d/%Y",
        "%b %d, %Y", "%B %d, %Y", "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(candidate, pattern).date()
        except ValueError:
            continue
    return None


def _description_tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    stop = {"the", "and", "for", "with", "services", "service", "monthly", "fee", "fees"}
    return {token for token in re.findall(r"[a-z]{3,}", text.casefold()) if token not in stop}


def _date_days_apart(left: str | None, right: str | None) -> int | None:
    a, b = _parse_date(left), _parse_date(right)
    if a is None or b is None:
        return None
    return abs((a - b).days)


def score_candidate(
    *,
    vendor: str | None,
    customer: str | None,
    amount: Decimal | None,
    currency: str | None,
    issue_date: str | None,
    description: str | None,
    prior: InvoiceHistoryRecord,
) -> InvoiceCandidate:
    """Score one prior invoice against the invoice under investigation.

    Every signal contributes a bounded weight, and each is recorded so the total
    can be explained line by line.
    """

    signals: list[MatchSignal] = []
    evidence: list[str] = []

    # Vendor ---------------------------------------------------------------
    vendor_key = normalize_name(vendor)
    vendor_match = bool(vendor_key) and vendor_key == prior.normalized_vendor
    signals.append(
        MatchSignal(
            signal="vendor",
            matched=vendor_match,
            weight=W_VENDOR,
            score=W_VENDOR if vendor_match else 0,
            detail=(
                f"Same vendor: {vendor}."
                if vendor_match
                else f"Different vendor ({vendor or 'none'} vs {prior.vendor or 'none'})."
            ),
        )
    )
    if vendor_match:
        evidence.append(f"Vendor matches: {prior.vendor}")

    # Customer / tenant ----------------------------------------------------
    customer_key = normalize_name(customer)
    customer_match = bool(customer_key) and customer_key == prior.normalized_customer
    signals.append(
        MatchSignal(
            signal="customer",
            matched=customer_match,
            weight=W_CUSTOMER,
            score=W_CUSTOMER if customer_match else 0,
            detail=(
                "Same customer account."
                if customer_match
                else f"Different customer ({customer or 'none'} vs {prior.customer or 'none'})."
            ),
        )
    )

    # Amount ---------------------------------------------------------------
    if amount is not None and prior.amount is not None:
        amount_match = abs(amount - prior.amount) <= Decimal("0.005")
        signals.append(
            MatchSignal(
                signal="amount",
                matched=amount_match,
                weight=W_AMOUNT,
                score=W_AMOUNT if amount_match else 0,
                detail=(
                    f"Amount matches exactly: {_money(prior.amount, prior.currency)}."
                    if amount_match
                    else f"Different amount ({_money(amount, currency)} vs {_money(prior.amount, prior.currency)})."
                ),
            )
        )
        if amount_match:
            evidence.append(f"Amount matches: {_money(prior.amount, prior.currency)}")
    else:
        signals.append(
            MatchSignal(
                signal="amount",
                matched=False,
                weight=W_AMOUNT,
                score=0,
                detail="Amount unavailable on one side, so it could not be compared.",
            )
        )

    # Currency -------------------------------------------------------------
    if currency and prior.currency:
        currency_match = currency.strip().upper() == prior.currency.strip().upper()
        signals.append(
            MatchSignal(
                signal="currency",
                matched=currency_match,
                weight=W_CURRENCY,
                score=W_CURRENCY if currency_match else 0,
                detail=(
                    f"Same currency: {currency.upper()}."
                    if currency_match
                    else f"Different currency ({currency} vs {prior.currency})."
                ),
            )
        )
    else:
        signals.append(
            MatchSignal(
                signal="currency",
                matched=False,
                weight=W_CURRENCY,
                score=0,
                detail="Currency unavailable on one side, so it could not be compared.",
            )
        )

    # Invoice date ---------------------------------------------------------
    days = _date_days_apart(issue_date, prior.issue_date)
    if days is None:
        signals.append(
            MatchSignal(
                signal="invoice_date",
                matched=False,
                weight=W_DATE,
                score=0,
                detail="Invoice date unavailable on one side, so it could not be compared.",
            )
        )
    elif days == 0:
        signals.append(
            MatchSignal(
                signal="invoice_date",
                matched=True,
                weight=W_DATE,
                score=W_DATE,
                detail=f"Same invoice date: {prior.issue_date}.",
            )
        )
        evidence.append(f"Same invoice date: {prior.issue_date}")
    elif days <= 3:
        awarded = int(W_DATE * 0.6)
        signals.append(
            MatchSignal(
                signal="invoice_date",
                matched=True,
                weight=W_DATE,
                score=awarded,
                detail=f"Invoice dates are {days} day(s) apart.",
            )
        )
    elif days <= 31:
        # Within a month: could be a duplicate issued a few weeks late, but also
        # exactly what a legitimate recurring invoice looks like.
        signals.append(
            MatchSignal(
                signal="invoice_date",
                matched=False,
                weight=W_DATE,
                score=0,
                detail=(
                    f"Invoice dates are {days} days apart, consistent with a recurring billing "
                    "cycle rather than a duplicate."
                ),
            )
        )
    else:
        signals.append(
            MatchSignal(
                signal="invoice_date",
                matched=False,
                weight=W_DATE,
                score=0,
                detail=f"Invoice dates are {days} days apart.",
            )
        )

    # Service description --------------------------------------------------
    left, right = _description_tokens(description), _description_tokens(prior.description)
    if left and right:
        overlap = left & right
        ratio = len(overlap) / max(len(left | right), 1)
        awarded = int(W_DESCRIPTION * ratio)
        signals.append(
            MatchSignal(
                signal="description",
                matched=awarded > 0,
                weight=W_DESCRIPTION,
                score=awarded,
                detail=(
                    f"Service descriptions overlap on: {', '.join(sorted(overlap))}."
                    if overlap
                    else "Service descriptions do not overlap."
                ),
            )
        )
        if overlap:
            evidence.append(f"Similar service description: {', '.join(sorted(overlap)[:4])}")
    else:
        signals.append(
            MatchSignal(
                signal="description",
                matched=False,
                weight=W_DESCRIPTION,
                score=0,
                detail="Description unavailable on one side, so it could not be compared.",
            )
        )

    total = sum(signal.score for signal in signals)
    band: str = "low"
    if total >= BAND_EXACT:
        band = "exact"
    elif total >= BAND_HIGH:
        band = "high"
    elif total >= BAND_MEDIUM:
        band = "medium"

    return InvoiceCandidate(
        record_id=prior.record_id,
        invoice_number=prior.invoice_number,
        vendor=prior.vendor,
        customer=prior.customer,
        amount=prior.amount,
        currency=prior.currency,
        issue_date=prior.issue_date,
        description=prior.description,
        match_score=total,
        match_band=band,  # type: ignore[arg-type]
        is_exact_duplicate=False,
        signals=signals,
        evidence=evidence,
    )


def is_recurring_pattern(candidate: InvoiceCandidate) -> bool:
    """True when the pairing looks like normal recurring billing.

    A monthly invoice repeats the vendor, amount and description by design; only
    the date separates it from a duplicate. Requiring the dates to be roughly a
    billing cycle apart keeps legitimate recurring invoices out of the flag.
    """

    date_signal = next((s for s in candidate.signals if s.signal == "invoice_date"), None)
    if date_signal is None or date_signal.matched:
        return False
    match = re.search(r"(\d+) days apart", date_signal.detail)
    if not match:
        return False
    days = int(match.group(1))
    return RECURRING_MIN_DAYS <= days <= RECURRING_MAX_DAYS and candidate.match_score >= BAND_HIGH


def build_payment_analysis(
    *,
    invoice_number: str | None,
    expected_amount: Decimal | None,
    currency: str | None,
    payment_attempts: list[dict],
    bank_transactions: list,
    reported_payment_count: int | None,
    reported_payment_amount: Decimal | None,
) -> PaymentAnalysis:
    """Check C - summarise every payment record against this invoice.

    Two reported payments are not two completed transfers. Only a settled bank
    transaction counts as a verified settlement.
    """

    records: list[PaymentRecordView] = []
    successful = failed = pending = 0

    for attempt in payment_attempts:
        status = str(attempt.get("status") or "PENDING").upper()
        if status == "SUCCESS":
            successful += 1
        elif status == "FAILED":
            failed += 1
        else:
            pending += 1
        records.append(
            PaymentRecordView(
                source="payment_attempt",
                reference=attempt.get("attempt_id"),
                amount=_as_decimal(attempt.get("amount")),
                currency=attempt.get("currency") or currency,
                status=status,
                # A synthetic execution is not a bank-verified settlement.
                verified=False,
                counted_as_settlement=status == "SUCCESS",
                detail=(
                    f"GhostLedger payment attempt {status}"
                    + (f" (reason: {attempt.get('failure_reason')})" if attempt.get("failure_reason") else "")
                    + "."
                ),
            )
        )

    verified = 0
    seen_references: set[str] = set()
    for txn in bank_transactions:
        txn_status = getattr(txn, "status", "settled")
        settled = txn_status == "settled"
        reference_key = (getattr(txn, "reference", None) or "").casefold()
        # One reference is one transfer, however many times it is stored. This
        # stops repeat seeding from looking like repeat payments.
        if reference_key:
            if reference_key in seen_references:
                continue
            seen_references.add(reference_key)
        if settled:
            verified += 1
        records.append(
            PaymentRecordView(
                source="bank_transaction",
                reference=getattr(txn, "reference", None),
                amount=getattr(txn, "amount", None),
                currency=getattr(txn, "currency", currency),
                status=str(txn_status),
                verified=settled,
                counted_as_settlement=settled,
                detail=f"Bank transaction {txn_status}."
                if settled
                else f"Bank transaction {txn_status}; not a completed settlement.",
            )
        )

    if reported_payment_count and reported_payment_count > 0:
        records.append(
            PaymentRecordView(
                source="customer_report",
                amount=reported_payment_amount,
                currency=currency,
                status="reported",
                verified=False,
                counted_as_settlement=False,
                detail=(
                    f"The customer reports {reported_payment_count} payment(s)"
                    + (f" totalling {_money(reported_payment_amount, currency)}" if reported_payment_amount else "")
                    + ". Reported payments are claims, not confirmed transfers."
                ),
            )
        )

    settlements = verified + sum(1 for r in records if r.source == "payment_attempt" and r.status == "SUCCESS")
    duplicate_suspected = settlements >= 2 or (reported_payment_count or 0) >= 2
    # A confirmed duplicate payment needs two verified settlements, never reports.
    confirmed = verified >= 2
    needs_bank = duplicate_suspected and not confirmed

    if confirmed:
        conclusion = (
            f"{verified} settled bank transactions exist for invoice {invoice_number or '(unknown)'}; "
            "this is a confirmed duplicate payment."
        )
    elif duplicate_suspected:
        conclusion = (
            f"{settlements} completed payment record(s) and {reported_payment_count or 0} customer report(s) "
            f"were found for invoice {invoice_number or '(unknown)'}. A duplicate payment is suspected but "
            "not confirmed: bank records must verify whether more than one transfer actually settled."
        )
    else:
        conclusion = "No evidence of more than one payment for this invoice."

    return PaymentAnalysis(
        invoice_number=invoice_number,
        records=records,
        successful_settlements=successful,
        failed_attempts=failed,
        pending_transactions=pending,
        customer_reported=reported_payment_count or 0,
        verified_settlements=verified,
        duplicate_payment_suspected=duplicate_suspected,
        confirmed_duplicate_payment=confirmed,
        needs_bank_verification=needs_bank,
        conclusion=conclusion,
    )


def _as_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - defensive parse of untrusted stored values
        return None


def decide_status(
    *,
    candidates: list[InvoiceCandidate],
    exact_duplicates: list[InvoiceCandidate],
    payment: PaymentAnalysis,
    history_available: bool,
    invoice_number: str | None,
    vendor: str | None,
) -> InvestigationStatus:
    """Turn the evidence into one of the seven investigation statuses."""

    # A duplicated *payment* and a duplicated *invoice* are different findings
    # and get different statuses. Check the payment evidence first, because a
    # second settlement is the more specific and more consequential result.
    if payment.confirmed_duplicate_payment:
        return InvestigationStatus.confirmed_duplicate_payment
    if exact_duplicates:
        return InvestigationStatus.confirmed_duplicate_invoice
    if payment.duplicate_payment_suspected:
        return InvestigationStatus.suspected_duplicate_payment

    strong = [c for c in candidates if c.match_score >= BAND_HIGH and not is_recurring_pattern(c)]
    if strong:
        return InvestigationStatus.possible_duplicate

    if not invoice_number and not vendor:
        return InvestigationStatus.insufficient_information
    if not history_available or not candidates:
        # Either the database holds no invoices at all, or none belong to this
        # customer. Absence of comparable records is not proof of uniqueness.
        return InvestigationStatus.insufficient_information

    recurring_only = candidates and all(is_recurring_pattern(c) for c in candidates)
    if recurring_only:
        return InvestigationStatus.no_duplicate_found
    return InvestigationStatus.no_duplicate_found


STATUS_GUIDANCE: dict[InvestigationStatus, tuple[str, bool, bool, bool]] = {
    # status: (recommended next action, needs agent 2, needs human, blocks payment)
    InvestigationStatus.no_duplicate_found: (
        "No duplicate evidence was found. Agent 1 raises no objection to proceeding.",
        False, False, False,
    ),
    InvestigationStatus.possible_duplicate: (
        "A similar invoice already exists. A human should confirm with the vendor which invoice is valid "
        "before any payment is made.",
        False, True, True,
    ),
    InvestigationStatus.confirmed_duplicate_invoice: (
        "This invoice duplicates one already on record. Do not pay it. A human must decide whether any "
        "corrective refund is owed.",
        False, True, True,
    ),
    InvestigationStatus.confirmed_duplicate_payment: (
        "Bank records confirm the invoice settled more than once. Do not pay it again. A human must decide "
        "whether a refund is owed and to whom.",
        False, True, True,
    ),
    InvestigationStatus.suspected_duplicate_payment: (
        "The invoice may have been paid more than once. Agent 2 must verify the settlements against bank "
        "records before anything else happens.",
        True, False, True,
    ),
    InvestigationStatus.requires_bank_verification: (
        "Bank verification is required before this case can proceed.",
        True, False, True,
    ),
    InvestigationStatus.requires_human_review: (
        "The investigation was inconclusive and needs human judgement.",
        False, True, True,
    ),
    InvestigationStatus.insufficient_information: (
        "There are not enough records to judge whether this invoice is a duplicate. Do not treat it as "
        "confirmed unique; a human should review it.",
        False, True, True,
    ),
}
