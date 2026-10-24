"""The Mock Bank: an independent simulated financial system.

This module is the *bank's* side of GhostLedger, not the payment agent's.

When an approved synthetic payment is processed, the payment simulator submits an
instruction here and the bank creates **its own** settlement record. Agent 5
never writes bank rows, so reconciliation is a genuine check rather than the
agent confirming its own work.

Every scenario produces a distinct, auditable bank outcome:

    SUCCESS              one settled credit for the full amount
    PARTIAL              one settled credit for less than the expected amount
    FAILED               a failed instruction; the receiver is not credited
    PENDING              an instruction accepted but not yet settled
    MISSING_SETTLEMENT   the instruction is accepted but never settles
    DUPLICATE_SETTLEMENT two distinct settlements for one payable (test anomaly)

All balances are fictional. No real money exists or moves.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import uuid4


class BankOutcome(str, Enum):
    """What the bank does with an instruction."""

    settled = "SETTLED"
    pending = "PENDING"
    failed = "FAILED"
    missing = "MISSING_SETTLEMENT"


class BankStatus(str, Enum):
    """Status of a ledger row."""

    pending = "PENDING"
    settled = "SETTLED"
    failed = "FAILED"
    partial = "PARTIAL"


SCENARIOS = (
    "SUCCESS",
    "PARTIAL",
    "FAILED",
    "PENDING",
    "MISSING_SETTLEMENT",
    "DUPLICATE_SETTLEMENT",
)

# Scenarios that post no settled credit.
NO_CREDIT_SCENARIOS = {"FAILED", "PENDING", "MISSING_SETTLEMENT"}

# The bank reports *why* an instruction failed. Agent 4 reads these codes and
# must never invent one that the bank did not send.
FAILURE_CODES = {
    "INSUFFICIENT_FUNDS": "The sender account did not hold sufficient cleared funds.",
    "INVALID_ACCOUNT": "The recipient account details were rejected as invalid.",
    "PAYMENT_REJECTED": "The receiving bank rejected the transaction.",
    "BANK_TIMEOUT": "The bank did not respond within the settlement window.",
    "ACCOUNT_CLOSED": "The recipient account is closed.",
    "LIMIT_EXCEEDED": "The transaction exceeds the sender's payment limit.",
    # Retained for older rows written before PAYMENT_REJECTED existed.
    "REJECTED": "The receiving bank rejected the transaction.",
}
DEFAULT_FAILURE_CODE = "PAYMENT_REJECTED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _stable(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# Simulated accounts.
SENDER_ACCOUNT = "ACC-GHOSTLEDGER-OPERATING"
RECEIVER_PREFIX = "ACC-VENDOR-"


def receiver_account_for(vendor: str | None) -> str:
    digest = _stable("vendor-account", (vendor or "unknown").casefold())[:8].upper()
    return f"{RECEIVER_PREFIX}{digest}"


@dataclass
class BankEntry:
    """One row in the bank's ledger."""

    entry_id: str
    payment_reference: str | None
    amount: Decimal
    currency: str
    counterparty: str | None
    direction: str
    status: str
    value_date: str
    description: str | None = None
    instruction_id: str | None = None
    raw: dict = field(default_factory=dict)


