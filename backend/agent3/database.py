"""SQLite persistence for document results and fictional customer references."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from .schemas import AnalysisResult, CustomerProfile, CustomerType, DocumentType, DuplicateMatch


class DocumentRepository:
    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS document_records (
                    record_id TEXT PRIMARY KEY,
                    file_hash TEXT NOT NULL,
                    document_identifier TEXT,
                    document_type TEXT NOT NULL,
                    vendor_name TEXT,
                    customer_name TEXT,
                    total_amount TEXT,
                    issue_date TEXT,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_records_hash ON document_records(file_hash)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_records_metadata ON document_records(document_identifier, vendor_name, total_amount, issue_date)"
            )
            # This non-destructive migration preserves prior fictional customer
            # records. Agent 3 uses them only as reference data for matching.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL UNIQUE,
                    customer_name TEXT NOT NULL,
                    customer_type TEXT NOT NULL,
                    company_name TEXT,
                    email TEXT NOT NULL COLLATE NOCASE,
                    phone TEXT,
                    address TEXT,
                    onboarding_status TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_customers_email ON customers(email COLLATE NOCASE)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_customers_name_company ON customers(customer_name, company_name)")
            conn.commit()
        finally:
            conn.close()

    def find_duplicates(self, result: AnalysisResult, file_hash: str) -> list[DuplicateMatch]:
        matches: dict[str, DuplicateMatch] = {}
        conn = self._connect()
        try:
            for row in conn.execute("SELECT record_id FROM document_records WHERE file_hash = ?", (file_hash,)):
                matches[row["record_id"]] = DuplicateMatch(record_id=row["record_id"], reason="file_hash")
            vendor = result.vendor.vendor_name
            customer = result.customer.customer_name
            amount = result.financial_information.total_amount
            if result.document_id and amount is not None and result.issue_date and (vendor or customer):
                rows = conn.execute(
                    """
                    SELECT record_id FROM document_records
                    WHERE document_identifier = ?
                      AND total_amount = ?
                      AND issue_date = ?
                      AND (vendor_name = ? OR customer_name = ?)
                    """,
                    (result.document_id, str(amount), result.issue_date, vendor, customer),
                ).fetchall()
                for row in rows:
                    matches.setdefault(row["record_id"], DuplicateMatch(record_id=row["record_id"], reason="metadata"))
        finally:
            conn.close()
        return list(matches.values())

    def save(self, result: AnalysisResult, file_hash: str) -> str:
        record_id = str(uuid4())
        result.record_id = record_id
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO document_records (
                    record_id, file_hash, document_identifier, document_type, vendor_name,
                    customer_name, total_amount, issue_date, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    file_hash,
                    result.document_id,
                    result.document_type.value,
                    result.vendor.vendor_name,
                    result.customer.customer_name,
                    str(result.financial_information.total_amount) if result.financial_information.total_amount is not None else None,
                    result.issue_date,
                    json.dumps(result.model_dump(mode="json")),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return record_id

    def get(self, identifier: str) -> AnalysisResult | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT result_json FROM document_records WHERE record_id = ? OR document_identifier = ? ORDER BY created_at DESC LIMIT 1",
                (identifier, identifier),
            ).fetchone()
        finally:
            conn.close()
        return AnalysisResult.model_validate_json(row["result_json"]) if row else None

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> CustomerProfile:
        return CustomerProfile(
            id=row["id"],
            customer_id=row["customer_id"],
            customer_name=row["customer_name"],
            customer_type=CustomerType(row["customer_type"]),
            company_name=row["company_name"],
            email=row["email"],
            phone=row["phone"],
            address=row["address"],
            onboarding_status=row["onboarding_status"],
            verification_status=row["verification_status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_customers(self) -> list[CustomerProfile]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM customers ORDER BY created_at, customer_id").fetchall()
            return [self._profile_from_row(row) for row in rows]
        finally:
            conn.close()

    def get_customer(self, customer_id: str) -> CustomerProfile | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM customers WHERE customer_id = ?", (customer_id,)).fetchone()
            return self._profile_from_row(row) if row else None
        finally:
            conn.close()

    def seed_customer_references(self, profiles: Iterable[dict[str, object]]) -> int:
        """Insert fictional reference records, skipping any already present.

        The operation is idempotent and never overwrites an existing record, so
        prior development data survives a restart. Returns how many rows were
        added.
        """

        added = 0
        conn = self._connect()
        try:
            for profile in profiles:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO customers (
                        id, customer_id, customer_name, customer_type, company_name,
                        email, phone, address, onboarding_status, verification_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        str(profile["customer_id"]),
                        str(profile["customer_name"]),
                        str(profile.get("customer_type") or CustomerType.individual.value),
                        profile.get("company_name"),
                        str(profile["email"]),
                        profile.get("phone"),
                        profile.get("address"),
                        str(profile.get("onboarding_status") or "reference"),
                        str(profile.get("verification_status") or "reference"),
                    ),
                )
                added += cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return added

    def count_customers(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0])
        finally:
            conn.close()

    def find_customer_matches(
        self, customer_name: str | None, company_name: str | None, email: str | None
    ) -> list[CustomerProfile]:
        """Return only deterministic exact-reference candidates, never fuzzy matches."""

        clauses: list[str] = []
        params: list[str] = []
        if email and email.strip():
            clauses.append("email = ? COLLATE NOCASE")
            params.append(email.strip())
        if customer_name and customer_name.strip() and company_name and company_name.strip():
            clauses.append("(customer_name = ? COLLATE NOCASE AND company_name = ? COLLATE NOCASE)")
            params.extend([customer_name.strip(), company_name.strip()])
        if not clauses:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(f"SELECT * FROM customers WHERE {' OR '.join(clauses)}", params).fetchall()
            return [self._profile_from_row(row) for row in rows]
        finally:
            conn.close()

    def add_customer_reference(
        self,
        customer_id: str,
        customer_name: str,
        email: str,
        *,
        customer_type: CustomerType = CustomerType.individual,
        company_name: str | None = None,
    ) -> CustomerProfile:
        """Add a fictional reference record for development or controlled imports.

        This method does not perform onboarding and is intentionally not exposed
        as a public Agent 3 workflow endpoint.
        """

        internal_id = str(uuid4())
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO customers (
                    id, customer_id, customer_name, customer_type, company_name, email,
                    onboarding_status, verification_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    internal_id,
                    customer_id,
                    customer_name,
                    customer_type.value,
                    company_name,
                    email,
                    "reference",
                    "reference",
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM customers WHERE id = ?", (internal_id,)).fetchone()
            assert row is not None
            return self._profile_from_row(row)
        finally:
            conn.close()
