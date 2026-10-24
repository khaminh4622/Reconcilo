"""Payable obligations and invoice submissions.

Agent 1 needs to know whether a re-submitted invoice has already been paid, and
a repeated upload must not create a second payable obligation. This module adds
the two tables that make that possible, on top of the existing database.

Design:

* ``payable_obligations`` - one row per invoice that GhostLedger may pay.
  ``obligation_key`` is UNIQUE, which is what prevents a repeated upload from
  creating a second obligation, even under concurrent requests.
* ``invoice_submissions`` - one row per uploaded document, linked to the
  obligation it belongs to. Duplicate submissions are recorded here without
  adding a new obligation.

The unique key is derived from the invoice number, vendor and currency (not the
amount, so a corrected amount on the same invoice still reuses the obligation).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .agent1_history import normalize_invoice_number, normalize_name


def obligation_key(invoice_number: str | None, vendor: str | None, currency: str | None) -> str:
    """Stable key identifying the payable obligation for an invoice.

    Derived from the invoice identity rather than a random id, so the same
    invoice always maps to the same obligation.
    """

    material = "|".join(
        [
            normalize_invoice_number(invoice_number),
            normalize_name(vendor),
            (currency or "").strip().upper(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class ObligationRepository:
    """Read/write access to invoice submissions and payable obligations."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        # WAL keeps concurrent submissions from blocking each other.
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payable_obligations (
                    obligation_id TEXT PRIMARY KEY,
                    obligation_key TEXT NOT NULL UNIQUE,
                    invoice_number TEXT,
                    vendor TEXT,
                    customer TEXT,
                    currency TEXT,
                    amount TEXT,
                    state TEXT NOT NULL DEFAULT 'OPEN',
                    paid_amount TEXT,
                    first_case_id TEXT,
                    original_record_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_obligation_invoice ON payable_obligations(invoice_number)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS invoice_submissions (
                    submission_id TEXT PRIMARY KEY,
                    obligation_id TEXT,
                    case_id TEXT,
                    filename TEXT,
                    invoice_number TEXT,
                    vendor TEXT,
                    customer TEXT,
                    amount TEXT,
                    currency TEXT,
                    is_duplicate INTEGER NOT NULL DEFAULT 0,
                    original_invoice TEXT,
                    payment_status TEXT,
                    payment_reference TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (obligation_id) REFERENCES payable_obligations(obligation_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_submission_obligation "
                "ON invoice_submissions(obligation_id)"
            )
            conn.commit()
        finally:
            conn.close()

    # -- obligations -------------------------------------------------------
    def get_obligation(self, key: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM payable_obligations WHERE obligation_key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def get_obligation_by_id(self, obligation_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM payable_obligations WHERE obligation_id = ?", (obligation_id,)
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def ensure_obligation(
        self,
        *,
        invoice_number: str | None,
        vendor: str | None,
        customer: str | None,
        currency: str | None,
        amount: Any,
        case_id: str | None = None,
        original_record_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Return the obligation for this invoice, creating it only once.

        Returns ``(obligation, created)``. ``created`` is False when an
        obligation already existed, which is how a duplicate upload reuses the
        original payable obligation instead of creating a second one.
        """

        key = obligation_key(invoice_number, vendor, currency)
        existing = self.get_obligation(key)
        if existing is not None:
            return existing, False

        import uuid

        obligation_id = str(uuid.uuid4())
        conn = self._connect()
        try:
            # INSERT OR IGNORE plus the UNIQUE constraint makes this safe when
            # two requests race for the same invoice: one wins, the other reads.
            conn.execute(
                """
                INSERT OR IGNORE INTO payable_obligations (
                    obligation_id, obligation_key, invoice_number, vendor, customer,
                    currency, amount, state, first_case_id, original_record_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    obligation_id,
                    key,
                    invoice_number,
                    vendor,
                    customer,
                    currency,
                    str(amount) if amount is not None else None,
                    case_id,
                    original_record_id,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM payable_obligations WHERE obligation_key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
        created = bool(row) and row["obligation_id"] == obligation_id
        return dict(row), created

    def set_obligation_state(
        self,
        obligation_id: str,
        state: str,
        paid_amount: Any = None,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE payable_obligations
                SET state = ?, paid_amount = COALESCE(?, paid_amount),
                    updated_at = CURRENT_TIMESTAMP
                WHERE obligation_id = ?
                """,
                (state, str(paid_amount) if paid_amount is not None else None, obligation_id),
            )
            conn.commit()
        finally:
            conn.close()

    # -- submissions -------------------------------------------------------
    def record_submission(
        self,
        *,
        submission_id: str,
        obligation_id: str | None,
        case_id: str | None,
        filename: str | None,
        invoice_number: str | None,
        vendor: str | None,
        customer: str | None,
        amount: Any,
        currency: str | None,
        is_duplicate: bool,
        original_invoice: dict | None = None,
        payment_status: str | None = None,
        payment_reference: str | None = None,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO invoice_submissions (
                    submission_id, obligation_id, case_id, filename, invoice_number, vendor,
                    customer, amount, currency, is_duplicate, original_invoice,
                    payment_status, payment_reference
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    obligation_id,
                    case_id,
                    filename,
                    invoice_number,
                    vendor,
                    customer,
                    str(amount) if amount is not None else None,
                    currency,
                    1 if is_duplicate else 0,
                    json.dumps(original_invoice) if original_invoice else None,
                    payment_status,
                    payment_reference,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def list_submissions(self, obligation_id: str | None = None) -> list[dict[str, Any]]:
        """Submissions in chronological order (oldest first).

        Callers reason about which submission was the original, so the order is
        oldest-first in both branches.
        """
        conn = self._connect()
        try:
            if obligation_id:
                # rowid breaks ties: several submissions can share a timestamp.
                rows = conn.execute(
                    "SELECT * FROM invoice_submissions WHERE obligation_id = ? "
                    "ORDER BY created_at, rowid",
                    (obligation_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM invoice_submissions ORDER BY created_at, rowid"
                ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def count_submissions(self, invoice_number: str | None) -> int:
        if not invoice_number:
            return 0
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM invoice_submissions
                WHERE invoice_number = ? COLLATE NOCASE
                """,
                (invoice_number,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
