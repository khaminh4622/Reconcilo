"""The Mock Bank's interface for the payment agents: execution, verification.

The bank lives in two layers:

    mock_bank_accounts.MockBankStore   accounts, payment_requests, transfers
    mock_bank.MockBank                 the settlement ledger and instruction log

``BankService`` is what the orchestrator's payment simulator and the agents use.
It has exactly two mutating entry points, and neither of them can be reached by
a model:

    submit(payment_id, actor)   the *bank* processes an APPROVED request under
                                its deterministic rules (funds, account status,
                                currency, reference). It either settles both
                                sides in one transaction or records a refusal.
    verify(payment_id, case)    a read-only settlement verification through the
                                restricted Agent 2 / Agent 5 bank tool.

Nothing here can create a transaction, approve a payment or change a balance on
the caller's instructions. "Uncertain" outcomes are preserved, never retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .bank_tools import AgentBankTools, BankToolError, CaseContext
from .mock_bank_accounts import (
    REQUEST_APPROVED,
    REQUEST_FAILED,
    REQUEST_PENDING,
    REQUEST_PROCESSING,
    REQUEST_REJECTED,
    REQUEST_SETTLED,
    REQUEST_UNCERTAIN,
    BankError,
    MockBankStore,
)

DISCLAIMER = "SIMULATED - NO REAL MONEY MOVED."


@dataclass
class Settlement:
    """What the bank did with one request."""

    status: str
    settled: bool
    already_settled: bool = False
    settlement_reference: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    debit_entry_id: str | None = None
    credit_entry_id: str | None = None
    uncertain: bool = False

    @property
    def failed(self) -> bool:
        return self.status in {REQUEST_FAILED, REQUEST_REJECTED}


class BankService:
    def __init__(self, database_path):
        self.store = MockBankStore(database_path)
        self.tools = AgentBankTools(self.store.path)

    # ------------------------------------------------------------------
    def payment_request(self, payment_id: str | None) -> dict | None:
        return self.store.payment_request(payment_id)

    def submit(self, payment_id: str, *, actor_id: str | None, actor_role: str) -> Settlement:
        """Have the bank process an APPROVED request.

        Refusals are decided by the bank's own rules and recorded with the
        bank's own code. A request that is not approved is refused before any
        balance is read, so processing is never a back door around approval.
        """

        request = self.store.payment_request(payment_id)
        if request is None:
            return Settlement(
                status=REQUEST_FAILED,
                settled=False,
                failure_code="UNKNOWN_ACCOUNT",
                failure_reason="That payment request does not exist.",
            )
        if request["status"] == REQUEST_SETTLED:
            return Settlement(
                status=REQUEST_SETTLED,
                settled=True,
                already_settled=True,
                settlement_reference=request.get("settlement_reference"),
            )
        if request["status"] in {REQUEST_UNCERTAIN, REQUEST_PROCESSING}:
            return Settlement(
                status=REQUEST_UNCERTAIN,
                settled=False,
                uncertain=True,
                failure_code="BANK_TIMEOUT",
                failure_reason=(
                    "The outcome of the existing attempt is unknown. It must be investigated; "
                    "a second payment must never be created automatically."
                ),
            )
        if request["status"] == REQUEST_FAILED:
            # A refusal the bank already recorded carries its own reason code.
            return Settlement(
                status=REQUEST_FAILED,
                settled=False,
                failure_code=request.get("failure_code") or "PAYMENT_REJECTED",
                failure_reason=request.get("failure_reason") or "The bank refused the instruction.",
            )
        if request["status"] == REQUEST_REJECTED:
            return Settlement(
                status=REQUEST_REJECTED,
                settled=False,
                failure_code="PAYMENT_REJECTED",
                failure_reason=request.get("failure_reason") or "The payment was rejected by the approver.",
            )
        if request["status"] != REQUEST_APPROVED:
            return Settlement(
                status=REQUEST_FAILED,
                settled=False,
                failure_code="APPROVAL_MISSING",
                failure_reason=(
                    f"The request is {request['status']}. Only an APPROVED request can be processed, "
                    "so no payment was attempted."
                ),
            )
        try:
            result = self.store.process_payment(
                payment_id,
                actor_id=actor_id,
                actor_role=actor_role,
                # The bank issues its own MB- reference; never the invoice id.
                payment_reference=None,
            )
        except BankError as exc:
            failed = self.store.fail_payment(
                payment_id,
                code=exc.code,
                reason=str(exc),
                actor_id=actor_id,
                actor_role=actor_role,
            )
            return Settlement(
                status=(failed or {}).get("status") or REQUEST_FAILED,
                settled=False,
                failure_code=exc.code,
                failure_reason=str(exc),
            )
        return Settlement(
            status=result["status"],
            settled=True,
            already_settled=bool(result.get("already_settled")),
            settlement_reference=result.get("settlement_reference"),
            debit_entry_id=result.get("debit_entry_id"),
            credit_entry_id=result.get("credit_entry_id"),
        )

    def mark_uncertain(self, payment_id: str, *, reason: str, actor_id: str | None, actor_role: str) -> Settlement:
        """Preserve a request whose outcome could not be established."""

        row = self.store.mark_uncertain(
            payment_id, reason=reason, actor_id=actor_id, actor_role=actor_role
        )
        return Settlement(
            status=(row or {}).get("status") or REQUEST_UNCERTAIN,
            settled=False,
            uncertain=True,
            failure_code="BANK_TIMEOUT",
            failure_reason=reason,
        )

    # ------------------------------------------------------------------
    def verify(self, payment_id: str, *, case_id: str, customer_id: str | None, invoice_id: str | None) -> dict:
        """Read-only settlement verification for Agent 2 / Agent 5."""

        context = CaseContext(
            agent_id="agent_2",
            case_id=case_id,
            customer_id=customer_id,
            invoice_id=invoice_id,
        )
        try:
            return self.tools.get_reconciliation_evidence(payment_id, context)
        except BankToolError as exc:
            return {
                "verified": False,
                "reason": str(exc),
                "scope_error": exc.reason,
            }

    def status(self, payment_id: str, *, case_id: str, customer_id: str | None, invoice_id: str | None) -> dict:
        context = CaseContext(
            agent_id="agent_5",
            case_id=case_id,
            customer_id=customer_id,
            invoice_id=invoice_id,
        )
        return self.tools.get_payment_status(payment_id, context)

    # ------------------------------------------------------------------
    def request_for_case(self, case) -> dict | None:
        """The bank request belonging to a case, if one exists."""

        payment_id = getattr(case, "payment_request_id", None)
        if payment_id:
            request = self.store.payment_request(payment_id)
            if request is not None:
                return request
        identity = {
            "customer_id": getattr(case, "owner_account_id", None),
            "payee": getattr(case, "vendor_name", None),
            "currency": getattr(case, "currency", None),
        }
        live = self.store.live_request_for_invoice(case.invoice_number, **identity)
        if live is not None:
            return live
        return self.store.settled_request_for_invoice(case.invoice_number, **identity)

    def to_attempt(self, request: dict) -> dict:
        """Map a bank request onto the case's payment-attempt view."""

        amount = Decimal(str(request.get("amount") or "0"))
        status = request.get("status")
        if status == REQUEST_SETTLED:
            attempt_status, provider_status = "SUCCESS", "SETTLED"
        elif status in {REQUEST_FAILED, REQUEST_REJECTED}:
            attempt_status, provider_status = "FAILED", "FAILED"
        elif status == REQUEST_UNCERTAIN:
            attempt_status, provider_status = "PENDING", "UNKNOWN"
        elif status == REQUEST_PENDING:
            attempt_status, provider_status = "PENDING", "AWAITING_APPROVAL"
        else:
            attempt_status, provider_status = "PENDING", "PROCESSING"
        return {
            "attempt_id": request["payment_id"],
            "idempotency_key": request["idempotency_key"],
            "invoice_number": request.get("invoice_id"),
            "payee": request.get("payee"),
            "amount": str(amount),
            "currency": request.get("currency"),
            "case_id": request.get("case_id"),
            "status": attempt_status,
            "provider_status": provider_status,
            "failure_code": request.get("failure_code"),
            "failure_reason": request.get("failure_reason"),
            "provider_reference": request.get("settlement_reference"),
            "settlement_reference": request.get("settlement_reference"),
            "settled_at": request.get("processed_at") if status == REQUEST_SETTLED else None,
            "verified": False,
            "attempted_at": request.get("created_at"),
        }
