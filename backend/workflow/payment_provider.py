"""Synthetic payment provider.

A stand-in for a real payment rail. It never touches a bank and never moves
money. Its job is to make payment outcomes *deterministic and inspectable*, so
Agent 5 can verify a settlement instead of assuming one.

The provider is deliberately separate from Agent 5: the agent asks the provider
to submit a transfer, then must ask again for the result. That separation is
what lets Agent 5 distinguish "initiated" from "settled".

Outcomes are chosen by a configurable policy, never by the LLM:

    SYNTHETIC_PAYMENT_OUTCOME = SUCCESS | FAILED | PENDING | UNKNOWN
    SYNTHETIC_PAYMENT_DELAY_SECONDS = 0

A per-invoice override is supported through ``scene`` so tests and demos can
force a specific result without changing global configuration.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class ProviderStatus(str, Enum):
    """What the provider reports about a transfer.

    ``initiated`` and ``processing`` are deliberately distinct from
    ``settled``: a successful API call is not proof that money moved.
    """

    initiated = "INITIATED"
    processing = "PROCESSING"
    settled = "SETTLED"
    failed = "FAILED"
    unknown = "UNKNOWN"


SETTLED = {ProviderStatus.settled}
UNRESOLVED = {ProviderStatus.initiated, ProviderStatus.processing, ProviderStatus.unknown}


@dataclass
class ProviderTransaction:
    """One transfer record held by the provider."""

    provider_reference: str
    status: ProviderStatus
    amount: Decimal
    currency: str
    payee: str | None
    invoice_number: str | None
    created_at: float
    settled_at: float | None = None
    failure_reason: str | None = None
    failure_code: str | None = None
    raw_response: dict = field(default_factory=dict)

    @property
    def is_settled(self) -> bool:
        return self.status == ProviderStatus.settled

    @property
    def is_unresolved(self) -> bool:
        return self.status in UNRESOLVED


FAILURE_CODES = {
    "insufficient funds": "INSUFFICIENT_FUNDS",
    "invalid account": "INVALID_ACCOUNT",
    "rejected": "REJECTED",
    "timeout": "TIMEOUT",
    "network": "NETWORK_ERROR",
}


class SyntheticPaymentProvider:
    """An in-memory, deterministic payment provider."""

    def __init__(self, default_outcome: str | None = None, settle_after_seconds: float | None = None):
        self._transactions: dict[str, ProviderTransaction] = {}
        configured = (default_outcome or os.getenv("SYNTHETIC_PAYMENT_OUTCOME") or "SUCCESS").upper()
        if configured not in {"SUCCESS", "FAILED", "PENDING", "UNKNOWN"}:
            configured = "SUCCESS"
        self.default_outcome = configured
        if settle_after_seconds is None:
            try:
                settle_after_seconds = float(os.getenv("SYNTHETIC_PAYMENT_SETTLE_SECONDS", "0"))
            except ValueError:
                settle_after_seconds = 0.0
        self.settle_after_seconds = settle_after_seconds

    # ------------------------------------------------------------------
    def _reference(self, idempotency_key: str) -> str:
        digest = hashlib.sha256(f"provider|{idempotency_key}".encode("utf-8")).hexdigest()
        return f"SP-{digest[:12].upper()}"

    def _outcome_for(self, scenario: str | None) -> str:
        outcome = (scenario or self.default_outcome).upper()
        return outcome if outcome in {"SUCCESS", "FAILED", "PENDING", "UNKNOWN"} else self.default_outcome

    # ------------------------------------------------------------------
    def submit(
        self,
        *,
        idempotency_key: str,
        amount: Decimal,
        currency: str,
        payee: str | None,
        invoice_number: str | None,
        scenario: str | None = None,
        failure_reason: str | None = None,
    ) -> ProviderTransaction:
        """Submit a transfer. Idempotent on ``idempotency_key``.

        The provider itself refuses to create a second transfer for the same
        key, which is the last line of defence against double payment.
        """

        existing = self._transactions.get(idempotency_key)
        if existing is not None:
            return existing

        outcome = self._outcome_for(scenario)
        reference = self._reference(idempotency_key)
        now = time.time()
        reason = failure_reason
        code = None

        if outcome == "SUCCESS":
            status = ProviderStatus.settled
            settled_at = now
        elif outcome == "FAILED":
            status = ProviderStatus.failed
            settled_at = None
            reason = reason or "The synthetic payment was rejected by the payment network."
            code = next(
                (v for k, v in FAILURE_CODES.items() if k in reason.casefold()),
                "REJECTED",
            )
        elif outcome == "PENDING":
            status = ProviderStatus.processing
            settled_at = None
        else:
            status = ProviderStatus.unknown
            settled_at = None
            reason = reason or "The provider did not return a definite result."

        transaction = ProviderTransaction(
            provider_reference=reference,
            status=status,
            amount=amount,
            currency=currency,
            payee=payee,
            invoice_number=invoice_number,
            created_at=now,
            settled_at=settled_at,
            failure_reason=reason if status == ProviderStatus.failed else None,
            failure_code=code,
            raw_response={
                "provider_reference": reference,
                "status": status.value,
                "amount": f"{amount:.2f}",
                "currency": currency,
                "payee": payee,
                "invoice_number": invoice_number,
                "synthetic": True,
            },
        )
        self._transactions[idempotency_key] = transaction
        return transaction

    def get(self, idempotency_key: str) -> ProviderTransaction | None:
        """Inspect an existing transfer without creating a new one."""

        return self._transactions.get(idempotency_key)

    def poll(self, idempotency_key: str) -> ProviderTransaction | None:
        """Re-check a transfer's status.

        A ``processing`` transfer settles (or fails) once the configured settle
        delay has elapsed. Until then it stays processing, so Agent 5 keeps
        reporting it as unresolved rather than retrying.
        """

        transaction = self._transactions.get(idempotency_key)
        if transaction is None:
            return None
        if transaction.status == ProviderStatus.processing and self.settle_after_seconds >= 0:
            elapsed = time.time() - transaction.created_at
            if self.settle_after_seconds == 0:
                return transaction
            if elapsed >= self.settle_after_seconds:
                transaction.status = ProviderStatus.settled
                transaction.settled_at = time.time()
                transaction.raw_response["status"] = transaction.status.value
        return transaction

    # Test helpers ------------------------------------------------------
    def resolve(self, idempotency_key: str, outcome: str) -> ProviderTransaction | None:
        """Force a processing transfer to a final outcome (tests only)."""

        transaction = self._transactions.get(idempotency_key)
        if transaction is None:
            return None
        if outcome.upper() == "SUCCESS":
            transaction.status = ProviderStatus.settled
            transaction.settled_at = time.time()
        else:
            transaction.status = ProviderStatus.failed
            transaction.failure_reason = "Forced failure in test."
            transaction.failure_code = "REJECTED"
        transaction.raw_response["status"] = transaction.status.value
        return transaction

    def clear(self) -> None:
        self._transactions.clear()


# A single provider instance is shared by the application so a retry of the same
# idempotency key sees the original transfer.
_PROVIDER: SyntheticPaymentProvider | None = None


def get_provider() -> SyntheticPaymentProvider:
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = SyntheticPaymentProvider()
    return _PROVIDER


def reset_provider(provider: SyntheticPaymentProvider | None = None) -> SyntheticPaymentProvider:
    global _PROVIDER
    _PROVIDER = provider or SyntheticPaymentProvider()
    return _PROVIDER
