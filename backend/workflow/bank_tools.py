"""Restricted bank tools for the AI agents.

The agents never touch the bank database directly. They call one of the five
functions below, and every call is checked against the *case* the agent is
working on before a single row is read:

    get_payment_status(payment_id, case_context)
    get_bank_transaction(payment_id, case_context)
    verify_payment_settlement(payment_id, case_context)
    get_reconciliation_evidence(payment_id, case_context)
    get_payment_exception_details(payment_id, case_context)

``case_context`` is a :class:`CaseContext`: it names the agent, the case the
agent is allowed to look at, and the account that owns that case. A call is
refused when:

* the agent is not one of the agents permitted to hold bank tools;
* the case is not the one the tools were opened for;
* the payment request belongs to a different case, customer or invoice;
* the requesting account does not own the case.

The result is always *case-scoped*: no balances of unrelated accounts, no other
customers' transactions, no raw ledger dump. The tools are read-only. They have
no method that changes a balance, approves a payment or creates a transaction,
so a model driving them cannot invent a settlement or move money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .mock_bank_accounts import (
    REQUEST_SETTLED,
    MockBankStore,
    from_minor,
)

# Which agents may hold bank tools at all. This is the least-privilege baseline:
# an unknown agent name gets nothing.
BANK_TOOL_AGENTS: dict[str, set[str]] = {
    # Reconciliation: needs settlement evidence for its own case.
    "agent_2": {
        "get_payment_status",
        "get_bank_transaction",
        "verify_payment_settlement",
        "get_reconciliation_evidence",
    },
    # Exceptions / support: needs the failure detail, the status of the prior
    # attempt, and the *payment history for this case's invoice*. The history
    # tool is case-scoped: it never returns a different customer's rows.
    "agent_4": {
        "get_payment_status",
        "get_bank_transaction",
        "get_payment_exception_details",
        "get_case_payment_history",
        "verify_payment_settlement",
    },
    # Final confirmation: reads the verified outcome and reports it.
    "agent_5": {
        "get_payment_status",
        "verify_payment_settlement",
    },
    # The duplicate investigator reads status only; it never inspects ledger rows.
    "agent_1": {
        "get_payment_status",
    },
}

TOOL_NAMES = tuple(
    sorted({name for tools in BANK_TOOL_AGENTS.values() for name in tools})
)


class BankToolError(PermissionError):
    """A bank tool call was refused. ``reason`` is a stable machine code."""

    def __init__(self, message: str, reason: str = "forbidden"):
        super().__init__(message)
        self.reason = reason


@dataclass
class CaseContext:
    """The scope every bank tool call is checked against."""

    agent_id: str
    case_id: str
    customer_id: str | None
    invoice_id: str | None = None
    customer_name: str | None = None
    # Used only to narrow a history lookup to the exact payable identity. They
    # are not part of any authorisation decision.
    payee: str | None = None
    currency: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "case_id": self.case_id,
            "customer_id": self.customer_id,
            "invoice_id": self.invoice_id,
            "payee": self.payee,
            "currency": self.currency,
        }


def _clean(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


class AgentBankTools:
    """The five restricted bank tools, bound to one case at a time."""

    def __init__(self, database_path):
        self.store = MockBankStore(database_path)

    # ------------------------------------------------------------------
    # Scope enforcement
    # ------------------------------------------------------------------
    def _authorise(self, tool: str, case_context: CaseContext | None) -> CaseContext:
        if not isinstance(case_context, CaseContext):
            raise BankToolError(
                "A bank tool needs a case context; none was supplied.", "missing_case_context"
            )
        if case_context.agent_id not in BANK_TOOL_AGENTS:
            raise BankToolError(
                f"{case_context.agent_id or 'an unidentified agent'} has no bank access.",
                "unknown_agent",
            )
        if tool not in BANK_TOOL_AGENTS[case_context.agent_id]:
            raise BankToolError(
                f"{case_context.agent_id} is not permitted to call {tool}.",
                "tool_not_permitted",
            )
        if not case_context.case_id:
            raise BankToolError("The case context names no case.", "missing_case_context")
        return case_context

    def _request(self, payment_id: str, context: CaseContext) -> dict:
        """Load a payment request and prove it belongs to the agent's case."""

        request = self.store.payment_request(payment_id)
        if request is None:
            raise BankToolError(
                f"No payment request {payment_id} exists.", "not_found"
            )
        if request.get("case_id") and request["case_id"] != context.case_id:
            # Deliberately the same wording as a miss: an agent must not learn
            # whether another case's payment exists.
            raise BankToolError(
                f"No payment request {payment_id} exists in this case.", "not_found"
            )
        if context.customer_id and request.get("customer_id") and (
            request["customer_id"] != context.customer_id
        ):
            raise BankToolError(
                f"No payment request {payment_id} exists in this case.", "customer_mismatch"
            )
        if context.invoice_id and request.get("invoice_id") and (
            _clean(request["invoice_id"]) != _clean(context.invoice_id)
        ):
            raise BankToolError(
                "That payment is not for the invoice in this case.", "invoice_mismatch"
            )
        return request

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    def get_payment_status(self, payment_id: str, case_context: CaseContext) -> dict:
        """Status of this case's payment and of its prior attempts."""

        context = self._authorise("get_payment_status", case_context)
        return self._status(payment_id, context)

    def _status(self, payment_id: str, context: CaseContext) -> dict:
        request = self._request(payment_id, context)
        history = self.store.payment_requests(
            case_id=context.case_id, limit=50
        )
        return {
            "tool": "get_payment_status",
            "simulated": True,
            "payment_id": request["payment_id"],
            "status": request["status"],
            "amount": request["amount"],
            "currency": request["currency"],
            "settlement_reference": request.get("settlement_reference"),
            "processed_at": request.get("processed_at"),
            "failure_code": request.get("failure_code"),
            # Prior attempts matter: a retry must check them first.
            "attempts_in_case": [
                {
                    "payment_id": row["payment_id"],
                    "status": row["status"],
                    "amount": row["amount"],
                    "created_at": row["created_at"],
                    "processed_at": row.get("processed_at"),
                }
                for row in history
            ],
        }

    def get_case_payment_history(self, case_context: CaseContext) -> dict:
        """Every Mock Bank request that belongs to this case's invoice.

        Discovery is deliberately narrow. It first reads requests linked to the
        case id. Only when that returns nothing does it look up the same
        customer, invoice, payee and currency, which covers a complaint case
        that was opened before its payment request was linked. Rows belonging to
        another customer are never returned, even if the invoice number matches.
        """

        context = self._authorise("get_case_payment_history", case_context)
        return self._history(context)

    def _history(self, context: CaseContext) -> dict:
        rows = self.store.payment_requests(case_id=context.case_id, limit=50)
        if not rows and context.invoice_id and context.customer_id:
            rows = self.store.payment_requests(
                customer_id=context.customer_id,
                invoice_id=context.invoice_id,
                payee=context.payee,
                currency=context.currency,
                limit=50,
            )

        safe: list[dict] = []
        for row in rows:
            if context.customer_id and row.get("customer_id") and (
                row["customer_id"] != context.customer_id
            ):
                continue
            if context.invoice_id and row.get("invoice_id") and (
                _clean(row["invoice_id"]) != _clean(context.invoice_id)
            ):
                continue
            safe.append(
                {
                    "payment_id": row["payment_id"],
                    "case_id": row.get("case_id"),
                    "invoice_number": row.get("invoice_id"),
                    "payee": row.get("payee"),
                    "amount": row.get("amount"),
                    "currency": row.get("currency"),
                    "status": row.get("status"),
                    "settlement_reference": row.get("settlement_reference"),
                    "failure_code": row.get("failure_code"),
                    "failure_reason": row.get("failure_reason"),
                    "created_at": row.get("created_at"),
                    "processed_at": row.get("processed_at"),
                }
            )
        # Most recent first, but stable for equal timestamps.
        safe.reverse()
        return {
            "tool": "get_case_payment_history",
            "simulated": True,
            "case_id": context.case_id,
            "invoice_number": context.invoice_id,
            "count": len(safe),
            "requests": safe,
        }

    def get_bank_transaction(self, payment_id: str, case_context: CaseContext) -> dict:
        """The bank's own ledger entries for this case's payment."""

        context = self._authorise("get_bank_transaction", case_context)
        return self._transaction(payment_id, context)

    def _transaction(self, payment_id: str, context: CaseContext) -> dict:
        request = self._request(payment_id, context)
        rows = self.store.ledger_rows_for_payment(request["payment_id"])
        return {
            "tool": "get_bank_transaction",
            "simulated": True,
            "payment_id": request["payment_id"],
            "status": request["status"],
            "settlement_reference": request.get("settlement_reference"),
            "entries": [
                {
                    "entry_id": row["entry_id"],
                    "direction": row["direction"],
                    "amount": row["amount"],
                    "currency": row["currency"],
                    "status": row["status"],
                    "value_date": row["value_date"],
                    "settlement_date": row.get("settlement_date"),
                    "account": row.get("sender_account") or row.get("receiver_account"),
                    "invoice_number": row.get("invoice_number"),
                    "description": row.get("description"),
                }
                for row in rows
            ],
            "entry_count": len(rows),
        }

    def verify_payment_settlement(self, payment_id: str, case_context: CaseContext) -> dict:
        """Deterministic check that both sides of the transfer settled.

        A settlement is verified only when the request is SETTLED *and* both a
        debit and a credit entry exist for it with equal amounts. A status flag
        alone is never enough.
        """

        context = self._authorise("verify_payment_settlement", case_context)
        return self._verify(payment_id, context)

    def _verify(self, payment_id: str, context: CaseContext) -> dict:
        request = self._request(payment_id, context)
        rows = self.store.ledger_rows_for_payment(request["payment_id"])
        debits = [r for r in rows if r["direction"] == "debit" and r["status"] == "SETTLED"]
        credits = [r for r in rows if r["direction"] == "credit" and r["status"] == "SETTLED"]
        amount_minor = int(request.get("amount_minor") or 0)
        amounts_agree = (
            len(debits) == 1
            and len(credits) == 1
            and int(Decimal(debits[0]["amount"]) * 100) == amount_minor
            and int(Decimal(credits[0]["amount"]) * 100) == amount_minor
        )
        verified = bool(
            request["status"] == REQUEST_SETTLED
            and len(debits) == 1
            and len(credits) == 1
            and amounts_agree
        )
        return {
            "tool": "verify_payment_settlement",
            "simulated": True,
            "payment_id": request["payment_id"],
            "verified": verified,
            "status": request["status"],
            "settlement_reference": request.get("settlement_reference"),
            "amount": request["amount"],
            "currency": request["currency"],
            "debit_entries": len(debits),
            "credit_entries": len(credits),
            "amounts_agree": amounts_agree,
            "reason": (
                "Both settlement entries are committed and equal the requested amount."
                if verified
                else "The bank has not posted a matching pair of settled entries for this payment."
            ),
        }

    def get_reconciliation_evidence(self, payment_id: str, case_context: CaseContext) -> dict:
        """Exactly the evidence Agent 2 needs, and nothing from other cases."""

        context = self._authorise("get_reconciliation_evidence", case_context)
        request = self._request(payment_id, context)
        verification = self._verify(payment_id, context)
        rows = self.store.ledger_rows_for_payment(request["payment_id"])
        source = self.store.account(request.get("source_account_id"))
        destination = self.store.account(request.get("destination_account_id"))
        return {
            "tool": "get_reconciliation_evidence",
            "simulated": True,
            "payment_id": request["payment_id"],
            "case_id": request["case_id"],
            "invoice_number": request.get("invoice_id"),
            "expected": {
                "amount": request["amount"],
                "currency": request["currency"],
                "reference": request.get("settlement_reference"),
                "payee": request.get("payee"),
            },
            "settled": {
                "verified": verification["verified"],
                "amount": from_minor(
                    sum(
                        int(Decimal(r["amount"]) * 100)
                        for r in rows
                        if r["direction"] == "credit" and r["status"] == "SETTLED"
                    )
                ),
                "currency": request["currency"],
                "reference": request.get("settlement_reference"),
            },
            "accounts": {
                # Masked numbers only: the agent can reconcile, not spy.
                "source": {
                    "account_id": source["account_id"],
                    "account_number_masked": source["account_number_masked"],
                    "owner_type": source["owner_type"],
                }
                if source
                else None,
                "destination": {
                    "account_id": destination["account_id"],
                    "account_number_masked": destination["account_number_masked"],
                    "owner_type": destination["owner_type"],
                }
                if destination
                else None,
            },
            "entries": [
                {
                    "entry_id": r["entry_id"],
                    "direction": r["direction"],
                    "amount": r["amount"],
                    "status": r["status"],
                    "value_date": r["value_date"],
                }
                for r in rows
            ],
            "scope": "This payment only. Other accounts and customers are not visible.",
        }

    def get_payment_exception_details(self, payment_id: str, case_context: CaseContext) -> dict:
        """Everything Agent 4 needs to diagnose this case's exception."""

        context = self._authorise("get_payment_exception_details", case_context)
        request = self._request(payment_id, context)
        rows = self.store.ledger_rows_for_payment(request["payment_id"])
        approvals = self.store.approvals_for(request["payment_id"])
        prior = [
            row
            for row in self.store.payment_requests(case_id=context.case_id, limit=50)
            if row["payment_id"] != request["payment_id"]
        ]
        return {
            "tool": "get_payment_exception_details",
            "simulated": True,
            "payment_id": request["payment_id"],
            "case_id": request["case_id"],
            "status": request["status"],
            # The bank's own code, or None. The agent must not invent one.
            "failure_code": request.get("failure_code"),
            "failure_reason": request.get("failure_reason"),
            "amount": request["amount"],
            "currency": request["currency"],
            "invoice_number": request.get("invoice_id"),
            "payee": request.get("payee"),
            "settlement_reference": request.get("settlement_reference"),
            "approved_by": request.get("approved_by"),
            "decisions": [
                {
                    "approval_id": row["approval_id"],
                    "decision": row["decision"],
                    "decided_at": row["decision_time"],
                    "reason": row.get("reason"),
                    "approver_id": row["approver_id"],
                }
                for row in approvals
            ],
            "ledger_entries": len(rows),
            "prior_attempts": [
                {
                    "payment_id": row["payment_id"],
                    "status": row["status"],
                    "failure_code": row.get("failure_code"),
                    "created_at": row["created_at"],
                }
                for row in prior
            ],
            # A retry needs a human to check the prior attempt first.
            "retry_blocked": True,
            "retry_requirement": (
                "Resolve this attempt and obtain fresh human approval before any retry. "
                "The bank tools cannot create a second payment."
            ),
        }

    # ------------------------------------------------------------------
    def describe(self) -> list[dict]:
        """The tool catalogue, per agent, for the UI and documentation."""

        return [
            {"agent": agent, "tools": sorted(tools)} for agent, tools in BANK_TOOL_AGENTS.items()
        ]
