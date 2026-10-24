"""Agent 5 - Payment Execution and Final Confirmation.

Agent 5 has two separate responsibilities that must never be merged:

``initiate_payment``   (before Agent 2)
    Validates the human approval, prevents duplicate execution, and asks the
    Mock Bank to move money. It does **not** decide that money arrived.

``confirm_payment``    (after Agent 2)
    Reads Agent 2's reconciliation verdict and, only when Agent 2 reports
    MATCHED with independent settlement evidence, marks the invoice PAID and
    issues the final confirmation receipt. For every other verdict it refuses
    to confirm success.

Execution and confirmation are split because a payment agent that both moves
money and certifies that it arrived can never be independently checked.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from .mock_bank import MockBank
from .payment_store import PaymentRecordStore, ReceiptStore
from .repository import WorkflowRepository
from .schemas import (
    AGENT_LABELS,
    AgentId,
    AgentReport,
    ApprovalRecord,
    Finding,
    PaymentAttempt,
    PaymentReceipt,
    WorkflowCase,
    utc_now,
)


def build_idempotency_key(
    invoice_number: str | None,
    payee: str | None,
    amount: Decimal | None,
    currency: str | None,
) -> str:
    """Stable key for one payable obligation."""

    material = "|".join(
        [
            " ".join((invoice_number or "").split()).casefold(),
            " ".join((payee or "").split()).casefold(),
            f"{amount:.2f}" if amount is not None else "",
            (currency or "").upper(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _norm(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


# Verdicts from Agent 2 that permit Agent 5 to finalise a payment.
CONFIRMABLE_VERDICTS = {"MATCHED", "SETTLEMENT_VERIFIED"}

# Verdicts that must block confirmation.
BLOCKING_VERDICTS = {
    "PENDING",
    "MISSING_PAYMENT",
    "MISSING_SETTLEMENT",
    "PARTIAL_PAYMENT",
    "OVERPAYMENT",
    "DUPLICATE_SETTLEMENT",
    "DUPLICATE_BANK_TRANSACTION",
    "AMOUNT_MISMATCH",
    "CURRENCY_MISMATCH",
    "UNMATCHED_BANK_TRANSACTION",
    "NEEDS_REVIEW",
    "NEEDS_HUMAN_REVIEW",
    "RECONCILIATION_REQUIRED",
    "DISCREPANCY_DETECTED",
}


class PaymentExecutionAgent:
    """Agent 5: initiate an approved payment, then confirm the verified result."""

    agent = AgentId.agent_5

    def __init__(
        self,
        repository: WorkflowRepository,
        bank: MockBank | None = None,
        store: PaymentRecordStore | None = None,
        receipts: ReceiptStore | None = None,
    ):
        self.repository = repository
        self.bank = bank or MockBank(repository.path)
        self.store = store or PaymentRecordStore(repository.path)
        self.receipts = receipts or ReceiptStore(repository.path)

    # ==================================================================
    # STEP 1 - initiation / execution (runs BEFORE Agent 2)
    # ==================================================================
    def initiate_payment(
        self,
        case: WorkflowCase,
        approval: ApprovalRecord | None,
        invoice_number: str | None,
        payee: str | None,
        amount: Decimal | None,
        currency: str | None,
        simulate: str | None = None,
        failure_reason: str | None = None,
        failure_code: str | None = None,
    ) -> AgentReport:
        started = utc_now()
        findings: list[Finding] = []
        key = build_idempotency_key(invoice_number, payee, amount, currency)

        # --- 1. An existing attempt always short-circuits -------------------
        existing = self.store.find_by_key(key)
        if existing is not None:
            findings.append(
                Finding(
                    code="duplicate_execution_blocked",
                    label="Repeated execution request blocked",
                    status="unverified",
                    detail=(
                        f"An attempt for this invoice, payee and amount already exists "
                        f"({existing.provider_status}). The original instruction was returned "
                        "instead of sending money again."
                    ),
                )
            )
            return AgentReport(
                agent=self.agent,
                agent_name=AGENT_LABELS[self.agent],
                summary="Payment not re-executed: an instruction already exists for these terms.",
                findings=findings,
                requires_human=False,
                blocking=False,
                resolved=False,
                data={
                    "executed": False,
                    "attempt": existing.model_dump(mode="json"),
                    "idempotency_key": key,
                    "awaiting_reconciliation": True,
                },
                started_at=started,
                completed_at=utc_now(),
            )

        # --- 2. Verify the approval ----------------------------------------
        blocked = self._verify_approval(
            approval, invoice_number, payee, amount, currency, findings
        )
        if blocked:
            return self._blocked(findings, blocked, started)

        # --- 3. Verify the invoice / history state -------------------------
        blocked = self._verify_invoice_state(invoice_number, findings)
        if blocked:
            return self._blocked(findings, blocked, started)

        # --- 4. Claim the attempt atomically -------------------------------
        attempt = PaymentAttempt(
            idempotency_key=key,
            approval_id=approval.approval_id if approval else None,
            invoice_number=invoice_number,
            payee=payee,
            amount=amount,
            currency=currency,
            case_id=case.case_id,
            provider_status="INITIATED",
            status="PENDING",
        )
        claim = self.store.claim(attempt)
        if not claim.created:
            findings.append(
                Finding(
                    code="concurrent_execution_blocked",
                    label="Concurrent execution blocked",
                    status="unverified",
                    detail="Another request claimed this payment first; no second transfer was sent.",
                )
            )
            return AgentReport(
                agent=self.agent,
                agent_name=AGENT_LABELS[self.agent],
                summary="Payment not executed: a concurrent request already claimed it.",
                findings=findings,
                blocking=False,
                resolved=False,
                data={"executed": False, "attempt": claim.attempt.model_dump(mode="json")},
                started_at=started,
                completed_at=utc_now(),
            )
        attempt = claim.attempt

        # --- 5. Ask the Mock Bank to move the money ------------------------
        instruction = self.bank.submit_instruction(
            idempotency_key=key,
            amount=amount or Decimal("0"),
            currency=currency or "USD",
            payee=payee,
            invoice_number=invoice_number,
            scenario=simulate,
            customer_name=case.customer_name,
            expected_amount=amount,
            failure_code=failure_code,
        )
        attempt.provider_reference = instruction["payment_reference"]
        scenario = instruction["outcome"]
        attempt.provider_status = {
            "SUCCESS": "SETTLED",
            "PARTIAL": "PARTIAL",
            "FAILED": "FAILED",
            "PENDING": "PENDING",
            "MISSING_SETTLEMENT": "MISSING_SETTLEMENT",
            "DUPLICATE_SETTLEMENT": "SETTLED",
        }.get(scenario, "PENDING")
        findings.append(
            Finding(
                code="payment_initiated",
                label="Instruction accepted by the Mock Bank",
                status="info",
                detail=(
                    f"The bank accepted the instruction under {instruction['payment_reference']}. "
                    "Initiation is not settlement; Agent 2 must reconcile it."
                ),
            )
        )

        if scenario == "FAILED":
            attempt.status = "FAILED"
            attempt.failure_reason = (
                failure_reason
                or instruction.get("failure_reason")
                or "The Mock Bank rejected the instruction."
            )
            # Always the bank's own code: Agent 4 must never have to guess.
            attempt.failure_code = instruction.get("failure_code") or "REJECTED"
            findings.append(
                Finding(
                    code="payment_failed", label="Instruction rejected", status="failed",
                    detail=attempt.failure_reason,
                )
            )
        elif scenario in {"PENDING", "MISSING_SETTLEMENT"}:
            attempt.status = "PENDING"
            findings.append(
                Finding(
                    code="payment_processing", label="Awaiting clearance", status="unverified",
                    detail=(
                        "The Mock Bank accepted the instruction but has not settled it. "
                        "Agent 2 will report it as pending."
                    ),
                )
            )
        else:
            # The bank settled it. Agent 5 still does not declare success: the
            # settlement must be reconciled first.
            attempt.status = "PENDING"
            findings.append(
                Finding(
                    code="awaiting_reconciliation", label="Awaiting Agent 2", status="info",
                    detail=(
                        "The bank posted the credit. The payment is not confirmed until Agent 2 "
                        "reconciles it against the bank ledger."
                    ),
                )
            )

        self.store.update(attempt)
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=(
                "Payment instruction rejected by the Mock Bank."
                if attempt.status == "FAILED"
                else "Payment instruction submitted; awaiting bank reconciliation."
            ),
            findings=findings,
            recommended_next=AgentId.agent_4 if attempt.status == "FAILED" else None,
            next_action=(
                "Agent 4 must investigate the rejected instruction."
                if attempt.status == "FAILED"
                else "Agent 2 must reconcile the instruction against the Mock Bank ledger."
            ),
            requires_human=False,
            blocking=attempt.status == "FAILED",
            resolved=False,
            data={
                "executed": True,
                "attempt": attempt.model_dump(mode="json"),
                "idempotency_key": key,
                "bank_instruction": instruction,
                "awaiting_reconciliation": attempt.status != "FAILED",
            },
            started_at=started,
            completed_at=utc_now(),
        )

    # ==================================================================
    # STEP 2 - final confirmation (runs AFTER Agent 2)
    # ==================================================================
    def confirm_payment(
        self,
        case: WorkflowCase,
        recommendation: dict | None,
        approval: ApprovalRecord | None = None,
    ) -> AgentReport:
        """Finalise the case based on Agent 2's verified outcome."""

        started = utc_now()
        findings: list[Finding] = []
        verdict = (recommendation or {}).get("verdict") or "NEEDS_REVIEW"
        independent = bool((recommendation or {}).get("independent_evidence"))

        attempt = self._attempt_for(case)

        if verdict in CONFIRMABLE_VERDICTS and independent and attempt is not None:
            return self._finalise_paid(case, attempt, recommendation, approval, findings, started)
        if verdict in CONFIRMABLE_VERDICTS and not independent:
            findings.append(
                Finding(
                    code="unverified_evidence", label="Not independently verified", status="unverified",
                    detail=(
                        "Agent 2 matched the payment but could not corroborate it with independent "
                        "settlement evidence, so Agent 5 will not confirm it."
                    ),
                )
            )
            return self._refuse(case, attempt, verdict, findings, started, "PENDING")

        findings.append(
            Finding(
                code="reconciliation_not_confirmed", label="Settlement not confirmed", status="failed",
                detail=(
                    f"Agent 2 reported {verdict}. Agent 5 cannot confirm a successful payment on "
                    "that basis."
                ),
            )
        )
        # An unsettled transfer keeps the case pending; anything else is a
        # problem a human must resolve.
        pending_verdicts = {"PENDING", "PROCESSING", "NEEDS_HUMAN_REVIEW"}
        status = "PENDING" if verdict in pending_verdicts else "REQUIRES_REVIEW"
        return self._refuse(case, attempt, verdict, findings, started, status)

    # ------------------------------------------------------------------
    def _finalise_paid(
        self, case, attempt, recommendation, approval, findings, started
    ) -> AgentReport:
        attempt.status = "SUCCESS"
        attempt.provider_status = "SETTLED"
        attempt.verified = True
        attempt.settled_at = utc_now()
        attempt.settlement_reference = (
            recommendation.get("matched_reference") or attempt.provider_reference
        )
        # Link to the bank ledger entry that Agent 2 verified, so the receipt and
        # the bank record refer to the same transaction.
        bank_entries = self.bank.credits_for(attempt.provider_reference)
        settled_entries = [
            e for e in bank_entries
            if (e.get("status") or "").upper() in {"SETTLED", "PARTIAL"}
        ]
        if settled_entries:
            attempt.transaction_id = settled_entries[-1]["entry_id"]
        self.store.update(attempt)

        self._mark_invoice_paid(case, attempt.invoice_number, attempt.amount, attempt.currency)
        receipt = self._issue_receipt(case, attempt, recommendation, approval)
        self.receipts.save(receipt)

        findings.append(
            Finding(
                code="payment_confirmed", label="Payment confirmed as settled", status="passed",
                detail=(
                    f"Agent 2 verified {recommendation.get('settled_amount')} "
                    f"{recommendation.get('currency') or attempt.currency} under "
                    f"{recommendation.get('matched_reference') or attempt.provider_reference}."
                ),
            )
        )
        findings.append(
            Finding(
                code="invoice_marked_paid", label="Invoice marked PAID", status="passed",
                detail=f"Invoice {attempt.invoice_number} is now PAID after verified reconciliation.",
            )
        )
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=f"Payment confirmed. Invoice {attempt.invoice_number} is PAID; receipt issued.",
            findings=findings,
            next_action="Final confirmation issued. Receipt available.",
            requires_human=False,
            blocking=False,
            resolved=True,
            data={
                "confirmed": True,
                "final_status": "PAID",
                "attempt": attempt.model_dump(mode="json"),
                "receipt": receipt.model_dump(mode="json"),
                "reconciliation": recommendation,
            },
            started_at=started,
            completed_at=utc_now(),
        )

    def _refuse(self, case, attempt, verdict, findings, started, status: str) -> AgentReport:
        if attempt is not None and status == "REQUIRES_REVIEW":
            attempt.failure_reason = attempt.failure_reason or f"Agent 2 reported {verdict}."
            self.store.update(attempt)
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=f"Payment not confirmed: Agent 2 reported {verdict}.",
            findings=findings,
            next_action=(
                "A human must resolve the reconciliation result before payment is confirmed."
            ),
            requires_human=True,
            blocking=True,
            resolved=False,
            data={
                "confirmed": False,
                "final_status": status,
                "verdict": verdict,
                "attempt": attempt.model_dump(mode="json") if attempt else None,
            },
            started_at=started,
            completed_at=utc_now(),
        )

    def _attempt_for(self, case: WorkflowCase) -> PaymentAttempt | None:
        if case.payment is not None:
            return case.payment
        history = self.store.list_for_invoice(case.invoice_number)
        return history[-1] if history else None

    # ------------------------------------------------------------------
    def _verify_approval(self, approval, invoice_number, payee, amount, currency, findings) -> str | None:
        if approval is None:
            findings.append(
                Finding(
                    code="no_approval", label="Human approval required", status="failed",
                    detail="No human approval exists for this payment, so no money was moved.",
                )
            )
            return "Payment not initiated: human approval is missing."
        if approval.revoked:
            findings.append(
                Finding(
                    code="approval_revoked", label="Approval revoked", status="failed",
                    detail="The approval for this payment was revoked.",
                )
            )
            return "Payment not initiated: the approval was revoked."
        if approval.is_expired():
            findings.append(
                Finding(
                    code="approval_expired", label="Approval expired", status="failed",
                    detail=f"The approval expired at {approval.expires_at}.",
                )
            )
            return "Payment not initiated: the approval has expired."
        mismatch = self._approval_mismatch(approval, invoice_number, payee, amount, currency)
        if mismatch:
            findings.append(
                Finding(
                    code="approval_mismatch", label="Approval does not match this payment",
                    status="failed", detail=mismatch,
                )
            )
            return "Payment not initiated: the approval does not match the payment terms."
        findings.append(
            Finding(
                code="approval_valid", label="Human approval verified", status="passed",
                detail=(
                    f"Approved by {approval.approved_by} for {amount:.2f} {currency} "
                    f"to {payee} against invoice {invoice_number}."
                ),
            )
        )
        return None

    def _verify_invoice_state(self, invoice_number, findings) -> str | None:
        history = self.store.list_for_invoice(invoice_number)
        settled = [a for a in history if a.status == "SUCCESS" and a.verified]
        if settled:
            findings.append(
                Finding(
                    code="invoice_already_paid", label="Invoice already paid", status="failed",
                    detail=(
                        f"Invoice {invoice_number} was already settled under "
                        f"{settled[-1].settlement_reference or settled[-1].provider_reference}."
                    ),
                )
            )
            return "Payment not initiated: the invoice is already paid."
        findings.append(
            Finding(
                code="invoice_open", label="No existing settlement", status="passed",
                detail=f"Invoice {invoice_number} has no settled payment.",
            )
        )
        return None

    def _mark_invoice_paid(self, case, invoice_number, amount, currency) -> None:
        from .obligations import ObligationRepository, obligation_key

        obligations = ObligationRepository(self.repository.path)
        key = obligation_key(invoice_number, case.vendor_name, currency or case.currency)
        obligation = obligations.get_obligation(key)
        if obligation is not None:
            obligations.set_obligation_state(obligation["obligation_id"], "PAID", amount)
        else:
            found, _created = obligations.ensure_obligation(
                invoice_number=invoice_number,
                vendor=case.vendor_name,
                customer=case.customer_name,
                currency=currency or case.currency,
                amount=amount,
                case_id=case.case_id,
            )
            obligations.set_obligation_state(found["obligation_id"], "PAID", amount)

    def _issue_receipt(self, case, attempt, recommendation, approval) -> PaymentReceipt:
        return PaymentReceipt(
            case_id=case.case_id,
            invoice_number=attempt.invoice_number,
            vendor_name=case.vendor_name,
            customer_name=case.customer_name,
            amount=attempt.amount,
            currency=attempt.currency,
            provider_reference=attempt.provider_reference,
            settlement_reference=attempt.settlement_reference,
            transaction_id=attempt.transaction_id,
            approved_by=approval.approved_by if approval else None,
            approved_at=approval.approved_at if approval else None,
            settled_at=attempt.settled_at,
            outcome="SETTLED",
            verification=[
                "Human approval verified against invoice, payee, amount and currency",
                "Instruction accepted by the Mock Bank",
                "Agent 2 reconciled the instruction against the bank ledger",
                f"Agent 2 reported {recommendation.get('verdict')} with independent evidence",
                "Agent 5 confirmed the final payment status",
            ],
        )

    @staticmethod
    def _approval_mismatch(approval, invoice_number, payee, amount, currency) -> str | None:
        if _norm(approval.invoice_number) != _norm(invoice_number):
            return f"Approval is for invoice {approval.invoice_number}, but this payment is for {invoice_number}."
        if _norm(approval.payee) != _norm(payee):
            return f"Approval is for payee {approval.payee}, but this payment is to {payee}."
        if approval.amount is None or amount is None or approval.amount != amount:
            return f"Approval amount {approval.amount} does not match the payment amount {amount}."
        if _norm(approval.currency) != _norm(currency):
            return f"Approval currency {approval.currency} does not match the payment currency {currency}."
        return None

    def _blocked(self, findings: list[Finding], summary: str, started: str) -> AgentReport:
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=summary,
            findings=findings,
            requires_human=True,
            blocking=True,
            resolved=False,
            data={"executed": False, "confirmed": False},
            started_at=started,
            completed_at=utc_now(),
        )
