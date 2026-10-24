"""Fictional bank transactions for Agent 2 to reconcile against.

All values are invented for development. Nothing here represents a real account.
"""

from __future__ import annotations

from decimal import Decimal

from .schemas import SyntheticTransaction


def synthetic_bank_seed() -> list[SyntheticTransaction]:
    return [
        # A settled payment matching the Blue Harbor dispute fixture.
        SyntheticTransaction(
            reference="TRF-7741290",
            amount=Decimal("410.40"),
            currency="USD",
            direction="credit",
            counterparty="Avery Nguyen",
            status="settled",
            value_date="2026-03-20",
        ),
        # Two settled payments for the duplicate-payment fixture.
        SyntheticTransaction(
            reference="PMT-778812",
            amount=Decimal("1944.00"),
            currency="USD",
            direction="credit",
            counterparty="Blue Harbor Technologies Inc.",
            status="settled",
            value_date="2026-02-12",
        ),
        SyntheticTransaction(
            reference="PMT-779330",
            amount=Decimal("1944.00"),
            currency="USD",
            direction="credit",
            counterparty="Blue Harbor Technologies Inc.",
            status="settled",
            value_date="2026-02-19",
        ),
        # A payment that reached the bank but has not settled yet.
        SyntheticTransaction(
            reference="TRF-990215",
            amount=Decimal("612.40"),
            currency="USD",
            direction="credit",
            counterparty="Marguerite Oyelaran",
            status="pending",
            value_date="2026-09-18",
        ),
        # A returned payment: credited then reversed.
        SyntheticTransaction(
            reference="TRF-551230",
            amount=Decimal("780.00"),
            currency="GBP",
            direction="credit",
            counterparty="Selby & Crane Accounts Desk",
            status="returned",
            value_date="2026-08-02",
        ),
        # An unrelated transfer, present to prove matching does not over-accept.
        SyntheticTransaction(
            reference="TRF-100004",
            amount=Decimal("250.00"),
            currency="USD",
            direction="credit",
            counterparty="Kestrel Logistics",
            status="settled",
            value_date="2026-06-11",
        ),
    ]
