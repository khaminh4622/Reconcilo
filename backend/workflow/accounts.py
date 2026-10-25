"""Accounts, roles and sessions.

GhostLedger had no concept of an authenticated customer: every case, payment and
bank row was globally visible. This module adds the ownership layer.

Three roles exist:

    CUSTOMER    sees only their own invoices, payments and confirmations
    BANK_ADMIN  sees Mock Bank transactions across accounts, for reconciliation
    DEMO_ADMIN  same visibility as BANK_ADMIN, for hackathon demonstrations

Passwords are stored as PBKDF2-HMAC-SHA256 hashes with a per-account salt. A
session is an opaque random token stored hashed, so a leaked database does not
leak live sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


class AuthError(ValueError):
    """Authentication or authorisation failed."""


ROLE_CUSTOMER = "CUSTOMER"
ROLE_BANK_ADMIN = "BANK_ADMIN"
ROLE_DEMO_ADMIN = "DEMO_ADMIN"
ROLES = {ROLE_CUSTOMER, ROLE_BANK_ADMIN, ROLE_DEMO_ADMIN}

# Roles allowed to see Mock Bank internals across accounts.
ADMIN_ROLES = {ROLE_BANK_ADMIN, ROLE_DEMO_ADMIN}

SESSION_DAYS = 30
PBKDF2_ROUNDS = 240_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS
    )
    return digest.hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Account:
    account_id: str
    email: str
    display_name: str
    role: str
    created_at: str

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES

    def public(self) -> dict:
        return {
            "account_id": self.account_id,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
            "created_at": self.created_at,
        }


class AccountStore:
    """Persistent accounts and sessions, in the existing GhostLedger database."""

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
                CREATE TABLE IF NOT EXISTS accounts (
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_sessions_account ON sessions(account_id)"
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def sign_up(
        self,
        *,
        email: str,
        password: str,
        display_name: str | None = None,
        role: str = ROLE_CUSTOMER,
    ) -> Account:
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise AuthError("A valid email address is required.")
        if not password or len(password) < 8:
            raise AuthError("The password must be at least 8 characters.")
        if role not in ROLES:
            raise AuthError(f"Unknown role {role!r}.")

        # Only an existing BANK_ADMIN may create another admin: handled by the
        # calling endpoint, which passes the role explicitly.
        salt = secrets.token_hex(16)
        account_id = str(uuid4())
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT 1 FROM accounts WHERE email = ?", (email,)
            ).fetchone()
            if existing is not None:
                raise AuthError("An account with that email already exists.")
            conn.execute(
                """
                INSERT INTO accounts
                    (account_id, email, display_name, role, password_salt, password_hash, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    account_id,
                    email,
                    (display_name or email.split("@")[0]).strip(),
                    role,
                    salt,
                    _hash_password(password, salt),
                    _now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get(account_id)

    def sign_in(self, *, email: str, password: str) -> tuple[Account, str]:
        email = (email or "").strip().lower()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM accounts WHERE email = ?", (email,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise AuthError("Email or password is incorrect.")
        expected = row["password_hash"]
        actual = _hash_password(password or "", row["password_salt"])
        if not hmac.compare_digest(expected, actual):
            raise AuthError("Email or password is incorrect.")

        account = self._row_to_account(row)
        return account, self._issue_token(account.account_id)

    # ------------------------------------------------------------------
    def _issue_token(self, account_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO sessions (token_hash, account_id, created_at, expires_at) VALUES (?,?,?,?)",
                (
                    _hash_token(token),
                    account_id,
                    now.isoformat(),
                    (now + timedelta(days=SESSION_DAYS)).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return token

    def sign_out(self, token: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sessions SET revoked = 1 WHERE token_hash = ?", (_hash_token(token),)
            )
            conn.commit()
        finally:
            conn.close()

    def account_for_token(self, token: str | None) -> Account | None:
        if not token:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT a.* FROM sessions s JOIN accounts a ON a.account_id = s.account_id "
                "WHERE s.token_hash = ? AND s.revoked = 0",
                (_hash_token(token),),
            ).fetchone()
            if row is None:
                return None
            expiry = row["expires_at"] if "expires_at" in row.keys() else None
        finally:
            conn.close()
        if expiry and expiry < _now():
            return None
        return self._row_to_account(row)

    def get(self, account_id: str) -> Account:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise AuthError("Account not found.")
        return self._row_to_account(row)

    def list_accounts(self, role: str | None = None) -> list[Account]:
        conn = self._connect()
        try:
            if role:
                rows = conn.execute(
                    "SELECT * FROM accounts WHERE role = ? ORDER BY created_at", (role,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
        finally:
            conn.close()
        return [self._row_to_account(r) for r in rows]

    def count(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        finally:
            conn.close()

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> Account:
        return Account(
            account_id=row["account_id"],
            email=row["email"],
            display_name=row["display_name"],
            role=row["role"],
            created_at=row["created_at"],
        )


def ensure_bootstrap_admin(store: AccountStore, database_path: str | Path) -> Account | None:
    """Create the first BANK_ADMIN from the environment, if configured.

    Without this there is no way to obtain an admin account after a fresh
    install. It is optional and never creates a customer.
    """

    email = (os.getenv("GHOSTLEDGER_ADMIN_EMAIL") or "").strip()
    password = (os.getenv("GHOSTLEDGER_ADMIN_PASSWORD") or "").strip()
    if not email or not password:
        return None
    try:
        return store.sign_up(
            email=email, password=password, display_name="Bank Administrator",
            role=ROLE_BANK_ADMIN,
        )
    except AuthError:
        # Already exists: return it, never duplicate.
        try:
            account, _token = store.sign_in(email=email, password=password)
            return account
        except AuthError:
            return None
