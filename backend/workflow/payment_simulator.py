"""The Payment Simulator: submits approved payments to the Mock Bank.

This is deliberately **not** an agent. It executes an approved payment
instruction against the Mock Bank and reports what the bank did. It does not
decide whether the payment succeeded in business terms - that is Agent 2's job
once it has checked the bank's own settled entries - and it never retries
anything.

Safety properties:

* execution requires a valid, unexpired, unrevoked approval bound to the exact
  invoice, payee, amount and currency;
* the bank will only process a payment request that a human authorised, so the
  request itself is the idempotency barrier (``idempotency_key`` is UNIQUE);
* an already-settled invoice is never paid again;
* a refused instruction never credits the account, and an uncertain one is
  preserved for investigation rather than retried.

``execute()`` keeps its signature for compatibility: the ``simulate`` argument
still lets a demo force a bank outcome (FAILED, PARTIAL, PENDING, ...), but it
now reaches the bank as part of the settlement request instead of bypassing the
accounts ledger.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from .bank_service import BankService, Settlement
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


# Bank scenarios the demo can force, expressed as a request state change.
FORCED_OUTCOMES = {
    "FAILED": ("FAILED", "PAYMENT_REJECTED"),
    "PENDING": ("UNCERTAIN", "BANK_TIMEOUT"),
    "MISSING_SETTLEMENT": ("UNCERTAIN", "BANK_TIMEOUT"),
}


class PaymentSimulatorService:
    """Executes approved synthetic payments against the Mock Bank."""

    def __init__(
        self,
        repository: WorkflowRepository,
        bank: MockBank | None = None,
        store: PaymentRecordStore | None = None,
    ):
        self.repository = repository
        # ``bank`` / ``ledger`` stay the raw MockBank ledger, exactly what
        # existing callers and tests read and swap. Account-based settlement
        # lives behind ``self.accounts`` so nothing changes for legacy callers.
        self.ledger = bank or MockBank(repository.path)
        self.bank = self.ledger
        self.store = store or PaymentRecordStore(repository.path)
        self.accounts = BankService(repository.path)

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
        """Run one synthetic payment instruction through the bank.

        The report is attributed to the simulator, not to Agent 5, so the agent
        trail shows money movement separately from final confirmation.
        """

        started = utc_now()
        findings: list[Finding] = []
        key = build_idempotency_key(invoice_number, payee, amount, currency)

        # --- 0. An existing attempt always short-circuits -------------------
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
            return AgentReport(
                agent=AgentId.payment_simulator,
                agent_name="Payment Simulator",
                summary="Payment not re-executed: an instruction already exists for these terms.",
                findings=findings,
                requires_human=False,
                blocking=False,
                resolved=False,
                data={"executed": False, "attempt": existing.model_dump(mode="json")},
                started_at=started,
                completed_at=utc_now(),
            )

        # --- 1. Approval must exist and bind to these exact terms -----------
        blocked = self._verify_approval(
            approval, invoice_number, payee, amount, currency, findings
        )
        if blocked:
            return self._blocked(findings, blocked, started)

        # --- 2. Never pay an invoice that already settled -------------------
        blocked = self._verify_invoice_state(invoice_number, findings)
        if blocked:
            return self._blocked(findings, blocked, started)

        # --- 3. The bank's own request must exist ---------------------------
        request = self.accounts.request_for_case(case)
        if request is None:
            if case.owner_account_id:
                findings.append(
                    Finding(
                        code="no_bank_request",
                        label="No payment request at the bank",
                        status="failed",
                        detail=(
                            "No simulated payment request exists for this invoice, so the bank has "
                            "nothing to process. A customer without a simulated account must be "
                            "onboarded to the bank first."
                        ),
                    )
                )
                return self._blocked(
                    findings, "Payment not executed: there is no bank payment request.", started
                )
            # A case with no owning account predates accounts (or is driven
            # directly, as the unit tests do). It settles through the legacy
            # ledger path so old records keep working; real API cases always
            # have an owner and go through the account ledger.
            return self._execute_legacy(
                case, approval, invoice_number, payee, amount, currency,
                simulate, failure_reason, failure_code, findings, started, key,
            )

        # --- 4. Force a demo outcome, if one was asked for -------------------
        forced = (simulate or "").upper()
        if forced in FORCED_OUTCOMES or forced in {"PARTIAL", "DUPLICATE_SETTLEMENT"}:
            request = self._force_outcome(
                request, forced, failure_reason=failure_reason, failure_code=failure_code
            )

        # --- 5. The bank applies its own rules ------------------------------
        settlement = self.accounts.submit(
            request["payment_id"], actor_id="payment_simulator", actor_role="SYSTEM"
        )
        attempt = self._attempt_for(case, request["payment_id"], settlement, request, key)
        self.store.claim(attempt)
        self.store.update(attempt)
        findings.append(
            Finding(
                code="bank_instruction",
                label=(
                    "Instruction settled by the Mock Bank"
                    if settlement.settled
                    else "Instruction refused by the Mock Bank"
                    if settlement.failed
                    else "Instruction not settled"
                ),
                status="passed" if settlement.settled else "failed" if settlement.failed else "unverified",
                detail=(
                    f"The bank processed request {request['payment_id']} under "
                    f"{settlement.settlement_reference or 'an unsettled status'}."
                    + (
                        f" It refused with {settlement.failure_code}: {settlement.failure_reason}"
                        if settlement.failed
                        else ""
                    )
                ),
            )
        )
        self.store.update(attempt)

        instruction = {
            "outcome": "FAILED" if settlement.failed else "SUCCESS" if settlement.settled else "PENDING",
            "payment_reference": settlement.settlement_reference,
            "payment_id": request["payment_id"],
            "failure_code": settlement.failure_code,
            "failure_reason": settlement.failure_reason,
            "simulated": True,
        }
        summary = (
            f"The Mock Bank settled {amount} {currency} under "
            f"{settlement.settlement_reference}. Agent 2 must verify it."
            if settlement.settled
            else f"The Mock Bank refused the instruction: {settlement.failure_reason}"
            if settlement.failed
            else "The Mock Bank has not settled this instruction; it must be investigated, not retried."
        )
        return AgentReport(
            agent=AgentId.payment_simulator,
            agent_name="Payment Simulator",
            summary=summary,
            findings=findings,
            requires_human=bool(settlement.failed or settlement.uncertain),
            blocking=bool(settlement.failed),
            resolved=False,
            data={
                "executed": True,
                "attempt": attempt.model_dump(mode="json"),
                "idempotency_key": key,
                "payment_request_id": request["payment_id"],
                "bank_instruction": instruction,
                "awaiting_verification": bool(settlement.settled),
                "bank_failed": bool(settlement.failed),
                "bank_uncertain": bool(settlement.uncertain or not settlement.settled),
            },
            started_at=started,
            completed_at=utc_now(),
        )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _execute_legacy(
        self, case, approval, invoice_number, payee, amount, currency,
        simulate, failure_reason, failure_code, findings, started, key,
    ) -> AgentReport:
        """Settle through the legacy instruction ledger (no owning account).

        Kept for cases that predate the account ledger and for direct
        orchestrator use. It still requires the approval, still claims a UNIQUE
        idempotency key and still never credits a refused instruction.
        """

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
                agent=AgentId.payment_simulator,
                agent_name="Payment Simulator",
                summary="Payment not executed: a concurrent request already claimed it.",
                findings=findings,
                blocking=False,
                resolved=False,
                data={"executed": False, "attempt": claim.attempt.model_dump(mode="json")},
                started_at=started,
                completed_at=utc_now(),
            )
        attempt = claim.attempt

        instruction = self.ledger.submit_instruction(
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
        scenario = instruction["outcome"]
        attempt.provider_reference = instruction["payment_reference"]
        attempt.provider_status = {
            "SUCCESS": "SETTLED",
            "PARTIAL": "PARTIAL",
            "FAILED": "FAILED",
            "PENDING": "PENDING",
            "MISSING_SETTLEMENT": "MISSING_SETTLEMENT",
            "DUPLICATE_SETTLEMENT": "SETTLED",
        }.get(scenario, "PENDING")
        if scenario == "FAILED":
            attempt.status = "FAILED"
            attempt.failure_reason = (
                failure_reason or instruction.get("failure_reason")
                or "The Mock Bank rejected the instruction."
            )
            attempt.failure_code = instruction.get("failure_code") or "PAYMENT_REJECTED"
        else:
            # Settlement is not success: Agent 2 still has to verify it.
            attempt.status = "PENDING"
        self.store.update(attempt)

        failed = attempt.status == "FAILED"
        summary = {
            "FAILED": "Payment instruction rejected by the Mock Bank.",
            "PENDING": "Payment instruction submitted; the bank has not settled it.",
            "MISSING_SETTLEMENT": "Payment instruction accepted but never settled.",
        }.get(scenario, "Payment instruction submitted; awaiting bank verification.")
        return AgentReport(
            agent=AgentId.payment_simulator,
            agent_name="Payment Simulator",
            summary=summary,
            findings=findings
            + [
                Finding(
                    code="legacy_instruction",
                    label="Settled through the bank instruction ledger",
                    status="info" if not failed else "failed",
                    detail=(
                        f"No simulated account is linked to this case, so the bank posted the "
                        f"instruction directly under {instruction['payment_reference']}."
                    ),
                )
            ],
            blocking=failed,
            resolved=False,
            data={
                "executed": True,
                "attempt": attempt.model_dump(mode="json"),
                "idempotency_key": key,
                "bank_instruction": instruction,
                "awaiting_verification": not failed,
                "bank_failed": failed,
                "bank_uncertain": scenario in {"PENDING", "MISSING_SETTLEMENT"},
            },
            started_at=started,
            completed_at=utc_now(),
        )

    def _force_outcome(
        self, request: dict, scenario: str, *, failure_reason: str | None, failure_code: str | None
    ) -> dict:
        """Prepare the bank request so a demo can show a specific outcome.

        PARTIAL is expressed as a smaller settled amount on the same request, so
        the bank still posts both sides and the ledger stays balanced.
        """

        if scenario == "PARTIAL":
            from .mock_bank_accounts import to_minor

            try:
                minor = to_minor(request["amount"]) // 2
            except (ValueError, KeyError):
                return request
            conn = self.accounts.store._connect()
            try:
                conn.execute(
                    "UPDATE payment_requests SET amount_minor = ? WHERE payment_id = ?",
                    (max(1, minor), request["payment_id"]),
                )
            finally:
                conn.close()
            return self.accounts.store.payment_request(request["payment_id"]) or request

        if scenario == "DUPLICATE_SETTLEMENT":
            # The bank settles once; the duplicate scenario is demonstrated at
            # the ledger level rather than by paying twice here.
            return request

        status, default_code = FORCED_OUTCOMES[scenario]
        reason = failure_reason or (
            f"Demo outcome: the bank {status.lower()} this instruction."
        )
        code = (failure_code or default_code)
        if status == "FAILED":
            # Record the refusal through the bank, so the failure ledger row and
            # audit entry exist exactly as a real refusal would leave them.
            self.accounts.store.fail_payment(
                request["payment_id"],
                code=code,
                reason=reason,
                actor_id="payment_simulator",
                actor_role="SYSTEM",
            )
            return self.accounts.store.payment_request(request["payment_id"]) or request
        conn = self.accounts.store._connect()
        try:
            conn.execute(
                """
                UPDATE payment_requests
                SET status = ?, failure_code = ?, failure_reason = ?
                WHERE payment_id = ?
                """,
                (status, code, reason, request["payment_id"]),
            )
        finally:
            conn.close()
        return self.accounts.store.payment_request(request["payment_id"]) or request

    def _attempt_for(self, case: WorkflowCase, payment_id: str, settlement: Settlement, request: dict, key: str) -> PaymentAttempt:
        attempt = self.store.get(payment_id) or PaymentAttempt(
            attempt_id=payment_id,
            idempotency_key=key,
            approval_id=case.approval.approval_id if case.approval else None,
            invoice_number=request.get("invoice_id"),
            payee=request.get("payee"),
            amount=Decimal(str(request.get("amount") or "0")),
            currency=request.get("currency"),
            case_id=case.case_id,
        )
        attempt.idempotency_key = key
        attempt.provider_reference = settlement.settlement_reference
        attempt.approval_id = case.approval.approval_id if case.approval else attempt.approval_id
        if settlement.settled:
            # The bank settled. The simulator still does not call it a success:
            # Agent 2 must reconcile the bank's entries first.
            attempt.status = "PENDING"
            attempt.provider_status = "SETTLED"
            attempt.settlement_reference = settlement.settlement_reference
            attempt.settled_at = utc_now()
            attempt.verified = False
            attempt.failure_code = None
            attempt.failure_reason = None
        elif settlement.failed:
            attempt.status = "FAILED"
            attempt.provider_status = "FAILED"
            attempt.failure_code = settlement.failure_code
            attempt.failure_reason = settlement.failure_reason
        else:
            attempt.status = "PENDING"
            attempt.provider_status = "UNKNOWN"
            attempt.failure_code = settlement.failure_code or "BANK_TIMEOUT"
            attempt.failure_reason = settlement.failure_reason
        return attempt

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
