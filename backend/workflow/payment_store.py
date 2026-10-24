"""Concurrency-safe payment execution records.

Agent 5 must never create two transfers for one approved obligation, even when
two requests arrive at the same instant. Application-level checks alone cannot
guarantee that: two processes can both read "no attempt exists" before either
writes.

This module therefore claims the attempt inside a single write transaction, and
the ``idempotency_key`` column is UNIQUE. Whichever request loses the race
receives the winner's record instead of creating a second payment.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .schemas import PaymentAttempt


class AttemptClaim:
    """Outcome of trying to claim the right to execute a payment."""

    def __init__(self, attempt: PaymentAttempt, created: bool):
        self.attempt = attempt
        self.created = created

    @property
    def already_existed(self) -> bool:
        return not self.created


class PaymentRecordStore:
    """Atomic storage for payment attempts."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    approval_id TEXT,
                    invoice_number TEXT,
                    payee TEXT,
                    amount TEXT,
                    currency TEXT,
                    status TEXT NOT NULL,
                    failure_reason TEXT,
                    transaction_id TEXT,
                    attempted_at TEXT NOT NULL,
                    attempt_json TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_attempts_key ON payment_attempts(idempotency_key)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_attempts_invoice ON payment_attempts(invoice_number, status)"
            )
            # Columns Agent 2 queries directly. Kept here so any database that
            # has payment attempts is queryable without a separate migration.
            present = {row[1] for row in conn.execute("PRAGMA table_info(payment_attempts)")}
            for name, ddl in (
                ("provider_reference", "TEXT"),
                ("settlement_reference", "TEXT"),
                ("provider_status", "TEXT"),
                ("verified", "INTEGER NOT NULL DEFAULT 0"),
                ("settled_at", "TEXT"),
            ):
                if name not in present:
                    conn.execute(f"ALTER TABLE payment_attempts ADD COLUMN {name} {ddl}")
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def claim(self, attempt: PaymentAttempt) -> AttemptClaim:
        """Atomically reserve the right to execute this payment.

        Returns ``created=True`` for the request that won the race. Every other
        request gets ``created=False`` and the winner's row, so it must not
        execute.
        """

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                present = {
                    row[1] for row in conn.execute("PRAGMA table_info(payment_attempts)")
                }
                base_cols = [
                    "attempt_id", "case_id", "idempotency_key", "approval_id",
                    "invoice_number", "payee", "amount", "currency", "status",
                    "failure_reason", "transaction_id", "attempted_at", "attempt_json",
                ]
                base_vals = [
                    attempt.attempt_id,
                    attempt.case_id or "",
                    attempt.idempotency_key,
                    attempt.approval_id,
                    attempt.invoice_number,
                    attempt.payee,
                    str(attempt.amount) if attempt.amount is not None else None,
                    attempt.currency,
                    attempt.status,
                    attempt.failure_reason,
                    attempt.transaction_id,
                    attempt.attempted_at,
                    attempt.model_dump_json(),
                ]
                promoted = {
                    "provider_reference": attempt.provider_reference,
                    "settlement_reference": attempt.settlement_reference,
                    "provider_status": attempt.provider_status,
                    "verified": 1 if attempt.verified else 0,
                    "settled_at": attempt.settled_at,
                }
                for name, value in promoted.items():
                    if name in present:
                        base_cols.append(name)
                        base_vals.append(value)

                placeholders = ", ".join("?" for _ in base_cols)
                cursor = conn.execute(
                    f"INSERT INTO payment_attempts ({', '.join(base_cols)}) "
                    f"VALUES ({placeholders}) ON CONFLICT(idempotency_key) DO NOTHING",
                    tuple(base_vals),
                )
                created = cursor.rowcount == 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

            row = conn.execute(
                "SELECT attempt_json FROM payment_attempts WHERE idempotency_key = ?",
                (attempt.idempotency_key,),
            ).fetchone()
        finally:
            conn.close()
        stored = PaymentAttempt.model_validate_json(row["attempt_json"]) if row else attempt
        return AttemptClaim(stored, created)

    def update(self, attempt: PaymentAttempt) -> None:
        """Persist a status change (initiated -> processing -> settled)."""

        conn = self._connect()
        try:
            # Keep both the JSON payload and the promoted columns in step, so
            # Agent 2 can query references without parsing JSON.
            present = {row[1] for row in conn.execute("PRAGMA table_info(payment_attempts)")}
            promoted = ""
            extra: list = []
            if "provider_reference" in present:
                promoted += ", provider_reference = ?"
                extra.append(attempt.provider_reference)
            if "settlement_reference" in present:
                promoted += ", settlement_reference = ?"
                extra.append(attempt.settlement_reference)
            if "provider_status" in present:
                promoted += ", provider_status = ?"
                extra.append(attempt.provider_status)
            if "verified" in present:
                promoted += ", verified = ?"
                extra.append(1 if attempt.verified else 0)
            if "settled_at" in present:
                promoted += ", settled_at = ?"
                extra.append(attempt.settled_at)

            conn.execute(
                f"""
                UPDATE payment_attempts
                SET status = ?, failure_reason = ?, transaction_id = ?, attempt_json = ?{promoted}
                WHERE attempt_id = ?
                """,
                (
                    attempt.status,
                    attempt.failure_reason,
                    attempt.transaction_id,
                    attempt.model_dump_json(),
                    *extra,
                    attempt.attempt_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, attempt_id: str) -> PaymentAttempt | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT attempt_json FROM payment_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        finally:
            conn.close()
        return PaymentAttempt.model_validate_json(row["attempt_json"]) if row else None

    def find_by_key(self, idempotency_key: str) -> PaymentAttempt | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT attempt_json FROM payment_attempts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        finally:
            conn.close()
        return PaymentAttempt.model_validate_json(row["attempt_json"]) if row else None

    def list_for_invoice(self, invoice_number: str | None) -> list[PaymentAttempt]:
        if not invoice_number:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT attempt_json FROM payment_attempts
                WHERE invoice_number = ? COLLATE NOCASE
                ORDER BY attempted_at, rowid
                """,
                (invoice_number,),
            ).fetchall()
        finally:
            conn.close()
        return [PaymentAttempt.model_validate_json(row["attempt_json"]) for row in rows]

    def list_all(self) -> list[PaymentAttempt]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT attempt_json FROM payment_attempts ORDER BY attempted_at, rowid"
            ).fetchall()
        finally:
            conn.close()
        return [PaymentAttempt.model_validate_json(row["attempt_json"]) for row in rows]


class ReceiptStore:
    """Persistent payment receipts."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    invoice_number TEXT,
                    provider_reference TEXT,
                    settlement_reference TEXT UNIQUE,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    receipt_json TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        return connection

    def save(self, receipt: Any) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO payment_receipts (
                    receipt_id, case_id, invoice_number, provider_reference,
                    settlement_reference, outcome, receipt_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    receipt.case_id,
                    receipt.invoice_number,
                    receipt.provider_reference,
                    receipt.settlement_reference,
                    receipt.outcome,
                    receipt.model_dump_json(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_for_case(self, case_id: str) -> Any | None:
        from .schemas import PaymentReceipt

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT receipt_json FROM payment_receipts WHERE case_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        finally:
            conn.close()
        return PaymentReceipt.model_validate_json(row["receipt_json"]) if row else None

    def get_for_invoice(self, invoice_number: str | None) -> Any | None:
        from .schemas import PaymentReceipt

        if not invoice_number:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT receipt_json FROM payment_receipts WHERE invoice_number = ? COLLATE NOCASE "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (invoice_number,),
            ).fetchone()
        finally:
            conn.close()
        return PaymentReceipt.model_validate_json(row["receipt_json"]) if row else None
