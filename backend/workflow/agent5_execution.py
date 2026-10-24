"""Agent 5 - Payment Execution & Monitoring (synthetic).

Executes *simulated* payments only. No banking connection exists; nothing real
moves. Safety properties enforced here:

* a valid human approval must exist and must match the exact invoice, payee,
  currency and amount;
* an invoice that is already paid or has a pending attempt cannot be paid again;
* the idempotency key is unique in the database, so a repeated request returns
  the original attempt instead of executing twice;
* a payment is never retried automatically.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from .repository import WorkflowRepository
from .schemas import (
    AGENT_LABELS,
    AgentId,
    AgentReport,
    ApprovalRecord,
    Finding,
    PaymentAttempt,
    SyntheticTransaction,
    WorkflowCase,
    utc_now,
)


def build_idempotency_key(
    invoice_number: str | None,
    payee: str | None,
    amount: Decimal | None,
    currency: str | None,
) -> str:
    """Stable key for one payable obligation.

    Derived from the payment terms rather than the case, so the same invoice
    cannot be paid twice even from a different case.
    """

    material = "|".join(
        [
            " ".join((invoice_number or "").split()).casefold(),
            " ".join((payee or "").split()).casefold(),
            f"{amount:.2f}" if amount is not None else "",
            (currency or "").upper(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class PaymentExecutionAgent:
    """Agent 5: execute an approved payment and report its status."""

    agent = AgentId.agent_5

    def __init__(self, repository: WorkflowRepository):
        self.repository = repository

    def execute(
        self,
        case: WorkflowCase,
        approval: ApprovalRecord | None,
        invoice_number: str | None,
        payee: str | None,
        amount: Decimal | None,
        currency: str | None,
        simulate: str = "SUCCESS",
        failure_reason: str | None = None,
    ) -> AgentReport:
        started = utc_now()
        findings: list[Finding] = []
        key = build_idempotency_key(invoice_number, payee, amount, currency)

        # --- 1. Idempotency: has this exact payment been attempted before? ----
        existing = self.repository.find_attempt_by_key(key)
        if existing is not None:
            findings.append(
                Finding(
                    code="duplicate_execution_blocked",
                    label="Repeated execution request blocked",
                    status="failed",
                    detail=(
                        f"An attempt for this invoice/payee/amount already exists with status "
                        f"{existing.status}. The original attempt was returned instead of paying again."
                    ),
                )
            )
            return AgentReport(
                agent=self.agent,
                agent_name=AGENT_LABELS[self.agent],
                summary=f"Payment not executed: an identical attempt already exists ({existing.status}).",
                findings=findings,
                requires_human=existing.status == "PENDING",
                blocking=True,
                resolved=existing.status == "SUCCESS",
                data={"existing_attempt": existing.model_dump(mode="json"), "executed": False},
                started_at=started,
                completed_at=utc_now(),
            )

        # --- 2. A pending attempt for the same invoice must be investigated --
        prior_pending = [
            a for a in self.repository.find_invoice_payments(invoice_number, payee) if a.get("status") == "PENDING"
        ]
        if prior_pending:
            findings.append(
                Finding(
                    code="pending_attempt_exists",
                    label="Uncertain previous attempt",
                    status="unverified",
                    detail=(
                        "A previous payment attempt for this invoice is still PENDING. It must be "
                        "investigated before any new payment is made."
                    ),
                )
            )
            return AgentReport(
                agent=self.agent,
                agent_name=AGENT_LABELS[self.agent],
                summary="Payment not executed: a previous attempt is still pending.",
                findings=findings,
                requires_human=True,
                blocking=True,
                resolved=False,
                data={"pending_attempts": prior_pending, "executed": False},
                started_at=started,
                completed_at=utc_now(),
            )

        # --- 3. Approval must exist and bind to these exact terms ------------
        if approval is None:
            findings.append(
                Finding(
                    code="no_approval",
                    label="Human approval required",
                    status="failed",
                    detail="No human approval exists for this payment, so it was not executed.",
                )
            )
            return self._blocked(findings, "Payment not executed: human approval is missing.", started)

        mismatch = self._approval_mismatch(approval, invoice_number, payee, amount, currency)
        if mismatch:
            findings.append(
                Finding(
                    code="approval_mismatch",
                    label="Approval does not match this payment",
                    status="failed",
                    detail=mismatch,
                )
            )
            return self._blocked(findings, "Payment not executed: the approval does not match the payment terms.", started)

        if approval.revoked:
            findings.append(
                Finding(
                    code="approval_revoked",
                    label="Approval revoked",
                    status="failed",
                    detail="The approval for this payment was revoked and cannot be used.",
                )
            )
            return self._blocked(findings, "Payment not executed: the approval was revoked.", started)

        findings.append(
            Finding(
                code="approval_valid",
                label="Human approval verified",
                status="passed",
                detail=f"Approved by {approval.approved_by} for {amount:.2f} {currency} to {payee}.",
            )
        )

        # --- 4. Never pay an invoice that is already settled -----------------
        prior_success = [
            a for a in self.repository.find_invoice_payments(invoice_number, payee) if a.get("status") == "SUCCESS"
        ]
        if prior_success:
            findings.append(
                Finding(
                    code="invoice_already_paid",
                    label="Invoice already paid",
                    status="failed",
                    detail=f"Invoice {invoice_number} has already been paid successfully; no second payment was made.",
                )
            )
            return self._blocked(findings, "Payment not executed: the invoice is already paid.", started)

        # --- 5. Execute the simulated payment --------------------------------
        status = simulate.upper()
        if status not in {"SUCCESS", "FAILED", "PENDING"}:
            status = "SUCCESS"
        attempt = PaymentAttempt(
            idempotency_key=key,
            approval_id=approval.approval_id,
            payee=payee,
            amount=amount,
            currency=currency,
            invoice_number=invoice_number,
            status=status,  # type: ignore[arg-type]
            failure_reason=failure_reason if status == "FAILED" else None,
            attempted_at=utc_now(),
        )
        if status == "SUCCESS":
            transaction = SyntheticTransaction(
                reference=f"PMT-{attempt.attempt_id[:8].upper()}",
                amount=amount or Decimal("0"),
                currency=currency or "USD",
                direction="debit",
                counterparty=payee,
                status="settled",
                value_date=utc_now()[:10],
                source="synthetic_bank",
            )
            self.repository.record_bank_transaction(transaction)
            attempt.transaction_id = transaction.transaction_id
            findings.append(
                Finding(
                    code="payment_success",
                    label="Simulated payment settled",
                    status="passed",
                    detail=f"Synthetic payment {transaction.reference} settled for {amount:.2f} {currency}.",
                )
            )
        elif status == "FAILED":
            findings.append(
                Finding(
                    code="payment_failed",
                    label="Simulated payment failed",
                    status="failed",
                    detail=failure_reason or "The simulated payment was rejected by the payment network.",
                )
            )
        else:
            findings.append(
                Finding(
                    code="payment_pending",
                    label="Simulated payment pending",
                    status="unverified",
                    detail="The payment was submitted but has not settled; the outcome is uncertain.",
                )
            )

        attempt.failure_reason = attempt.failure_reason or (failure_reason if status == "FAILED" else None)
        self.repository.save_attempt(case.case_id, attempt)

        next_agent = AgentId.agent_4 if status in {"FAILED", "PENDING"} else None
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary={
                "SUCCESS": "Synthetic payment executed successfully.",
                "FAILED": "Synthetic payment failed and needs exception handling.",
                "PENDING": "Synthetic payment is pending; its outcome is uncertain.",
            }[status],
            findings=findings,
            recommended_next=next_agent,
            next_action=(
                "Record the transaction and complete the workflow."
                if status == "SUCCESS"
                else "Agent 4 must investigate the exception before anything else happens."
            ),
            requires_human=status != "SUCCESS",
            blocking=status != "SUCCESS",
            resolved=status == "SUCCESS",
            data={
                "executed": True,
                "attempt": attempt.model_dump(mode="json"),
                "idempotency_key": key,
            },
            started_at=started,
            completed_at=utc_now(),
        )

    @staticmethod
    def _approval_mismatch(
        approval: ApprovalRecord,
        invoice_number: str | None,
        payee: str | None,
        amount: Decimal | None,
        currency: str | None,
    ) -> str | None:
        def norm(v: str | None) -> str:
            return " ".join((v or "").split()).casefold()

        if norm(approval.invoice_number) != norm(invoice_number):
            return f"Approval is for invoice {approval.invoice_number}, but this payment is for {invoice_number}."
        if norm(approval.payee) != norm(payee):
            return f"Approval is for payee {approval.payee}, but this payment is to {payee}."
        if approval.amount is None or amount is None or approval.amount != amount:
            return f"Approval amount {approval.amount} does not match the payment amount {amount}."
        if norm(approval.currency) != norm(currency):
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
            data={"executed": False},
            started_at=started,
            completed_at=utc_now(),
        )