class MockBank:
    """An independent simulated bank with its own ledger."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mock_bank_ledger (
                    entry_id TEXT PRIMARY KEY,
                    payment_reference TEXT,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    counterparty TEXT,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    description TEXT,
                    instruction_id TEXT,
                    entry_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mock_bank_instructions (
                    instruction_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payment_reference TEXT,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    payee TEXT,
                    outcome TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    instruction_json TEXT NOT NULL
                )
                """
            )
            # Additive enrichment columns (safe on an existing database).
            present = {row[1] for row in conn.execute("PRAGMA table_info(mock_bank_ledger)")}
            for name, ddl in (
                ("invoice_number", "TEXT"),
                ("customer_name", "TEXT"),
                ("vendor_name", "TEXT"),
                ("expected_amount", "TEXT"),
                ("received_amount", "TEXT"),
                ("sender_account", "TEXT"),
                ("receiver_account", "TEXT"),
                ("transaction_date", "TEXT"),
                ("settlement_date", "TEXT"),
                ("scenario", "TEXT"),
                ("is_anomaly", "INTEGER NOT NULL DEFAULT 0"),
                ("failure_code", "TEXT"),
                ("failure_reason", "TEXT"),
                ("audit_timestamp", "TEXT"),
            ):
                if name not in present:
                    conn.execute(f"ALTER TABLE mock_bank_ledger ADD COLUMN {name} {ddl}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_mock_bank_ref ON mock_bank_ledger(payment_reference)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_mock_bank_invoice ON mock_bank_ledger(invoice_number)"
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def issue_reference(self, idempotency_key: str) -> str:
        """The reference the bank assigns to a settlement."""

        return f"MB-{_stable('bank-reference', idempotency_key)[:12].upper()}"

    def _entry_id(self, reference: str, suffix: str = "settled") -> str:
        return f"BK-{_stable(reference, suffix)[:14].upper()}"

    # ------------------------------------------------------------------
    def submit_instruction(
        self,
        *,
        idempotency_key: str,
        amount: Decimal,
        currency: str,
        payee: str | None,
        invoice_number: str | None,
        scenario: str | None = None,
        customer_name: str | None = None,
        expected_amount: Decimal | None = None,
        partial_amount: Decimal | None = None,
        failure_code: str | None = None,
    ) -> dict:
        """Process a payment instruction and create the bank's own record.

        Idempotent on ``idempotency_key``: submitting the same instruction twice
        never creates a second settlement.
        """

        existing = self.instruction(idempotency_key)
        if existing is not None:
            return existing

        chosen = (scenario or "").upper() or "SUCCESS"
        if chosen not in SCENARIOS:
            chosen = "SUCCESS"

        instruction_id = str(uuid4())
        reference = self.issue_reference(idempotency_key)
        accepted_at = _now()
        transaction_date = _today()
        expected = expected_amount if expected_amount is not None else amount
        sender = SENDER_ACCOUNT
        receiver = receiver_account_for(payee)

        base = {
            "invoice_number": invoice_number,
            "customer_name": customer_name,
            "vendor_name": payee,
            "expected_amount": f"{expected:.2f}",
            "sender_account": sender,
            "receiver_account": receiver,
            "transaction_date": transaction_date,
            "scenario": chosen,
            "audit_timestamp": accepted_at,
        }

        def post(
            status: BankStatus,
            value: Decimal,
            suffix: str,
            *,
            settled: bool,
            anomaly: bool = False,
            failure_code: str | None = None,
            failure_reason: str | None = None,
        ):
            self._post(
                {
                    "entry_id": self._entry_id(reference, suffix),
                    "payment_reference": reference,
                    "amount": f"{value:.2f}",
                    "currency": currency,
                    "counterparty": payee,
                    "direction": "credit",
                    "status": status.value,
                    "value_date": transaction_date,
                    "description": f"{chosen} settlement for {invoice_number or 'invoice'}",
                    "instruction_id": instruction_id,
                    "invoice_number": invoice_number,
                    "customer_name": customer_name,
                    "vendor_name": payee,
                    "expected_amount": f"{expected:.2f}",
                    "received_amount": f"{value:.2f}" if settled else None,
                    "sender_account": sender,
                    "receiver_account": receiver,
                    "transaction_date": transaction_date,
                    "settlement_date": _today() if settled else None,
                    "scenario": chosen,
                    "is_anomaly": 1 if anomaly else 0,
                    "failure_code": failure_code,
                    "failure_reason": failure_reason,
                    "audit_timestamp": _now(),
                }
            )

        # A FAILED instruction always carries a reason code from the bank.
        resolved_code: str | None = None
        if chosen == "FAILED":
            candidate = (failure_code or DEFAULT_FAILURE_CODE).upper()
            resolved_code = candidate if candidate in FAILURE_CODES else DEFAULT_FAILURE_CODE

        if chosen == "SUCCESS":
            post(BankStatus.settled, expected, "settled", settled=True)
        elif chosen == "PARTIAL":
            value = partial_amount if partial_amount is not None else (expected / 2).quantize(Decimal("0.01"))
            if value > expected:
                value = expected
            post(BankStatus.partial, value, "partial", settled=True)
        elif chosen == "FAILED":
            # A failed instruction never credits the receiver.
            post(
                BankStatus.failed,
                expected,
                "failed",
                settled=False,
                failure_code=resolved_code,
                failure_reason=FAILURE_CODES.get(resolved_code, ""),
            )
        elif chosen == "PENDING":
            post(BankStatus.pending, expected, "pending", settled=False)
        elif chosen == "MISSING_SETTLEMENT":
            # The instruction is accepted but the bank posts no settlement.
            pass
        elif chosen == "DUPLICATE_SETTLEMENT":
            # Two genuinely distinct settlements for one payable. This is a test
            # anomaly and is flagged as such.
            post(BankStatus.settled, expected, "settled-1", settled=True, anomaly=True)
            post(BankStatus.settled, expected, "settled-2", settled=True, anomaly=True)

        payload = {
            "instruction_id": instruction_id,
            "idempotency_key": idempotency_key,
            "payment_reference": reference,
            "amount": f"{amount:.2f}",
            "currency": currency,
            "payee": payee,
            "invoice_number": invoice_number,
            "outcome": chosen,
            "accepted_at": accepted_at,
            "settled": chosen not in NO_CREDIT_SCENARIOS,
            "failure_code": resolved_code,
            "failure_reason": FAILURE_CODES.get(resolved_code, "") if resolved_code else None,
            "simulated": True,
            **{k: v for k, v in base.items() if k in {"expected_amount", "sender_account", "receiver_account", "transaction_date"}},
        }
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO mock_bank_instructions
                    (instruction_id, idempotency_key, payment_reference, amount, currency,
                     payee, outcome, accepted_at, instruction_json)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    instruction_id, idempotency_key, reference, f"{amount:.2f}",
                    currency, payee, chosen, accepted_at, json.dumps(payload),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return payload

    # ------------------------------------------------------------------
    def _post(self, row: dict) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO mock_bank_ledger (
                    entry_id, payment_reference, amount, currency, counterparty, direction,
                    status, value_date, description, instruction_id, entry_json,
                    invoice_number, customer_name, vendor_name, expected_amount,
                    received_amount, sender_account, receiver_account, transaction_date,
                    settlement_date, scenario, is_anomaly, failure_code, failure_reason,
                    audit_timestamp
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["entry_id"], row["payment_reference"], row["amount"], row["currency"],
                    row["counterparty"], row["direction"], row["status"], row["value_date"],
                    row["description"], row["instruction_id"], json.dumps({"simulated": True}),
                    row["invoice_number"], row["customer_name"], row["vendor_name"],
                    row["expected_amount"], row["received_amount"], row["sender_account"],
                    row["receiver_account"], row["transaction_date"], row["settlement_date"],
                    row["scenario"], row["is_anomaly"], row["failure_code"],
                    row["failure_reason"], row["audit_timestamp"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read-only access (Agent 2 uses only these)
    # ------------------------------------------------------------------
    def instruction(self, idempotency_key: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT instruction_json FROM mock_bank_instructions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return json.loads(row["instruction_json"]) if row else None
        finally:
            conn.close()

    def ledger(
        self,
        *,
        payment_reference: str | None = None,
        invoice_number: str | None = None,
    ) -> list[dict]:
        conn = self._connect()
        try:
            query = "SELECT * FROM mock_bank_ledger"
            clauses: list[str] = []
            params: list = []
            if payment_reference:
                clauses.append("payment_reference = ? COLLATE NOCASE")
                params.append(payment_reference)
            if invoice_number:
                clauses.append("invoice_number = ? COLLATE NOCASE")
                params.append(invoice_number)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY created_at, entry_id"
            return [dict(row) for row in conn.execute(query, params)]
        finally:
            conn.close()

    def credits_for(self, payment_reference: str | None, invoice_number: str | None = None) -> list[dict]:
        rows = self.ledger(payment_reference=payment_reference, invoice_number=invoice_number)
        return [row for row in rows if row["direction"] == "credit"]

    def settle(self, payment_reference: str) -> dict | None:
        """Complete a pending entry."""

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT entry_id FROM mock_bank_ledger WHERE payment_reference = ? "
                "AND status = 'PENDING' LIMIT 1",
                (payment_reference,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE mock_bank_ledger SET status='SETTLED', received_amount=amount, "
                "settlement_date=?, audit_timestamp=? WHERE entry_id = ?",
                (_today(), _now(), row["entry_id"]),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM mock_bank_ledger WHERE entry_id = ?", (row["entry_id"],)
            ).fetchone()
            return dict(updated) if updated else None
        finally:
            conn.close()

    def entry(self, entry_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM mock_bank_ledger WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def history_for_reference(self, payment_reference: str) -> list[dict]:
        """Complete settlement history for one payment reference."""

        return self.ledger(payment_reference=payment_reference)

    def balances(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for row in self.ledger():
            if (row["status"] or "").upper() != "SETTLED":
                continue
            currency = row["currency"]
            totals[currency] = totals.get(currency, Decimal("0")) + Decimal(
                row["received_amount"] or row["amount"]
            )
        return totals

    def count(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM mock_bank_ledger").fetchone()[0]
        finally:
            conn.close()
