"""Invoice history access for Agent 1.

Agent 1 compares a new invoice against invoices GhostLedger has already
recorded. That history lives in the pre-existing ``document_records`` table
(written by Agent 3), so no new invoice store is introduced - this module only
reads it and exposes the fields the matcher needs.

Two important rules:

* comparisons are scoped to the same customer/tenant, so two different
  customers with identical invoices are not treated as duplicates of each other;
* when the history is empty the caller is told so, rather than concluding that
  the invoice is unique.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path


# Legal-form suffixes that do not change vendor identity.
_COMPANY_SUFFIXES = (
    "incorporated", "corporation", "company", "limited", "holdings",
    "inc", "llc", "ltd", "plc", "gmbh", "pty", "corp", "co", "sa", "bv", "ag",
)
_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize_name(value: str | None) -> str:
    """Normalise a vendor or customer name for comparison.

    Lower-cases, strips punctuation and legal suffixes, so
    "Northstar Office Supplies LLC" and "NORTHSTAR OFFICE SUPPLIES, L.L.C."
    compare equal.
    """
    if not value:
        return ""
    # Remove punctuation entirely (not replace with space) so "L.L.C." becomes
    # "llc" rather than three tokens "l l c".
    text = _PUNCTUATION.sub("", value.casefold())
    tokens = _WHITESPACE.sub(" ", text).strip().split()
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_invoice_number(value: str | None) -> str:
    """Normalise an invoice number: case, spacing and separators do not matter.

    "INV-2026-1042", "inv 2026 1042" and "INV/2026/1042" all collapse to the
    same key, which catches "slightly different formatting" resubmissions.
    """
    if not value:
        return ""
    text = _PUNCTUATION.sub("", value.casefold())
    return _WHITESPACE.sub("", text)


@dataclass
class InvoiceHistoryRecord:
    """One previously recorded invoice."""

    record_id: str
    invoice_number: str | None = None
    vendor: str | None = None
    customer: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    issue_date: str | None = None
    description: str | None = None
    document_type: str | None = None
    normalized_vendor: str = ""
    normalized_customer: str = ""
    normalized_number: str = ""
    raw: dict = field(default_factory=dict)


def _extract_description(result_json: dict | None) -> str | None:
    """Pull a service description out of a stored Agent 3 result.

    Prefers line-item descriptions, then the problem description. Returns a
    single normalised string for comparison.
    """
    if not isinstance(result_json, dict):
        return None
    parts: list[str] = []
    for item in result_json.get("line_items") or []:
        if isinstance(item, dict) and item.get("description"):
            parts.append(str(item["description"]))
    vendor = (result_json.get("vendor") or {}).get("vendor_name")
    if vendor:
        parts.append(str(vendor))
    if not parts:
        problem = (result_json.get("detected_problem") or {}).get("description")
        if problem:
            parts.append(str(problem))
    text = " ".join(" ".join(parts).split())
    return text or None


def _to_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


class InvoiceHistoryRepository:
    """Read-only view of previously recorded invoices for duplicate comparison."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def available(self) -> bool:
        """True when a comparable invoice history exists at all."""

        return self.count() > 0

    def count(self) -> int:
        if not self.path.exists():
            return 0
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM document_records WHERE document_type = 'invoice'"
            ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def list_records(
        self,
        *,
        customer: str | None = None,
        exclude_record_id: str | None = None,
        limit: int = 500,
    ) -> list[InvoiceHistoryRecord]:
        """Return prior invoices, scoped to one customer when given.

        Scoping matters: the same amount billed to two different customers is
        not a duplicate. When ``customer`` is supplied, only that customer's
        invoices are returned.
        """

        if not self.path.exists():
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT record_id, document_identifier, vendor_name, customer_name,
                       total_amount, issue_date, document_type, result_json
                FROM document_records
                WHERE document_type = 'invoice'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

        wanted_customer = normalize_name(customer)
        records: list[InvoiceHistoryRecord] = []
        for row in rows:
            if exclude_record_id and row["record_id"] == exclude_record_id:
                continue
            try:
                parsed = json.loads(row["result_json"]) if row["result_json"] else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            currency = (
                parsed.get("currency")
                or (parsed.get("financial_information") or {}).get("currency")
            )
            record = InvoiceHistoryRecord(
                record_id=row["record_id"],
                invoice_number=row["document_identifier"],
                vendor=row["vendor_name"],
                customer=row["customer_name"],
                amount=_to_decimal(row["total_amount"]),
                currency=currency,
                issue_date=row["issue_date"],
                description=_extract_description(parsed),
                document_type=row["document_type"],
                normalized_vendor=normalize_name(row["vendor_name"]),
                normalized_customer=normalize_name(row["customer_name"]),
                normalized_number=normalize_invoice_number(row["document_identifier"]),
                raw=parsed,
            )
            # Tenant scoping: skip other customers' invoices entirely.
            if wanted_customer and record.normalized_customer and record.normalized_customer != wanted_customer:
                continue
            records.append(record)
        return records
