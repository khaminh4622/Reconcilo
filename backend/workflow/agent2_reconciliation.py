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


def _field(txn, name, default=None):
    """Read a field from either a SyntheticTransaction or a dict row."""

    if isinstance(txn, dict):
        return txn.get(name, default)
    return getattr(txn, name, default)


def _money(value: Decimal | None, currency: str | None = None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f} {currency or ''}".strip()


class ReconciliationSpecialist:
    """Agent 2: reconcile reported payments against synthetic bank records."""

    agent = AgentId.agent_2

    def __init__(self, repository: WorkflowRepository):
        self.repository = repository

    # ------------------------------------------------------------------
    # Bank evidence lookup
    # ------------------------------------------------------------------
    def _bank_evidence(
        self,
        *,
        reference: str | None = None,
        amount=None,
        counterparty: str | None = None,
        invoice_number: str | None = None,
    ) -> list[dict]:
        """Bank credits matching a query, from every ledger in the system.

        GhostLedger has two sources of bank evidence and Agent 2 must consult
        both:

        * ``mock_bank_ledger`` - settlements the payment simulator created
          automatically (the normal case).
        * ``synthetic_bank_transactions`` - legacy rows and developer imports.

        Querying only the legacy table is what made a settled Mock Bank payment
        invisible to a payment-status inquiry.
        """

        from .bank_reconciliation import ReconciliationService

        try:
            rows = ReconciliationService(self.repository.path)._bank_credits()
        except Exception:  # noqa: BLE001
            rows = []

        def ref_of(row):
            return (row.get("payment_reference") or "").casefold()

        def inv_of(row):
            return (row.get("invoice_number") or "").casefold()

        wanted_ref = (reference or "").casefold()
        wanted_inv = (invoice_number or "").casefold()
        wanted_cp = " ".join((counterparty or "").split()).casefold()

        def bank_cp_of(row):
            return " ".join((row.get("counterparty") or "").split()).casefold()

        def counterparty_ok(row):
            bank_cp = bank_cp_of(row)
            return bool(bank_cp) and bool(wanted_cp) and (
                bank_cp in wanted_cp or wanted_cp in bank_cp
            )

        settled = [row for row in rows if row.get("settled")]

        # 1. A named reference must match. If the caller supplied one, a row
        #    with a *different* reference is not evidence for this payment,
        #    however tempting its amount looks.
        if wanted_ref:
            by_reference = [row for row in settled if ref_of(row) == wanted_ref]
            if by_reference:
                # Narrow by invoice when the bank row knows its invoice.
                by_both = [row for row in by_reference if not wanted_inv or inv_of(row) == wanted_inv]
                return by_both or by_reference
            # The reference is unknown to the bank: do not fall back to amount.
            return []

        # 2. No reference: resolve by invoice number, and only by invoice number.
        #    A bank credit belonging to another invoice is never evidence for
        #    this one, even when the amount and counterparty happen to agree.
        if wanted_inv:
            return [row for row in settled if inv_of(row) == wanted_inv]

        # 3. No stable identifier was supplied at all. Amount plus counterparty
        #    is the only remaining signal, and it arrives here solely because the
        #    caller is working without an invoice number.
        if amount is not None and not wanted_inv:
            return [
                row for row in settled
                if str(row.get("amount")) == str(amount) and counterparty_ok(row)
            ]
        return []

    def _invoice_reference(self, invoice_number: str | None) -> str | None:
        """The payment reference GhostLedger recorded for an invoice."""

        if not invoice_number:
            return None
        try:
            from .payment_store import PaymentRecordStore

            records = PaymentRecordStore(self.repository.path).list_for_invoice(invoice_number)
            for record in reversed(records):
                if record.provider_reference:
                    return record.provider_reference
        except Exception:  # noqa: BLE001
            pass
        return None

    def reconcile(self, case: WorkflowCase, analysis: AnalysisResult) -> AgentReport:
        started = utc_now()
        findings: list[Finding] = []

        claim = analysis.financial_information.payment_claim
        amount = analysis.financial_information.amount_paid
        currency = analysis.currency or analysis.financial_information.currency
        invoice_number = case.invoice_number or analysis.document_id
        # Resolve the invoice to the reference GhostLedger actually recorded, so
        # bank evidence is found by stable identifier rather than by amount.
        recorded_reference = self._invoice_reference(invoice_number)
        vendor = analysis.vendor.vendor_name or case.vendor_name
        # Prefer the stored payment reference over whatever the PDF quotes.
        reference = recorded_reference or claim.reference

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
                "matched": False, "invoice_number": invoice_number, "settlement_count": 0,
            }, started=started)

        # 1. Try an exact reference match first: it is the strongest evidence.
        matched = []
        if reference:
            matched = self._bank_evidence(
                reference=reference, invoice_number=invoice_number
            )
            if matched:
                findings.append(
                    Finding(
                        code="reference_matched",
                        label="Payment reference found in bank records",
                        status="passed" if self._amount_matches(matched, amount) else "failed",
                        detail=(
                            f"Reference {reference} was found in bank records"
                            + (
                                f" for {_money(Decimal(str(_field(matched[0], 'amount'))), _field(matched[0], 'currency'))}."
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
        # Without an invoice number there is nothing to scope the evidence to,
        # so amount+counterparty would verify an unrelated credit. Require the
        # invoice before accepting weaker evidence.
        if not matched and not reference_unmatched and vendor and invoice_number:
            matched = self._bank_evidence(
                amount=amount, counterparty=vendor, invoice_number=invoice_number
            )
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
            matched = self._bank_evidence(amount=amount, invoice_number=invoice_number)
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
            invoice_txns = self._bank_evidence(invoice_number=invoice_number)
            if len(invoice_txns) > 1:
                findings.append(
                    Finding(
                        code="duplicate_settlement",
                        label="Possible duplicate settlement",
                        status="unverified",
                        detail=f"{len(invoice_txns)} bank transactions reference invoice {invoice_number}.",
                    )
                )

        verified = bool(matched) and all(str(_field(t, "status") or "").casefold() in {"settled", "partial"} for t in matched) and self._amount_matches(matched, amount)
        if matched and any(str(_field(t, "status") or "").casefold() == "pending" for t in matched):
            findings.append(
                Finding(
                    code="settlement_pending",
                    label="Bank settlement pending",
                    status="unverified",
                    detail="A matching transaction exists but has not settled; it cannot be treated as complete.",
                )
            )
            verified = False
        if matched and any(str(_field(t, "status") or "").casefold() in {"failed", "returned"} for t in matched):
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
                # How many transactions genuinely settled. Agent 1 uses this to
                # decide whether a duplicate payment is confirmed.
                "settlement_count": len([t for t in matched if str(_field(t, "status") or "").casefold() in {"settled", "partial"}]),
                "matched_transactions": [t if isinstance(t, dict) else t.model_dump(mode="json") for t in matched],
            },
            started=started,
        )

    @staticmethod
    def _amount_matches(transactions, amount: Decimal) -> bool:
        """True when a bank row carries the same amount.

        Rows arrive as objects from the legacy table and as dicts from the Mock
        Bank ledger, so both shapes are handled.
        """

        def value_of(txn):
            if isinstance(txn, dict):
                return txn.get("amount")
            return getattr(txn, "amount", None)

        for txn in transactions:
            value = value_of(txn)
            try:
                if abs(Decimal(str(value)) - amount) <= Decimal("0.005"):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False


    # ------------------------------------------------------------------
    # Reconcile a payment Agent 5 has just initiated (runs BEFORE confirmation)
    # ------------------------------------------------------------------
    def reconciliation_report(self, case: WorkflowCase, recommendation: dict) -> AgentReport:
        """Wrap Agent 2's verdict as a structured report for the audit trail."""

        verdict = recommendation.get("verdict") or "NEEDS_REVIEW"
        matched = verdict in {"MATCHED", "SETTLEMENT_VERIFIED"}
        findings = [
            Finding(code="reconciliation_verdict", label="Reconciliation verdict", status="passed" if matched else "failed", detail=f"Agent 2 reported {verdict}."),
        ]
        if recommendation.get("matched_reference"):
            findings.append(
                Finding(
                    code="matched_reference", label="Matched bank reference", status="passed",
                    detail=(
                        f"Bank reference {recommendation['matched_reference']} for "
                        f"{recommendation.get('settled_amount')} {recommendation.get('currency') or ''}."
                    ).strip(),
                )
            )
        return AgentReport(
            agent=AgentId.agent_2,
            agent_name=AGENT_LABELS[AgentId.agent_2],
            summary=recommendation.get("summary") or f"Reconciliation verdict: {verdict}.",
            findings=findings,
            next_action=(
                "Settlement verified against the Mock Bank ledger."
                if matched
                else "A human must resolve the reconciliation result."
            ),
            requires_human=not matched,
            blocking=not matched,
            resolved=matched,
            data={"recommendation": recommendation, "verdict": verdict},
            started_at=utc_now(),
            completed_at=utc_now(),
        )

    def reconcile_payment(self, case: WorkflowCase) -> dict:
        """Compare the expected payment with what the Mock Bank actually holds."""

        from .bank_reconciliation import ReconciliationService

        service = ReconciliationService(self.repository.path)
        recommendation = service.recommendation()
        recommendation["case_id"] = case.case_id
        return recommendation

    def verify_settlement(self, case: WorkflowCase) -> AgentReport:
        """Reconcile a payment Agent 5 executed against bank evidence.

        Kept separate from :meth:`reconcile`, which examines what a *customer
        claimed*. This method only runs once a payment has been executed.
        """

        from .agent2_reconciliation_engine import ReconciliationAgent, ReconciliationRepository

        started = utc_now()
        engine = ReconciliationAgent(ReconciliationRepository(self.repository.path))
        result = engine.reconcile(case.case_id, case.invoice_number)

        findings = [
            Finding(code=f.code, label=f.label, status=f.status, detail=f.detail)
            for f in result.findings
        ]
        outcome = result.outcome.value
        verified = outcome == "SETTLEMENT_VERIFIED"

        if verified:
            summary = (
                f"Settlement verified: {result.settled_amount} {result.currency or ''} "
                f"confirmed by {result.matched_reference}."
            ).strip()
            next_action = "Settlement is corroborated by independent bank evidence."
        elif outcome == "DISCREPANCY_DETECTED":
            summary = (
                f"Discrepancy detected: payment recorded {result.expected_amount} "
                f"{result.currency or ''} but bank evidence shows {result.settled_amount}."
            ).replace("  ", " ")
            next_action = "A human must resolve the amount discrepancy before the invoice is treated as paid."
        else:
            summary = "Reconciliation required: no independent bank evidence confirms this payment."
            next_action = "A human must verify the payment against the bank before it is treated as settled."

        return AgentReport(
            agent=AgentId.agent_2,
            agent_name=AGENT_LABELS[AgentId.agent_2],
            summary=summary,
            findings=findings,
            next_action=next_action,
            requires_human=not verified,
            blocking=not verified,
            resolved=verified,
            data={
                "reconciliation": result.model_dump(mode="json"),
                "outcome": outcome,
                "independent_evidence": result.independent_evidence,
                "settlement_verified": verified,
                "invoice_should_be_paid": verified,
            },
            started_at=started,
            completed_at=utc_now(),
        )

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
