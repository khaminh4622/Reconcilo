"""The Payment Simulator: the only component allowed to move fictional money.

This is deliberately **not** an agent. It executes an approved payment
instruction against the Mock Bank and reports what the bank did. It does not
decide whether the payment succeeded in business terms - that is Agent 2's job
once it has checked the bank ledger - and it never retries anything.

Everything here was previously inside Agent 5. Splitting it out lets Agent 5
become the final confirmation and customer-response agent without holding the
keys to the payment rail.

Safety properties preserved from Agent 5:

* execution requires a valid, unexpired, unrevoked approval bound to the exact
  invoice, payee, amount and currency;
* the attempt is claimed atomically against a UNIQUE idempotency key, so two
  concurrent requests cannot both pay;
* an already-settled invoice is never paid again;
* a failed instruction never credits the account.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from .mock_bank import MockBank
from .payment_store import PaymentRecordStore
from .repository import WorkflowRepository
from .schemas import (
    AgentId,
    AgentReport,
    ApprovalRecord,
    Finding,
    PaymentAttempt,
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


class PaymentSimulatorService:
    """Executes approved synthetic payments against the Mock Bank."""

    def __init__(
        self,
        repository: WorkflowRepository,
        bank: MockBank | None = None,
        store: PaymentRecordStore | None = None,
    ):
        self.repository = repository
        self.bank = bank or MockBank(repository.path)
        self.store = store or PaymentRecordStore(repository.path)

    # ------------------------------------------------------------------
    def execute(
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
        """Run one synthetic payment instruction.

        The report is attributed to the simulator, not to Agent 5, so the agent
        trail shows money movement separately from final confirmation.
        """

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
                        f"An instruction for this invoice, payee and amount already exists "
                        f"({existing.provider_status}). The original was returned instead of "
                        "moving money again."
                    ),
                )
            )
            return self._report(
                findings,
                "Payment not re-executed: an instruction already exists for these terms.",
                started,
                executed=False,
                attempt=existing,
                key=key,
            )

        # --- 2. Approval must exist and bind to these exact terms -----------
        blocked = self._verify_approval(
            approval, invoice_number, payee, amount, currency, findings
        )
        if blocked:
            return self._blocked(findings, blocked, started)

        # --- 3. Never pay an invoice that already settled -------------------
        blocked = self._verify_invoice_state(invoice_number, findings)
        if blocked:
            return self._blocked(findings, blocked, started)

        # --- 4. Claim the attempt atomically --------------------------------
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
            return self._report(
                findings,
                "Payment not executed: a concurrent request already claimed it.",
                started,
                executed=False,
                attempt=claim.attempt,
                key=key,
            )
        attempt = claim.attempt

        # --- 5. Submit to the Mock Bank -------------------------------------
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
                    "Submission is not settlement; Agent 2 must verify it."
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
            attempt.failure_code = instruction.get("failure_code") or "PAYMENT_REJECTED"
            findings.append(
                Finding(
                    code="payment_failed",
                    label="Instruction rejected by the bank",
                    status="failed",
                    detail=f"{attempt.failure_code}: {attempt.failure_reason}",
                )
            )
        elif scenario in {"PENDING", "MISSING_SETTLEMENT"}:
            attempt.status = "PENDING"
            findings.append(
                Finding(
                    code="payment_processing",
                    label="Awaiting settlement",
                    status="unverified",
                    detail=(
                        "The bank has not settled this instruction. It must not be retried until "
                        "its status is known."
                    ),
                )
            )
        else:
            # The bank settled (or partially settled) it. The simulator still
            # does not declare success: Agent 2 must reconcile the ledger.
            attempt.status = "PENDING"
            findings.append(
                Finding(
                    code="awaiting_verification",
                    label="Awaiting Agent 2 verification",
                    status="info",
                    detail=(
                        "The bank posted the credit. The payment is not confirmed until Agent 2 "
                        "reconciles it against the Mock Bank ledger."
                    ),
                )
            )

        self.store.update(attempt)
        return self._report(
            findings,
            {
                "FAILED": "Payment instruction rejected by the Mock Bank.",
                "PENDING": "Payment instruction submitted; the bank has not settled it.",
                "MISSING_SETTLEMENT": "Payment instruction accepted but never settled.",
            }.get(scenario, "Payment instruction submitted; awaiting bank verification."),
            started,
            executed=True,
            attempt=attempt,
            key=key,
            instruction=instruction,
        )

    # ------------------------------------------------------------------
    def _verify_approval(self, approval, invoice_number, payee, amount, currency, findings) -> str | None:
        if approval is None:
            findings.append(
                Finding(
                    code="no_approval",
                    label="Human approval required",
                    status="failed",
                    detail="No human approval exists for this payment, so no money was moved.",
                )
            )
            return "Payment not executed: human approval is missing."
        if approval.revoked:
            findings.append(
                Finding(code="approval_revoked", label="Approval revoked", status="failed",
                        detail="The approval for this payment was revoked.")
            )
            return "Payment not executed: the approval was revoked."
        if approval.is_expired():
            findings.append(
                Finding(code="approval_expired", label="Approval expired", status="failed",
                        detail=f"The approval expired at {approval.expires_at}.")
            )
            return "Payment not executed: the approval has expired."
        mismatch = self._approval_mismatch(approval, invoice_number, payee, amount, currency)
        if mismatch:
            findings.append(
                Finding(code="approval_mismatch", label="Approval does not match this payment",
                        status="failed", detail=mismatch)
            )
            return "Payment not executed: the approval does not match the payment terms."
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
            return "Payment not executed: the invoice is already paid."
        findings.append(
            Finding(
                code="invoice_open", label="No existing settlement", status="passed",
                detail=f"Invoice {invoice_number} has no settled payment.",
            )
        )
        return None

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

    # ------------------------------------------------------------------
    def _report(
        self,
        findings: list[Finding],
        summary: str,
        started: str,
        *,
        executed: bool,
        attempt: PaymentAttempt | None,
        key: str,
        instruction: dict | None = None,
    ) -> AgentReport:
        """The simulator reports as its own actor, not as Agent 5."""

        failed = attempt is not None and attempt.status == "FAILED"
        return AgentReport(
            agent=AgentId.payment_simulator,
            agent_name="Payment Simulator",
            summary=summary,
            findings=findings,
            requires_human=False,
            blocking=failed,
            resolved=False,
            data={
                "executed": executed,
                "attempt": attempt.model_dump(mode="json") if attempt else None,
                "idempotency_key": key,
                "bank_instruction": instruction,
                "awaiting_verification": bool(executed and not failed),
            },
            started_at=started,
            completed_at=utc_now(),
        )

    def _blocked(self, findings: list[Finding], summary: str, started: str) -> AgentReport:
        return AgentReport(
            agent=AgentId.payment_simulator,
            agent_name="Payment Simulator",
            summary=summary,
            findings=findings,
            requires_human=True,
            blocking=True,
            resolved=False,
            data={"executed": False, "confirmed": False},
            started_at=started,
            completed_at=utc_now(),
        )
