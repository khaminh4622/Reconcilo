"""Developer-only Mock Bank failure simulator.

Creates realistic synthetic failed payment attempts so Agent 4 can be tested
against each reason code. This is a *developer tool*: it is disabled unless a
developer key is configured, exactly like the statement importer.

Every simulated failure obeys the same rules as a real one:

* it never credits the receiver, so the settled balance is unchanged;
* it is idempotent on its key, so repeating the request creates nothing new;
* it is marked ``simulated`` and its origin is recorded.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .mock_bank import FAILURE_CODES, MockBank
from .schemas import PaymentAttempt, utc_now

# Scenarios the simulator can produce, mapped to the bank code they carry.
SIMULATABLE = {
    "INSUFFICIENT_FUNDS": "INSUFFICIENT_FUNDS",
    "INVALID_ACCOUNT": "INVALID_ACCOUNT",
    "PAYMENT_REJECTED": "PAYMENT_REJECTED",
    "BANK_TIMEOUT": "BANK_TIMEOUT",
    "ACCOUNT_CLOSED": "ACCOUNT_CLOSED",
    "LIMIT_EXCEEDED": "LIMIT_EXCEEDED",
}


class FailureSimulationError(ValueError):
    """The simulation could not be created."""


class FailureSimulator:
    """Creates a synthetic failed payment attempt and bank record."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.bank = MockBank(self.path)

    def simulate(
        self,
        *,
        invoice_number: str,
        amount: Decimal,
        currency: str = "USD",
        payee: str | None = None,
        customer_name: str | None = None,
        failure_code: str | None = None,
        scenario: str | None = None,
        case_id: str | None = None,
    ) -> dict:
        code = (scenario or failure_code or "").upper()
        if code not in SIMULATABLE:
            raise FailureSimulationError(
                f"Unknown failure scenario {code!r}. Choose one of: {', '.join(sorted(SIMULATABLE))}."
            )
        if invoice_number is None or not str(invoice_number).strip():
            raise FailureSimulationError("An invoice number is required.")
        if amount is None or Decimal(str(amount)) <= 0:
            raise FailureSimulationError("A positive amount is required.")

        invoice = str(invoice_number).strip()
        amount = Decimal(str(amount))
        # Idempotent: the same invoice + code + amount yields one simulation.
        key = hashlib.sha256(
            f"simulated-failure|{invoice.casefold()}|{code}|{amount:.2f}".encode("utf-8")
        ).hexdigest()[:32]

        existing = self.bank.instruction(key)
        if existing is not None:
            return {
                "created": False,
                "already_existed": True,
                "invoice_number": invoice,
                "failure_code": code,
                "payment_reference": existing.get("payment_reference"),
                "instruction": existing,
                "settled_balance": self._balances(),
            }

        instruction = self.bank.submit_instruction(
            idempotency_key=key,
            amount=amount,
            currency=currency.upper(),
            payee=payee or "Simulated Payee Ltd",
            invoice_number=invoice,
            scenario="FAILED",
            customer_name=customer_name or "Simulated Customer LLC",
            expected_amount=amount,
            failure_code=code,
        )

        # Record a matching attempt so Agent 4 sees the same picture it would
        # for a real failure.
        attempt = PaymentAttempt(
            idempotency_key=key,
            invoice_number=invoice,
            payee=payee or "Simulated Payee Ltd",
            amount=amount,
            currency=currency.upper(),
            case_id=case_id,
            status="FAILED",
            provider_status="FAILED",
            provider_reference=instruction["payment_reference"],
            failure_code=code,
            failure_reason=FAILURE_CODES.get(code, ""),
            attempted_at=utc_now(),
        )
        self._save_attempt(attempt)

        row = next(
            (r for r in self.bank.ledger(payment_reference=instruction["payment_reference"])),
            None,
        )
        return {
            "created": True,
            "already_existed": False,
            "invoice_number": invoice,
            "failure_code": code,
            "failure_reason": FAILURE_CODES.get(code, ""),
            "payment_reference": instruction["payment_reference"],
            "attempt_id": attempt.attempt_id,
            "bank_record": row,
            "settled_balance": self._balances(),
            "simulated": True,
        }

    # ------------------------------------------------------------------
    def _save_attempt(self, attempt: PaymentAttempt) -> None:
        from .payment_store import PaymentRecordStore

        store = PaymentRecordStore(self.path)
        claim = store.claim(attempt)
        if not claim.created:
            store.update(attempt)

    def _balances(self) -> dict[str, str]:
        return {k: f"{v:.2f}" for k, v in self.bank.balances().items()}


def failure_scenarios() -> list[dict]:
    return [
        {"code": code, "reason": FAILURE_CODES[code]}
        for code in sorted(SIMULATABLE)
    ]
