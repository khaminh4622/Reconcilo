"""Account ownership for every financial record.

The GhostLedger database was single-tenant: one set of invoices, payments and
bank rows shared by every visitor. This module adds an owner to each record and
keeps the existing rows safe.

Rules:

1. **Additive only.** Columns are added; no table is dropped and no row deleted.
2. **Nothing is auto-assigned.** Existing rows had no owner, so they are placed
   in a restricted ``legacy_unassigned`` state until an admin verifies them.
   They are never given to whichever customer signs up next.
3. **A backup is taken first**, always.

Ownership columns:

    workflow_cases.owner_account_id
    payment_attempts.owner_account_id
    payable_obligations.owner_account_id
    mock_bank_ledger.owner_account_id
    payment_receipts.owner_account_id
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .migrations import backup_database

# Status given to records that existed before accounts did.
LEGACY_STATE = "legacy_unassigned"

# table -> whether the ownership column already existed
OWNED_TABLES = (
    "workflow_cases",
    "payment_attempts",
    "payable_obligations",
    "mock_bank_ledger",
    "payment_receipts",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def add_ownership_columns(conn: sqlite3.Connection) -> list[str]:
    """Give every financial table an owner. Adds nothing else."""

    added: list[str] = []
    for table in OWNED_TABLES:
        if not _table_exists(conn, table):
            continue
        columns = _columns(conn, table)
        if "owner_account_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_account_id TEXT")
            added.append(f"{table}.owner_account_id")
        # Retained for auditing which rows predate accounts.
        if "ownership_state" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN ownership_state TEXT")
            added.append(f"{table}.ownership_state")
    return added


def mark_legacy_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """Label unowned rows instead of guessing an owner.

    A row written before accounts existed cannot be attributed safely, so it is
    recorded as ``legacy_unassigned`` and hidden from customers until an admin
    claims it.
    """

    counts: dict[str, int] = {}
    for table in OWNED_TABLES:
        if not _table_exists(conn, table):
            continue
        columns = _columns(conn, table)
        if "ownership_state" not in columns:
            continue
        cursor = conn.execute(
            f"UPDATE {table} SET ownership_state = ? "
            f"WHERE (owner_account_id IS NULL OR owner_account_id = '') "
            f"AND (ownership_state IS NULL OR ownership_state = '')",
            (LEGACY_STATE,),
        )
        counts[table] = cursor.rowcount
    return counts


def add_indexes(conn: sqlite3.Connection) -> list[str]:
    created: list[str] = []
    for table in OWNED_TABLES:
        if not _table_exists(conn, table):
            continue
        name = f"ix_{table}_owner"
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table}(owner_account_id)"
        )
        created.append(name)
    return created


def create_account_tables(conn: sqlite3.Connection) -> list[str]:
    """Accounts and sessions, so ownership always has a real referent."""

    created: list[str] = []
    if not _table_exists(conn, "accounts"):
        conn.execute(
            """
            CREATE TABLE accounts (
                account_id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'CUSTOMER',
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        created.append("accounts")
    if not _table_exists(conn, "sessions"):
        conn.execute(
            """
            CREATE TABLE sessions (
                token_hash TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (account_id) REFERENCES accounts(account_id)
            )
            """
        )
        created.append("sessions")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_sessions_account ON sessions(account_id)"
    )
    return created


def migrate_tenancy(database_path: str | Path, take_backup: bool = True) -> dict:
    """Apply the ownership migration. Safe to run repeatedly."""

    path = Path(database_path)
    report: dict = {
        "database": str(path),
        "backup": None,
        "tables_created": [],
        "columns_added": [],
        "legacy_marked": {},
        "indexes": [],
    }
    if take_backup and path.exists():
        report["backup"] = str(backup_database(path))

    conn = sqlite3.connect(path, timeout=20.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            report["tables_created"] = create_account_tables(conn)
            report["columns_added"] = add_ownership_columns(conn)
            report["legacy_marked"] = mark_legacy_rows(conn)
            report["indexes"] = add_indexes(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return report


def claim_legacy_row(
    conn: sqlite3.Connection, table: str, row_key_column: str, row_key: str,
    account_id: str,
) -> bool:
    """Attach one legacy row to an account, after an admin has verified it."""

    if table not in OWNED_TABLES:
        raise ValueError(f"{table} is not an owned table.")
    cursor = conn.execute(
        f"UPDATE {table} SET owner_account_id = ?, ownership_state = 'claimed' "
        f"WHERE {row_key_column} = ?",
        (account_id, row_key),
    )
    return cursor.rowcount > 0
