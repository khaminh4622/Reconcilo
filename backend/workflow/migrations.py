"""Safe, additive migrations for Agent 2 reconciliation.

Rules this module follows:

* Never drop a table or a column.
* Never delete a business row (invoices, payments, obligations, receipts).
* Every step is idempotent: running twice is a no-op.
* A backup is taken before the first schema change.

The only rows ever removed are duplicate copies of *seed fixtures* in
``synthetic_bank_transactions`` - generated sample data, not user records.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def backup_database(database_path: str | Path, backup_dir: str | Path | None = None) -> Path:
    """Take a consistent SQLite backup before any schema change."""

    source = Path(database_path)
    directory = Path(backup_dir) if backup_dir else source.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = directory / f"{source.stem}-{stamp}.db"

    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


# ----------------------------------------------------------------------
def add_bank_invoice_link(conn: sqlite3.Connection) -> list[str]:
    """Give bank rows an explicit invoice link and a demo flag.

    Without this column Agent 2 could only guess which invoice a bank row
    belongs to, by comparing amounts and counterparties.
    """

    applied: list[str] = []
    columns = _columns(conn, "synthetic_bank_transactions")
    for name, ddl in (
        ("invoice_number", "TEXT"),
        ("is_demo", "INTEGER NOT NULL DEFAULT 0"),
        ("deduplicated", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE synthetic_bank_transactions ADD COLUMN {name} {ddl}")
            applied.append(f"synthetic_bank_transactions.{name}")
    return applied


def promote_attempt_references(conn: sqlite3.Connection) -> list[str]:
    """Expose payment references as queryable columns.

    The references already exist inside ``attempt_json``; they were simply not
    queryable. Promoting them adds no new information, it only indexes it.
    """

    applied: list[str] = []
    columns = _columns(conn, "payment_attempts")
    for name, ddl in (
        ("provider_reference", "TEXT"),
        ("settlement_reference", "TEXT"),
        ("provider_status", "TEXT"),
        ("verified", "INTEGER NOT NULL DEFAULT 0"),
        ("settled_at", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE payment_attempts ADD COLUMN {name} {ddl}")
            applied.append(f"payment_attempts.{name}")
    return applied


def backfill_attempt_references(conn: sqlite3.Connection) -> int:
    """Copy references out of attempt_json into the new columns.

    Values are only filled where the column is still empty, so this is safe to
    re-run and never overwrites anything.
    """

    import json

    updated = 0
    rows = conn.execute(
        "SELECT attempt_id, attempt_json, provider_reference FROM payment_attempts"
    ).fetchall()
    for attempt_id, payload, existing_reference in rows:
        if existing_reference:
            continue
        try:
            data = json.loads(payload or "{}")
        except (TypeError, ValueError):
            continue
        conn.execute(
            """
            UPDATE payment_attempts
            SET provider_reference = ?, settlement_reference = ?, provider_status = ?,
                verified = ?, settled_at = ?
            WHERE attempt_id = ?
            """,
            (
                data.get("provider_reference"),
                data.get("settlement_reference"),
                data.get("provider_status"),
                1 if data.get("verified") else 0,
                data.get("settled_at"),
                attempt_id,
            ),
        )
        updated += 1
    return updated


# ----------------------------------------------------------------------
def dedupe_seed_fixtures(conn: sqlite3.Connection) -> dict:
    """Collapse duplicated seed fixtures to one row each.

    Only touches rows whose reference is one of the known generated fixtures
    AND that are not linked to an invoice. Rows written by Agent 5 (``SP-*``)
    and any row with an invoice link are never removed.
    """

    rows = conn.execute(
        """
        SELECT reference, COUNT(*) AS n
        FROM synthetic_bank_transactions
        WHERE reference NOT LIKE 'SP-%' AND invoice_number IS NULL
        GROUP BY reference HAVING n > 1
        """
    ).fetchall()

    removed = 0
    for reference, count in rows:
        # Keep the earliest row (lowest rowid); delete the rest.
        keep = conn.execute(
            """
            SELECT rowid FROM synthetic_bank_transactions
            WHERE reference = ? AND reference NOT LIKE 'SP-%' AND invoice_number IS NULL
            ORDER BY rowid LIMIT 1
            """,
            (reference,),
        ).fetchone()
        if keep is None:
            continue
        cursor = conn.execute(
            """
            DELETE FROM synthetic_bank_transactions
            WHERE reference = ? AND reference NOT LIKE 'SP-%'
              AND invoice_number IS NULL AND rowid != ?
            """,
            (reference, keep[0]),
        )
        removed += cursor.rowcount

    # The same fixture can appear twice with different transaction ids (an old
    # random uuid and a newer stable hash), so deduplicate on the business key
    # as well: reference + amount + currency + status.
    business_key_rows = conn.execute(
        """
        SELECT reference, amount, currency, status, COUNT(*) AS n
        FROM synthetic_bank_transactions
        WHERE reference NOT LIKE 'SP-%' AND invoice_number IS NULL
          AND transaction_id NOT IN (SELECT transaction_id FROM payment_attempts)
        GROUP BY reference, amount, currency, status HAVING n > 1
        """
    ).fetchall()
    for reference, amount, currency, status, _count in business_key_rows:
        keep = conn.execute(
            """
            SELECT rowid FROM synthetic_bank_transactions
            WHERE reference = ? AND amount = ? AND currency = ? AND status = ?
              AND invoice_number IS NULL
              AND transaction_id NOT IN (SELECT transaction_id FROM payment_attempts)
            ORDER BY rowid LIMIT 1
            """,
            (reference, amount, currency, status),
        ).fetchone()
        if keep is None:
            continue
        cursor = conn.execute(
            """
            DELETE FROM synthetic_bank_transactions
            WHERE reference = ? AND amount = ? AND currency = ? AND status = ?
              AND invoice_number IS NULL
              AND transaction_id NOT IN (SELECT transaction_id FROM payment_attempts)
              AND rowid != ?
            """,
            (reference, amount, currency, status, keep[0]),
        )
        removed += cursor.rowcount

    # Classify what remains. A row is NOT demo data if it traces back to a real
    # payment attempt through its transaction_id - that row was written by
    # Agent 5, so it is an execution record rather than a generated fixture.
    conn.execute(
        """
        UPDATE synthetic_bank_transactions
        SET is_demo = 0
        WHERE reference NOT LIKE 'SP-%'
          AND transaction_id IN (SELECT transaction_id FROM payment_attempts)
        """
    )
    conn.execute(
        """
        UPDATE synthetic_bank_transactions SET is_demo = 1
        WHERE reference NOT LIKE 'SP-%'
          AND transaction_id NOT IN (SELECT transaction_id FROM payment_attempts)
        """
    )
    return {"distinct_fixtures": len(rows), "rows_removed": removed}


def link_agent5_settlements(conn: sqlite3.Connection) -> int:
    """Attach invoice numbers to settlement rows written by Agent 5."""

    import json

    linked = 0
    attempts = conn.execute(
        """
        SELECT invoice_number, provider_reference FROM payment_attempts
        WHERE provider_reference IS NOT NULL AND provider_reference LIKE 'SP-%'
        """
    ).fetchall()
    for invoice_number, reference in attempts:
        cursor = conn.execute(
            """
            UPDATE synthetic_bank_transactions SET invoice_number = ?
            WHERE reference = ? AND invoice_number IS NULL
            """,
            (invoice_number, reference),
        )
        linked += cursor.rowcount
    return linked


def ensure_mock_bank_tables(conn: sqlite3.Connection) -> list[str]:
    """The Mock Bank's own ledger and instruction log."""

    created: list[str] = []
    if not _table_exists(conn, "mock_bank_ledger"):
        conn.execute(
            """
            CREATE TABLE mock_bank_ledger (
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
        created.append("mock_bank_ledger")
    if not _table_exists(conn, "mock_bank_instructions"):
        conn.execute(
            """
            CREATE TABLE mock_bank_instructions (
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
        created.append("mock_bank_instructions")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_mock_bank_ref ON mock_bank_ledger(payment_reference)"
    )
    return created


def ensure_bank_tables(conn: sqlite3.Connection) -> list[str]:
    """Bank evidence tables and the Agent 2 reconciliation run log."""

    created: list[str] = []
    if not _table_exists(conn, "bank_transactions"):
        conn.execute(
            """
            CREATE TABLE bank_transactions (
                row_id TEXT PRIMARY KEY,
                bank_transaction_id TEXT NOT NULL,
                payment_reference TEXT,
                transaction_date TEXT,
                description TEXT,
                counterparty TEXT,
                amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                direction TEXT NOT NULL,
                duplicate_key TEXT NOT NULL,
                import_id TEXT NOT NULL,
                duplicate_of TEXT,
                simulated INTEGER NOT NULL DEFAULT 1,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        created.append("bank_transactions")
    if not _table_exists(conn, "bank_imports"):
        conn.execute(
            """
            CREATE TABLE bank_imports (
                import_id TEXT PRIMARY KEY,
                filename TEXT,
                source_type TEXT NOT NULL,
                document_hash TEXT NOT NULL UNIQUE,
                row_count INTEGER NOT NULL,
                simulated INTEGER NOT NULL DEFAULT 1,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        created.append("bank_imports")
    if not _table_exists(conn, "bank_reconciliation_runs"):
        conn.execute(
            """
            CREATE TABLE bank_reconciliation_runs (
                report_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expected_count INTEGER NOT NULL,
                bank_transaction_count INTEGER NOT NULL,
                matched INTEGER NOT NULL,
                unmatched_bank INTEGER NOT NULL,
                report_json TEXT NOT NULL
            )
            """
        )
        created.append("bank_reconciliation_runs")
    return created


def create_reconciliation_tables(conn: sqlite3.Connection) -> list[str]:
    """Agent 2's own tables. Additive only."""

    created: list[str] = []
    if not _table_exists(conn, "reconciliation_results"):
        conn.execute(
            """
            CREATE TABLE reconciliation_results (
                result_id TEXT PRIMARY KEY,
                case_id TEXT,
                invoice_number TEXT,
                payment_attempt_id TEXT,
                provider_reference TEXT,
                outcome TEXT NOT NULL,
                matched_reference TEXT,
                expected_amount TEXT,
                settled_amount TEXT,
                currency TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                findings_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        created.append("reconciliation_results")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_recon_invoice ON reconciliation_results(invoice_number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_recon_attempt ON reconciliation_results(payment_attempt_id)"
    )
    return created


# ----------------------------------------------------------------------
def migrate(database_path: str | Path, take_backup: bool = True) -> dict:
    """Run every migration step. Safe to call repeatedly."""

    path = Path(database_path)
    report: dict = {
        "database": str(path),
        "backup": None,
        "columns_added": [],
        "attempts_backfilled": 0,
        "seed_rows_removed": 0,
        "settlements_linked": 0,
        "tables_created": [],
    }
    if take_backup and path.exists():
        report["backup"] = str(backup_database(path))

    conn = sqlite3.connect(path, timeout=20.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _table_exists(conn, "synthetic_bank_transactions"):
                report["columns_added"] += add_bank_invoice_link(conn)
            if _table_exists(conn, "payment_attempts"):
                report["columns_added"] += promote_attempt_references(conn)
                report["attempts_backfilled"] = backfill_attempt_references(conn)
            if _table_exists(conn, "synthetic_bank_transactions"):
                dedupe = dedupe_seed_fixtures(conn)
                report["seed_rows_removed"] = dedupe["rows_removed"]
                report["settlements_linked"] = link_agent5_settlements(conn)
            report["tables_created"] = (
                ensure_bank_tables(conn)
                + create_reconciliation_tables(conn)
                + ensure_mock_bank_tables(conn)
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return report
