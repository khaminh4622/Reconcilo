"""Agent 2 - Bank Reconciliation Specialist.

Matches the payments a document reports against synthetic bank transactions.
A customer-reported payment is never marked verified unless a matching
transaction is actually found in the bank data.
"""

from __future__ import annotations

from decimal import Decimal

from agent3.schemas import AnalysisResult

from .repository import WorkflowRepository
from .schemas import AGENT_LABELS, AgentId, AgentReport, Finding, WorkflowCase, utc_now


def _money(value: Decimal | None, currency: str | None = None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f} {currency or ''}".strip()


class ReconciliationSpecialist:
    """Agent 2: reconcile reported payments against synthetic bank records."""

    agent = AgentId.agent_2

    def __init__(self, repository: WorkflowRepository):
        self.repository = repository

    def reconcile(self, case: WorkflowCase, analysis: AnalysisResult) -> AgentReport:
        started = utc_now()
        findings: list[Finding] = []

        claim = analysis.financial_information.payment_claim
        amount = analysis.financial_information.amount_paid
        currency = analysis.currency or analysis.financial_information.currency
        invoice_number = analysis.document_id or case.invoice_number
        vendor = analysis.vendor.vendor_name or case.vendor_name
        reference = claim.reference

        if amount is None or amount == 0:
            findings.append(
                Finding(
                    code="no_payment_claimed",
                    label="Reported payments",
                    status="info",
                    detail="The document does not report any payment, so there is nothing to reconcile.",
                )
            )
            return self._report(findings, resolved=True, summary="No payment required reconciliation.", data={
                "matched": False, "invoice_number": invoice_number,
            }, started=started)

        # 1. Try an exact reference match first: it is the strongest evidence.
        matched = []
        if reference:
            matched = self.repository.find_bank_transactions(reference=reference)
            if matched:
                findings.append(
                    Finding(
                        code="reference_matched",
                        label="Payment reference found in bank records",
                        status="passed" if self._amount_matches(matched, amount) else "failed",
                        detail=(
                            f"Reference {reference} was found in bank records"
                            + (
                                f" for {_money(matched[0].amount, matched[0].currency)}."
                                if self._amount_matches(matched, amount)
                                else f", but the amount differs from the reported {_money(amount, currency)}."
                            )
                        ),
                    )
                )

        # 2. Otherwise try amount + counterparty, then amount alone.
        # Skipped when the document names a reference that the bank does not
        # know: matching on amount alone would wrongly "verify" a payment whose
        # stated proof does not exist.
        reference_unmatched = bool(reference) and not matched
        if not matched and not reference_unmatched and vendor:
            matched = self.repository.find_bank_transactions(amount=amount, counterparty=vendor)
            if matched:
                findings.append(
                    Finding(
                        code="amount_counterparty_matched",
                        label="Matching bank credit found",
                        status="passed",
                        detail=f"A bank credit of {_money(amount, currency)} from {vendor} was found.",
                    )
                )

        if not matched and not reference_unmatched:
            matched = self.repository.find_bank_transactions(amount=amount)
            if matched:
                findings.append(
                    Finding(
                        code="amount_matched",
                        label="Bank transaction with the same amount found",
                        status="unverified",
                        detail=(
                            f"A bank transaction of {_money(amount, currency)} exists, but its reference or "
                            "counterparty does not confirm it belongs to this invoice."
                        ),
                    )
                )

        if not matched:
            if reference_unmatched:
                findings.append(
                    Finding(
                        code="reference_not_found",
                        label="Stated payment reference not in bank records",
                        status="failed",
                        detail=(
                            f"The document cites reference {reference}, which does not exist in the bank "
                            "records. Matching on amount alone would not confirm this specific payment."
                        ),
                    )
                )
            findings.append(
                Finding(
                    code="no_bank_match",
                    label="No matching bank transaction",
                    status="failed",
                    detail=(
                        f"No bank transaction matching {_money(amount, currency)}"
                        + (f" with reference {reference}" if reference else "")
                        + " was found. The reported payment remains unverified."
                    ),
                )
            )

        # 3. Duplicate settlements of the same invoice.
        if invoice_number:
            invoice_txns = [t for t in self.repository.list_bank_transactions(500)
                            if (t.reference or "").casefold() == invoice_number.casefold()]
            if len(invoice_txns) > 1:
                findings.append(
                    Finding(
                        code="duplicate_settlement",
                        label="Possible duplicate settlement",
                        status="unverified",
                        detail=f"{len(invoice_txns)} bank transactions reference invoice {invoice_number}.",
                    )
                )

        verified = bool(matched) and all(t.status == "settled" for t in matched) and self._amount_matches(matched, amount)
        if matched and any(t.status == "pending" for t in matched):
            findings.append(
                Finding(
                    code="settlement_pending",
                    label="Bank settlement pending",
                    status="unverified",
                    detail="A matching transaction exists but has not settled; it cannot be treated as complete.",
                )
            )
            verified = False
        if matched and any(t.status in {"failed", "returned"} for t in matched):
            findings.append(
                Finding(
                    code="settlement_reversed",
                    label="Bank transaction failed or returned",
                    status="failed",
                    detail="A matching transaction exists but is failed or returned.",
                )
            )
            verified = False

        findings.append(
            Finding(
                code="verification_result",
                label="Payment verification",
                status="passed" if verified else "unverified",
                detail=(
                    "The reported payment was matched to a settled bank transaction."
                    if verified
                    else "The reported payment could not be verified against bank records and must be reconciled by a human."
                ),
            )
        )

        resolved = verified
        summary = (
            "Reported payment matched a settled bank transaction."
            if verified
            else "Reported payment could not be verified against bank records."
        )
        return self._report(
            findings,
            resolved=resolved,
            summary=summary,
            data={
                "invoice_number": invoice_number,
                "reference": reference,
                "amount": str(amount),
                "currency": currency,
                "matched": verified,
                "matched_transactions": [t.model_dump(mode="json") for t in matched],
            },
            started=started,
        )

    @staticmethod
    def _amount_matches(transactions, amount: Decimal) -> bool:
        return any(abs(t.amount - amount) <= Decimal("0.005") for t in transactions)

    def _report(self, findings, resolved, summary, data, started) -> AgentReport:
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=summary,
            findings=findings,
            recommended_next=None,
            next_action=(
                "No further action required."
                if resolved
                else "A human must reconcile the reported payment against the bank statement."
            ),
            requires_human=not resolved,
            blocking=not resolved,
            resolved=resolved,
            data=data,
            started_at=started,
            completed_at=utc_now(),
        )
