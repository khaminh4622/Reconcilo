"""Resolve the payment status of an invoice's original submission.

A duplicate submission is not automatically a payment problem. Before Agent 1
blocks anything it determines what actually happened to the original invoice:

  ALREADY_PAID            one verified settlement covers the full amount
  UNPAID_ELIGIBLE...      nothing settled and nothing in flight
  PAYMENT_PENDING         an attempt is in flight; outcome unknown
  PAYMENT_FAILED          the last attempt failed and nothing settled
  PAYMENT_STATUS_UNKNOWN  records are missing or contradictory
  DUPLICATE_PAYMENT...    two or more distinct settlements

Everything here is deterministic. The LLM never decides a payment status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .agent1_schemas import ObligationState, PreviousPaymentStatus


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


@dataclass
class OriginalInvoicePaymentState:
    """The payment position of the original invoice."""

    status: PreviousPaymentStatus
    obligation_state: ObligationState
    paid_amount: Decimal | None = None
    invoice_amount: Decimal | None = None
    settlements: list[dict] = field(default_factory=list)
    payment_reference: str | None = None
    pending_attempts: list[dict] = field(default_factory=list)
    failed_attempts: list[dict] = field(default_factory=list)
    remaining_balance: Decimal | None = None
    reason: str = ""
    needs_agent_2: bool = False
    needs_agent_4: bool = False
    blocks_payment: bool = True

    @property
    def is_paid(self) -> bool:
        return self.status == PreviousPaymentStatus.already_paid


def resolve_payment_state(
    *,
    invoice_amount: Decimal | None,
    currency: str | None,
    payment_attempts: list[dict],
    bank_transactions: list,
) -> OriginalInvoicePaymentState:
    """Determine the payment status of the original invoice.

    Order of evidence, strongest first:
      1. settled bank transactions (verified money)
      2. successful payment attempts (GhostLedger believes it paid)
      3. pending attempts (uncertain)
      4. failed attempts (nothing settled)
      5. nothing -> unknown rather than "unpaid", unless the records are clear
    """

    # --- verified settlements, de-duplicated by reference -------------------
    settlements: list[dict] = []
    seen_refs: set[str] = set()
    total_settled = Decimal("0")
    # Bank statuses arrive in mixed case (the Mock Bank stores SETTLED, the
    # importer stores settled), so compare case-insensitively.
    SETTLED_STATUSES = {"settled", "partial"}

    for txn in bank_transactions:
        status = str(getattr(txn, "status", "settled") or "settled").casefold()
        if status not in SETTLED_STATUSES:
            continue
        reference = (getattr(txn, "reference", None) or "").casefold()
        if reference:
            if reference in seen_refs:
                continue
            seen_refs.add(reference)
        amount = getattr(txn, "amount", None)
        settlements.append(
            {
                "reference": getattr(txn, "reference", None),
                "amount": str(amount) if amount is not None else None,
                "status": status,
                "value_date": getattr(txn, "value_date", None),
            }
        )
        if isinstance(amount, Decimal):
            total_settled += amount

    # A successful payment *attempt* is GhostLedger's own execution record. It is
    # not independent settlement evidence: the money may never have left, and a
    # synthetic execution proves nothing about the bank. Attempts are therefore
    # tracked separately and never counted as verified settlements.
    attempt_successes = [
        a for a in payment_attempts if str(a.get("status")).upper() == "SUCCESS"
    ]
    completed_attempts = [
        {
            "reference": attempt.get("attempt_id"),
            "amount": attempt.get("amount"),
            "status": "SUCCESS",
            "value_date": attempt.get("attempted_at"),
            "source": "payment_attempt",
        }
        for attempt in attempt_successes
    ]
    # A successful attempt may corroborate a bank settlement, but only when the
    # bank itself shows the money moved. Without that, the payment stays a
    # completed attempt, not a confirmed settlement.
    unverified_attempts = (
        []
        if settlements
        else completed_attempts
    )

    pending = [a for a in payment_attempts if str(a.get("status")).upper() == "PENDING"]
    failed = [a for a in payment_attempts if str(a.get("status")).upper() == "FAILED"]

    reference = settlements[0]["reference"] if settlements else None

    # --- Case F: more than one verified settlement --------------------------
    # Only bank-verified settlements reach here.
    if len(settlements) >= 2:
        return OriginalInvoicePaymentState(
            status=PreviousPaymentStatus.duplicate_payment_detected,
            obligation_state=ObligationState.paid,
            paid_amount=total_settled,
            invoice_amount=invoice_amount,
            settlements=settlements,
            payment_reference=reference,
            reason=(
                f"{len(settlements)} distinct settled payments exist for this invoice "
                f"({', '.join(str(s['reference']) for s in settlements[:4])}). "
                "The invoice was paid more than once."
            ),
            needs_agent_2=False,
            needs_agent_4=False,
            blocks_payment=True,
        )

    # --- Case A: paid in full ----------------------------------------------
    if settlements and invoice_amount is not None and total_settled + Decimal("0.005") >= invoice_amount:
        return OriginalInvoicePaymentState(
            status=PreviousPaymentStatus.already_paid,
            obligation_state=ObligationState.paid,
            paid_amount=total_settled,
            invoice_amount=invoice_amount,
            settlements=settlements,
            payment_reference=reference,
            reason=(
                f"The original invoice was already paid in full: {reference} settled "
                f"{total_settled:.2f} {currency or ''}".strip()
                + f" against an invoice of {invoice_amount:.2f}."
            ),
            needs_agent_2=False,
            needs_agent_4=False,
            blocks_payment=True,
        )

    # Partially paid: treat as still open, but say so plainly.
    if settlements and invoice_amount is not None and total_settled > 0:
        remaining = invoice_amount - total_settled
        # A pending attempt on top of a partial settlement is uncertain.
        if pending:
            return OriginalInvoicePaymentState(
                status=PreviousPaymentStatus.payment_pending,
                obligation_state=ObligationState.pending,
                paid_amount=total_settled,
                invoice_amount=invoice_amount,
                settlements=settlements,
                payment_reference=reference,
                pending_attempts=pending,
                remaining_balance=remaining,
                reason=(
                    f"{total_settled:.2f} was settled but a further payment attempt is still pending. "
                    "The outcome is uncertain."
                ),
                needs_agent_2=True,
                blocks_payment=True,
            )
        return OriginalInvoicePaymentState(
            status=PreviousPaymentStatus.unpaid_eligible_for_approval,
            obligation_state=ObligationState.open,
            paid_amount=total_settled,
            invoice_amount=invoice_amount,
            settlements=settlements,
            payment_reference=reference,
            remaining_balance=remaining,
            reason=(
                f"The invoice is only partly paid: {total_settled:.2f} of {invoice_amount:.2f} settled, "
                f"leaving {remaining:.2f} {currency or ''}".strip()
                + " outstanding on the original obligation."
            ),
            needs_agent_2=False,
            needs_agent_4=False,
            blocks_payment=False,
        )

    # Completed attempts with no bank evidence: the payment was executed by
    # GhostLedger but the money is not confirmed. This is uncertain, never a
    # confirmed settlement, and needs reconciliation.
    if unverified_attempts:
        return OriginalInvoicePaymentState(
            status=PreviousPaymentStatus.payment_status_unknown,
            obligation_state=ObligationState.unknown,
            invoice_amount=invoice_amount,
            reason=(
                f"{len(unverified_attempts)} payment attempt(s) completed in GhostLedger, but no bank "
                "settlement corroborates them. A completed attempt is not a verified transfer, so the "
                "payment status is unknown pending reconciliation."
            ),
            needs_agent_2=True,
            blocks_payment=True,
        )

    # --- Case C: something is in flight ------------------------------------
    if pending:
        return OriginalInvoicePaymentState(
            status=PreviousPaymentStatus.payment_pending,
            obligation_state=ObligationState.pending,
            invoice_amount=invoice_amount,
            pending_attempts=pending,
            reason=(
                f"{len(pending)} payment attempt(s) for this invoice are still pending. "
                "No further payment may be made until the existing attempt is resolved."
            ),
            needs_agent_2=True,
            blocks_payment=True,
        )

    # --- Case D: the last attempt failed -----------------------------------
    if failed:
        return OriginalInvoicePaymentState(
            status=PreviousPaymentStatus.payment_failed,
            obligation_state=ObligationState.failed,
            invoice_amount=invoice_amount,
            failed_attempts=failed,
            reason=(
                f"The most recent payment attempt failed "
                f"({failed[-1].get('failure_reason') or 'reason not recorded'}). "
                "Nothing is known to have settled."
            ),
            needs_agent_2=False,
            needs_agent_4=True,
            blocks_payment=True,
        )

    # --- Case B vs Case E ---------------------------------------------------
    # No settlements, no pending, no failed attempts. This is only confidently
    # "unpaid" when we have an invoice amount to compare against and the
    # absence of records is meaningful.
    if invoice_amount is not None:
        # Unpaid is only asserted because the obligation exists and no money is
        # recorded against it. Absence of a payment record is evidence of an
        # unpaid invoice here, but the reason says exactly what was checked.
        return OriginalInvoicePaymentState(
            status=PreviousPaymentStatus.unpaid_eligible_for_approval,
            obligation_state=ObligationState.open,
            paid_amount=Decimal("0"),
            invoice_amount=invoice_amount,
            remaining_balance=invoice_amount,
            reason=(
                "No settled payment, pending attempt or failed attempt exists for this invoice, "
                "and no money is recorded against its obligation, so it is treated as unpaid."
            ),
            needs_agent_2=False,
            needs_agent_4=False,
            blocks_payment=False,
        )

    return OriginalInvoicePaymentState(
        status=PreviousPaymentStatus.payment_status_unknown,
        obligation_state=ObligationState.unknown,
        invoice_amount=invoice_amount,
        reason=(
            "The invoice amount could not be established, so whether it has been paid cannot be "
            "determined. An unpaid invoice must not be assumed."
        ),
        needs_agent_2=True,
        blocks_payment=True,
    )


# Status -> (blocks payment, needs agent 2, needs agent 4, needs human, approved path)
DECISION_TABLE: dict[PreviousPaymentStatus, tuple[bool, bool, bool, bool, str]] = {
    PreviousPaymentStatus.already_paid: (
        True, False, False, True,
        "The original invoice is already paid. Do not pay it again; a human may decide whether a "
        "refund is owed.",
    ),
    PreviousPaymentStatus.unpaid_eligible_for_approval: (
        False, False, False, False,
        "The original invoice is still unpaid, so the repeated submission reuses that obligation. "
        "Send it for human approval, then Agent 5 pays it once.",
    ),
    PreviousPaymentStatus.payment_pending: (
        True, True, False, True,
        "A payment for this invoice is still pending. Hold the case and verify the existing "
        "transaction with Agent 2 before anything else.",
    ),
    PreviousPaymentStatus.payment_failed: (
        True, False, True, True,
        "The previous payment attempt failed. Agent 4 must confirm nothing settled before a new "
        "payment decision is made.",
    ),
    PreviousPaymentStatus.payment_status_unknown: (
        True, True, False, True,
        "The payment status cannot be determined from the records. Route to Agent 2 for "
        "reconciliation; do not assume the invoice is unpaid.",
    ),
    PreviousPaymentStatus.duplicate_payment_detected: (
        True, False, False, True,
        "This invoice has been paid more than once. Block further payment and request human review. "
        "No refund is issued automatically.",
    ),
}
