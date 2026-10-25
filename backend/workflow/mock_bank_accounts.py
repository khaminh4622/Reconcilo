"""Simulated bank accounts, payment requests and the settlement engine.

This is the *bank's* bookkeeping. It extends the Mock Bank (``mock_bank.py``,
which holds the settlement ledger and instruction log) with the four records an
internal bank needs:

    bank_accounts       one simulated account per owner (customer or vendor)
    payment_requests    an authorised instruction waiting to be processed
    payment_approvals   the human decision that made a request executable
    bank_audit_logs     every bank-side action, in order

Money rules enforced here, deterministically:

* Amounts are stored as **integer minor units** (cents). No float ever touches a
  financial calculation; ``Decimal`` is parsed from text and converted once.
* A transfer is **one SQLite transaction**: debit the payer, credit the payee,
  post both ledger entries, write the settlement reference and flip the request
  to SETTLED. Any failure rolls back everything, so a partial transfer is not
  representable.
* Total simulated funds are conserved by an internal transfer. The double-entry
  postings are checked before commit and the transaction is refused otherwise.
* ``payment_requests.idempotency_key`` is UNIQUE, so the same approved payment
  can never be processed twice, even by concurrent callers.

No real money exists or moves anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

MINOR_UNITS = Decimal("100")

# ---------------------------------------------------------------------------
# Status vocabularies
# ---------------------------------------------------------------------------
ACCOUNT_ACTIVE = "ACTIVE"
ACCOUNT_CLOSED = "CLOSED"
ACCOUNT_FROZEN = "FROZEN"

REQUEST_PENDING = "PENDING_APPROVAL"   # created, no authorisation yet
REQUEST_APPROVED = "APPROVED"          # a human authorised it; bank has not run it
REQUEST_PROCESSING = "PROCESSING"      # claimed by the bank, mid-transfer
REQUEST_SETTLED = "SETTLED"            # both entries committed
REQUEST_REJECTED = "REJECTED"          # the bank (or an approver) refused it
REQUEST_FAILED = "FAILED"              # processing attempted, bank rules refused it
REQUEST_UNCERTAIN = "UNCERTAIN"        # outcome unknown; must be investigated
REQUEST_CANCELLED = "CANCELLED"

# An uncertain transfer may already have reached the bank.  It is live until a
# reviewer resolves it, never a licence to create another instruction.
LIVE_REQUEST_STATUSES = {REQUEST_PENDING, REQUEST_APPROVED, REQUEST_PROCESSING, REQUEST_UNCERTAIN}
TERMINAL_REQUEST_STATUSES = {
    REQUEST_SETTLED,
    REQUEST_REJECTED,
    REQUEST_FAILED,
    REQUEST_CANCELLED,
}

# Bank-side refusal reasons. Agent 4 reads these; it never invents one.
REJECTION_APPROVAL_MISSING = "APPROVAL_MISSING"
REJECTION_APPROVER_NOT_ELIGIBLE = "APPROVER_NOT_ELIGIBLE"
REJECTION_SAME_ACCOUNT = "SAME_ACCOUNT"
REJECTION_UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
REJECTION_ACCOUNT_BLOCKED = "ACCOUNT_BLOCKED"
REJECTION_CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
REJECTION_INVALID_AMOUNT = "INVALID_AMOUNT"
REJECTION_MISSING_REFERENCE = "MISSING_REFERENCE"

TOP_UP_PREFIX = "SEED-TOPUP-"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------
def to_minor(value: Decimal | str | int | None) -> int:
    """Convert an amount to integer minor units (cents).

    ``Decimal`` is used throughout so no binary floating point ever decides a
    balance. A value with more than two decimal places is refused rather than
    silently rounded.
    """

    if value is None:
        raise ValueError("An amount is required.")
    if isinstance(value, bool):
        raise ValueError("An amount must be a number.")
    if isinstance(value, int):
        return value * 100
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{value!r} is not a valid amount.") from exc
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError(f"{value} has more than two decimal places.")
    return int(amount * MINOR_UNITS)


def from_minor(minor: int | None) -> str:
    """Render minor units back as a fixed two-decimal string."""

    if minor is None:
        return "0.00"
    sign = "-" if minor < 0 else ""
    whole, part = divmod(abs(int(minor)), 100)
    return f"{sign}{whole}.{part:02d}"


def _money_or_none(minor: int | None) -> str | None:
    return None if minor is None else from_minor(minor)


def transfer_reference(payment_id: str) -> str:
    """The bank's own settlement reference for a payment request."""

    return f"MB-{hashlib.sha256(payment_id.encode('utf-8')).hexdigest()[:12].upper()}"


def mask_account_number(account_number: str) -> str:
    digits = "".join(ch for ch in account_number if ch.isalnum())
    tail = digits[-4:].rjust(4, "0")
    return f"****{tail}"


class BankError(RuntimeError):
    """A bank operation was refused. ``code`` is the machine-readable reason."""

    def __init__(self, message: str, code: str = REJECTION_UNKNOWN_ACCOUNT):
        super().__init__(message)
        self.code = code


class MockBankStore:
    """Simulated accounts, payment requests and the settlement engine."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The account ledger posts into MockBank's evidence table.  Initialise
        # it before any account transaction starts so this store is safe to use
        # on its own (not only through FastAPI, which historically did this
        # setup first).
        from .mock_bank import MockBank

        MockBank(self.path)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Connections and schema
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bank_accounts (
                    account_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    owner_type TEXT NOT NULL,
                    account_number_masked TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    balance_minor INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    display_name TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_bank_accounts_owner
                    ON bank_accounts(owner_id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_bank_accounts_owner_currency
                    ON bank_accounts(owner_id, currency);

                CREATE TABLE IF NOT EXISTS payment_requests (
                    payment_id TEXT PRIMARY KEY,
                    case_id TEXT,
                    customer_id TEXT,
                    invoice_id TEXT,
                    source_account_id TEXT,
                    destination_account_id TEXT,
                    amount_minor INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    settlement_reference TEXT,
                    failure_code TEXT,
                    failure_reason TEXT,
                    payee TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    processed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS ix_payment_requests_case
                    ON payment_requests(case_id);
                CREATE TABLE IF NOT EXISTS payment_approvals (
                    approval_id TEXT PRIMARY KEY,
                    payment_id TEXT NOT NULL,
                    approver_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    decision_time TEXT NOT NULL,
                    reason TEXT,
                    FOREIGN KEY (payment_id) REFERENCES payment_requests(payment_id)
                );

                CREATE INDEX IF NOT EXISTS ix_payment_approvals_payment
                    ON payment_approvals(payment_id);

                CREATE TABLE IF NOT EXISTS bank_audit_logs (
                    log_id TEXT PRIMARY KEY,
                    actor_id TEXT,
                    actor_role TEXT,
                    action TEXT NOT NULL,
                    resource_type TEXT,
                    resource_id TEXT,
                    timestamp TEXT NOT NULL,
                    details TEXT
                );

                CREATE INDEX IF NOT EXISTS ix_bank_audit_resource
                    ON bank_audit_logs(resource_id);
                CREATE INDEX IF NOT EXISTS ix_bank_audit_timestamp
                    ON bank_audit_logs(timestamp);
                """
            )
            # Older installations protected SETTLED invoice numbers globally.
            # That made two different customers with the same vendor invoice
            # number conflict.  Bank identity includes the customer, payee and
            # currency, so both the live-request and settled guards use that
            # scope.  We preserve all historic rows while upgrading the index.
            conn.execute("DROP INDEX IF EXISTS ux_payment_requests_settled_invoice")
            try:
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_requests_settled_identity
                    ON payment_requests(
                        customer_id, invoice_id COLLATE NOCASE,
                        payee COLLATE NOCASE, currency COLLATE NOCASE
                    ) WHERE status = 'SETTLED'
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_requests_live_identity
                    ON payment_requests(
                        customer_id, invoice_id COLLATE NOCASE,
                        payee COLLATE NOCASE, currency COLLATE NOCASE
                    ) WHERE status IN ('PENDING_APPROVAL', 'APPROVED', 'PROCESSING', 'UNCERTAIN')
                    """
                )
            except sqlite3.IntegrityError:
                # A database created by an older version can contain historic
                # duplicate rows.  Never delete evidence at startup; the
                # transactional lookup in create_payment_request still blocks
                # every new duplicate instruction.
                pass
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    def _audit(
        self,
        conn: sqlite3.Connection,
        *,
        actor_id: str | None,
        actor_role: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        details: dict | str | None = None,
    ) -> None:
        """Append one bank audit line inside the caller's transaction."""

        payload = details if isinstance(details, str) or details is None else json.dumps(details, default=str)
        conn.execute(
            """
            INSERT INTO bank_audit_logs
                (log_id, actor_id, actor_role, action, resource_type, resource_id, timestamp, details)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid4()),
                actor_id,
                actor_role,
                action,
                resource_type,
                resource_id,
                utc_now(),
                payload,
            ),
        )

    def audit(
        self,
        *,
        actor_id: str | None,
        actor_role: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        details: dict | str | None = None,
    ) -> None:
        """Record a bank-side action outside any transfer transaction."""

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._audit(
                    conn,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    details=details,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    @staticmethod
    def _account_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["balance"] = from_minor(data.get("balance_minor") or 0)
        return data

    def account(self, account_id: str | None) -> dict | None:
        if not account_id:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM bank_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        finally:
            conn.close()
        return self._account_row(row)

    def account_for_owner(self, owner_id: str, currency: str = "USD") -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM bank_accounts WHERE owner_id = ? AND currency = ? COLLATE NOCASE",
                (owner_id, currency.upper()),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM bank_accounts WHERE owner_id = ? ORDER BY created_at LIMIT 1",
                    (owner_id,),
                ).fetchone()
        finally:
            conn.close()
        return self._account_row(row)

    def accounts(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM bank_accounts ORDER BY owner_type, display_name, created_at"
            ).fetchall()
        finally:
            conn.close()
        return [self._account_row(row) for row in rows]

    def ensure_account(
        self,
        *,
        owner_id: str,
        owner_type: str,
        display_name: str | None = None,
        currency: str = "USD",
        opening_balance: Decimal | str | None = None,
        account_number: str | None = None,
        actor_id: str | None = None,
        actor_role: str = "SYSTEM",
    ) -> tuple[dict, bool]:
        """Create a simulated account once per (owner, currency).

        ``opening_balance`` is an explicit onboarding step. It is only applied
        when the account is created, so calling this again never moves a
        balance. An existing account is returned untouched.
        """

        existing = self.account_for_owner(owner_id, currency)
        if existing is not None and existing["currency"].upper() == currency.upper():
            return existing, False

        opening_minor = to_minor(opening_balance) if opening_balance is not None else 0
        if opening_minor < 0:
            raise ValueError("An opening balance cannot be negative.")
        account_id = f"BA-{uuid4().hex[:16].upper()}"
        number = account_number or account_id
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO bank_accounts (
                        account_id, owner_id, owner_type, account_number_masked, currency,
                        balance_minor, status, display_name, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(owner_id, currency) DO NOTHING
                    """,
                    (
                        account_id,
                        owner_id,
                        owner_type.upper(),
                        mask_account_number(number),
                        currency.upper(),
                        opening_minor,
                        ACCOUNT_ACTIVE,
                        display_name or owner_id,
                        utc_now(),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM bank_accounts WHERE owner_id = ? AND currency = ? COLLATE NOCASE",
                    (owner_id, currency.upper()),
                ).fetchone()
                created = bool(row) and row["account_id"] == account_id
                if created and opening_minor:
                    # The opening credit is a funding entry, not a transfer, so
                    # it is recorded as its own ledger row for the audit trail.
                    self._post_ledger(
                        conn,
                        account_id=account_id,
                        counterparty_account_id=None,
                        invoice_number=None,
                        payment_id=None,
                        amount_minor=opening_minor,
                        currency=currency.upper(),
                        entry_type="OPENING_BALANCE",
                        status="SETTLED",
                        description=f"Simulated opening balance for {display_name or owner_id}",
                        idempotency_key=f"{TOP_UP_PREFIX}{account_id}",
                    )
                if created:
                    self._audit(
                        conn,
                        actor_id=actor_id,
                        actor_role=actor_role,
                        action="account_opened",
                        resource_type="bank_account",
                        resource_id=account_id,
                        details={
                            "owner_id": owner_id,
                            "owner_type": owner_type,
                            "opening_balance": from_minor(opening_minor),
                            "currency": currency.upper(),
                        },
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return self._account_row(row), created

    def set_account_status(
        self, account_id: str, status: str, *, actor_id: str | None, actor_role: str
    ) -> dict:
        if status not in {ACCOUNT_ACTIVE, ACCOUNT_CLOSED, ACCOUNT_FROZEN}:
            raise BankError(f"Unknown account status {status!r}.", REJECTION_ACCOUNT_BLOCKED)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    "UPDATE bank_accounts SET status = ? WHERE account_id = ?", (status, account_id)
                )
                if cursor.rowcount == 0:
                    raise BankError("That simulated account does not exist.", REJECTION_UNKNOWN_ACCOUNT)
                self._audit(
                    conn,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    action="account_status_changed",
                    resource_type="bank_account",
                    resource_id=account_id,
                    details={"status": status},
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return self.account(account_id) or {}

    def top_up(
        self, account_id: str, amount: Decimal | str, *, actor_id: str | None, actor_role: str
    ) -> dict:
        """Fund a simulated account (a demo/onboarding operation, never a payment)."""

        minor = to_minor(amount)
        if minor <= 0:
            raise BankError("A top-up amount must be positive.", REJECTION_INVALID_AMOUNT)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM bank_accounts WHERE account_id = ?", (account_id,)
                ).fetchone()
                if row is None:
                    raise BankError("That simulated account does not exist.", REJECTION_UNKNOWN_ACCOUNT)
                conn.execute(
                    "UPDATE bank_accounts SET balance_minor = balance_minor + ? WHERE account_id = ?",
                    (minor, account_id),
                )
                self._post_ledger(
                    conn,
                    account_id=account_id,
                    counterparty_account_id=None,
                    invoice_number=None,
                    payment_id=None,
                    amount_minor=minor,
                    currency=row["currency"],
                    entry_type="TOP_UP",
                    status="SETTLED",
                    description=f"Simulated top-up of {from_minor(minor)}",
                    idempotency_key=f"{TOP_UP_PREFIX}{uuid4().hex}",
                )
                self._audit(
                    conn,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    action="account_topped_up",
                    resource_type="bank_account",
                    resource_id=account_id,
                    details={"amount": from_minor(minor)},
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return self.account(account_id) or {}

    def total_funds_minor(self, currency: str = "USD") -> int:
        """Every simulated cent the bank holds, for the conservation invariant."""

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(balance_minor), 0) FROM bank_accounts WHERE currency = ? COLLATE NOCASE",
                (currency,),
            ).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Payment requests
    # ------------------------------------------------------------------
    @staticmethod
    def _request_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["amount"] = from_minor(data.get("amount_minor"))
        return data

    def payment_request(self, payment_id: str | None) -> dict | None:
        if not payment_id:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM payment_requests WHERE payment_id = ?", (payment_id,)
            ).fetchone()
        finally:
            conn.close()
        return self._request_row(row)

    def payment_request_by_key(self, idempotency_key: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM payment_requests WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        finally:
            conn.close()
        return self._request_row(row)

    def payment_requests(
        self,
        *,
        case_id: str | None = None,
        customer_id: str | None = None,
        invoice_id: str | None = None,
        payee: str | None = None,
        currency: str | None = None,
        statuses: set[str] | None = None,
        limit: int = 500,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if case_id:
            clauses.append("case_id = ?")
            params.append(case_id)
        if customer_id:
            clauses.append("customer_id = ?")
            params.append(customer_id)
        if invoice_id:
            clauses.append("invoice_id = ? COLLATE NOCASE")
            params.append(invoice_id)
        if payee:
            clauses.append("payee = ? COLLATE NOCASE")
            params.append(payee)
        if currency:
            clauses.append("currency = ? COLLATE NOCASE")
            params.append(currency)
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(sorted(statuses))
        query = "SELECT * FROM payment_requests"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, int(limit)))
        conn = self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        return [self._request_row(row) for row in rows]

    def live_request_for_invoice(
        self,
        invoice_id: str | None,
        *,
        customer_id: str | None = None,
        payee: str | None = None,
        currency: str | None = None,
    ) -> dict | None:
        """A request for this invoice that is still live, if any.

        Live means awaiting approval, approved, or mid-processing. It is the
        check that stops a second payment request being created for one
        customer's invoice while the first is unresolved.  The optional
        identity fields are deliberately part of the lookup: two unrelated
        customers can legitimately receive invoices with the same number.
        """

        if not invoice_id:
            return None
        rows = self.payment_requests(
            customer_id=customer_id,
            invoice_id=invoice_id,
            payee=payee,
            currency=currency,
            statuses=LIVE_REQUEST_STATUSES,
            limit=5,
        )
        return rows[0] if rows else None

    def settled_request_for_invoice(
        self,
        invoice_id: str | None,
        *,
        customer_id: str | None = None,
        payee: str | None = None,
        currency: str | None = None,
    ) -> dict | None:
        if not invoice_id:
            return None
        rows = self.payment_requests(
            customer_id=customer_id,
            invoice_id=invoice_id,
            payee=payee,
            currency=currency,
            statuses={REQUEST_SETTLED},
            limit=1,
        )
        return rows[0] if rows else None

    def create_payment_request(
        self,
        *,
        customer_id: str,
        invoice_id: str | None,
        source_account_id: str | None,
        destination_account_id: str | None,
        amount: Decimal | str,
        currency: str,
        payee: str | None = None,
        case_id: str | None = None,
        idempotency_key: str,
        status: str = REQUEST_PENDING,
        actor_id: str | None = None,
        actor_role: str = "CUSTOMER",
    ) -> tuple[dict, bool]:
        """Create a pending payment request, or return the existing one.

        The ``idempotency_key`` is UNIQUE, so a repeated submission returns the
        original request with ``created=False`` instead of a second payable.
        A separate identity guard inside the write transaction also prevents
        two concurrent case uploads for the same customer's invoice from
        creating parallel live instructions.
        """

        existing = self.payment_request_by_key(idempotency_key)
        if existing is not None:
            return existing, False
        if to_minor(amount) <= 0:
            raise BankError("A payment amount must be positive.", REJECTION_INVALID_AMOUNT)

        payment_id = f"PR-{uuid4().hex[:16].upper()}"
        now = utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if invoice_id:
                    existing_row = conn.execute(
                        """
                        SELECT * FROM payment_requests
                        WHERE customer_id = ?
                          AND invoice_id = ? COLLATE NOCASE
                          AND payee = ? COLLATE NOCASE
                          AND currency = ? COLLATE NOCASE
                          AND status IN ('PENDING_APPROVAL', 'APPROVED', 'PROCESSING', 'UNCERTAIN', 'SETTLED')
                        ORDER BY created_at DESC, rowid DESC LIMIT 1
                        """,
                        (customer_id, invoice_id, payee, currency.upper()),
                    ).fetchone()
                    if existing_row is not None:
                        conn.execute("COMMIT")
                        return self._request_row(existing_row), False
                conn.execute(
                    """
                    INSERT INTO payment_requests (
                        payment_id, case_id, customer_id, invoice_id, source_account_id,
                        destination_account_id, amount_minor, currency, status, approved_by,
                        approved_at, idempotency_key, settlement_reference, failure_code,
                        failure_reason, payee, created_at, updated_at, processed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(idempotency_key) DO NOTHING
                    """,
                    (
                        payment_id,
                        case_id,
                        customer_id,
                        invoice_id,
                        source_account_id,
                        destination_account_id,
                        to_minor(amount),
                        currency.upper(),
                        status,
                        None,
                        None,
                        idempotency_key,
                        None,
                        None,
                        None,
                        payee,
                        now,
                        now,
                        None,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM payment_requests WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                created = bool(row) and row["payment_id"] == payment_id
                if created:
                    self._audit(
                        conn,
                        actor_id=actor_id,
                        actor_role=actor_role,
                        action="payment_requested",
                        resource_type="payment_request",
                        resource_id=payment_id,
                        details={
                            "invoice_id": invoice_id,
                            "amount": from_minor(to_minor(amount)),
                            "currency": currency.upper(),
                            "source_account_id": source_account_id,
                            "destination_account_id": destination_account_id,
                            "status": status,
                        },
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return self._request_row(row), created

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------
    def record_approval(
        self,
        *,
        payment_id: str,
        approver_id: str,
        approver_role: str,
        decision: str,
        reason: str | None = None,
        base_status: str = "APPROVED",
    ) -> dict:
        """Record a human decision and move the request accordingly.

        A rejection is terminal for the request. An approval only records the
        authorisation; it never moves money by itself.
        """

        decision = decision.upper()
        if decision not in {"APPROVED", "REJECTED"}:
            raise BankError(f"Unknown decision {decision!r}.", REJECTION_APPROVAL_MISSING)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM payment_requests WHERE payment_id = ?", (payment_id,)
                ).fetchone()
                if row is None:
                    raise BankError("That payment request does not exist.", REJECTION_UNKNOWN_ACCOUNT)
                if row["status"] not in {REQUEST_PENDING, REQUEST_APPROVED}:
                    raise BankError(
                        f"Payment {payment_id} is {row['status']}; it cannot be decided again.",
                        REJECTION_APPROVAL_MISSING,
                    )
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO payment_approvals
                        (approval_id, payment_id, approver_id, decision, decision_time, reason)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (str(uuid4()), payment_id, approver_id, decision, now, reason),
                )
                new_status = base_status if decision == "APPROVED" else REQUEST_REJECTED
                conn.execute(
                    """
                    UPDATE payment_requests
                    SET status = ?, approved_by = ?, approved_at = ?, failure_code = ?, failure_reason = ?,
                        updated_at = ?
                    WHERE payment_id = ?
                    """,
                    (
                        new_status,
                        approver_id if decision == "APPROVED" else row["approved_by"],
                        now if decision == "APPROVED" else row["approved_at"],
                        "REJECTED_BY_BANK_ADMIN" if decision == "REJECTED" else row["failure_code"],
                        reason if decision == "REJECTED" else row["failure_reason"],
                        now,
                        payment_id,
                    ),
                )
                self._audit(
                    conn,
                    actor_id=approver_id,
                    actor_role=approver_role,
                    action="payment_approved" if decision == "APPROVED" else "payment_rejected",
                    resource_type="payment_request",
                    resource_id=payment_id,
                    details={"decision": decision, "reason": reason},
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return self.payment_request(payment_id) or {}

    def approvals_for(self, payment_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM payment_approvals WHERE payment_id = ? ORDER BY decision_time, rowid",
                (payment_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Ledger posting
    # ------------------------------------------------------------------
    def _post_ledger(
        self,
        conn: sqlite3.Connection,
        *,
        account_id: str,
        counterparty_account_id: str | None,
        invoice_number: str | None,
        payment_id: str | None,
        amount_minor: int,
        currency: str,
        entry_type: str,
        status: str,
        description: str,
        idempotency_key: str,
        failure_code: str | None = None,
        failure_reason: str | None = None,
    ) -> str:
        """Write one ledger row on the same connection as the transfer.

        The uniqueness of ``idempotency_key`` in the instructions table is what
        makes a repeated submission a no-op. Ledger rows themselves are keyed on
        the posting, so a debit and a credit can never exist where only one was
        intended.
        """

        reference = transfer_reference(payment_id or idempotency_key)
        entry_id = f"BK-{hashlib.sha256(f'{reference}|{entry_type}|{account_id}'.encode()).hexdigest()[:14].upper()}"
        settled_at = utc_now()
        row = {
            "entry_id": entry_id,
            "payment_reference": reference,
            "amount": from_minor(amount_minor),
            "currency": currency.upper(),
            "counterparty": None,
            "direction": "debit" if entry_type == "DEBIT" else "credit",
            "status": status.upper(),
            "value_date": settled_at[:10],
            "description": description,
            "instruction_id": payment_id,
            "invoice_number": invoice_number,
            "customer_name": None,
            "vendor_name": None,
            "expected_amount": from_minor(amount_minor),
            "received_amount": from_minor(amount_minor) if status.upper() == "SETTLED" else None,
            "sender_account": account_id if entry_type == "DEBIT" else None,
            "receiver_account": account_id if entry_type == "CREDIT" else None,
            "transaction_date": settled_at[:10],
            "settlement_date": settled_at[:10] if status.upper() == "SETTLED" else None,
            "scenario": entry_type,
            "is_anomaly": 0,
            "failure_code": failure_code,
            "failure_reason": failure_reason,
            "audit_timestamp": settled_at,
        }
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
        return row["entry_id"]

    def ledger_rows_for_payment(self, payment_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM mock_bank_ledger WHERE instruction_id = ? ORDER BY direction DESC, rowid",
                (payment_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # The transfer engine
    # ------------------------------------------------------------------
    def process_payment(
        self,
        payment_id: str,
        *,
        actor_id: str | None,
        actor_role: str,
        payment_reference: str | None = None,
    ) -> dict:
        """Run the approved request against the deterministic bank rules.

        Everything happens inside one ``BEGIN IMMEDIATE`` transaction: the
        request is claimed, the rules are evaluated, the balances move and both
        ledger entries are posted, then the request becomes SETTLED. Any
        refusal rolls back and records FAILED in a separate transaction, so a
        refusal never leaves an unmatched debit.
        """

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM payment_requests WHERE payment_id = ?", (payment_id,)
                ).fetchone()
                if row is None:
                    raise BankError("That payment request does not exist.", REJECTION_UNKNOWN_ACCOUNT)

                # Idempotent: a settled request is returned, never re-run.
                if row["status"] == REQUEST_SETTLED:
                    conn.execute("COMMIT")
                    return {
                        "status": REQUEST_SETTLED,
                        "already_settled": True,
                        "settlement_reference": row["settlement_reference"],
                        "debit_entry_id": None,
                        "credit_entry_id": None,
                        "payment": self._request_row(row),
                    }
                if row["status"] != REQUEST_APPROVED:
                    raise BankError(
                        f"Payment {payment_id} is {row['status']}; only an APPROVED request can be processed.",
                        REJECTION_APPROVAL_MISSING,
                    )

                amount_minor = int(row["amount_minor"] or 0)
                currency = (row["currency"] or "").upper()
                source_id = row["source_account_id"]
                destination_id = row["destination_account_id"]

                # ---- deterministic bank-side rules --------------------
                approvals = conn.execute(
                    "SELECT approver_id, decision FROM payment_approvals WHERE payment_id = ?",
                    (payment_id,),
                ).fetchall()
                if not approvals or not any(a["decision"] == "APPROVED" for a in approvals):
                    raise BankError(
                        "No recorded human approval exists for this payment.",
                        REJECTION_APPROVAL_MISSING,
                    )
                if not source_id or not destination_id:
                    raise BankError(
                        "The payment request does not name both a payer and a recipient account.",
                        REJECTION_UNKNOWN_ACCOUNT,
                    )
                if source_id == destination_id:
                    raise BankError(
                        "The payer and recipient accounts are the same.",
                        REJECTION_SAME_ACCOUNT,
                    )
                if amount_minor <= 0:
                    raise BankError("The amount is not payable.", REJECTION_INVALID_AMOUNT)
                if not (payment_reference or row["invoice_id"]):
                    raise BankError(
                        "The payment has no reference, so the recipient cannot be identified.",
                        REJECTION_MISSING_REFERENCE,
                    )

                source = conn.execute(
                    "SELECT * FROM bank_accounts WHERE account_id = ?", (source_id,)
                ).fetchone()
                destination = conn.execute(
                    "SELECT * FROM bank_accounts WHERE account_id = ?", (destination_id,)
                ).fetchone()
                if source is None or destination is None:
                    raise BankError(
                        "The payer or recipient account does not exist.",
                        REJECTION_UNKNOWN_ACCOUNT,
                    )
                if (source["status"] or "").upper() != ACCOUNT_ACTIVE:
                    raise BankError(
                        f"The payer account is {source['status']}, so it cannot send funds.",
                        REJECTION_ACCOUNT_BLOCKED,
                    )
                if (destination["status"] or "").upper() != ACCOUNT_ACTIVE:
                    raise BankError(
                        f"The recipient account is {destination['status']}, so it cannot receive funds.",
                        REJECTION_ACCOUNT_BLOCKED,
                    )
                if (source["currency"] or "").upper() != currency:
                    raise BankError(
                        f"The payer account is in {source['currency']} but the payment is in {currency}.",
                        REJECTION_CURRENCY_MISMATCH,
                    )
                if (destination["currency"] or "").upper() != currency:
                    raise BankError(
                        f"The recipient account is in {destination['currency']} but the payment is in {currency}.",
                        REJECTION_CURRENCY_MISMATCH,
                    )
                if int(source["balance_minor"] or 0) < amount_minor:
                    raise BankError(
                        "The payer account does not hold sufficient simulated funds.",
                        "INSUFFICIENT_FUNDS",
                    )

                # ---- move the money -----------------------------------
                total_before = source["balance_minor"] + destination["balance_minor"]
                debit = conn.execute(
                    "UPDATE bank_accounts SET balance_minor = balance_minor - ? "
                    "WHERE account_id = ? AND balance_minor >= ?",
                    (amount_minor, source_id, amount_minor),
                )
                if debit.rowcount != 1:
                    # The balance changed since it was read: refuse rather than
                    # allow an overdraft.
                    raise BankError(
                        "The payer account does not hold sufficient simulated funds.",
                        "INSUFFICIENT_FUNDS",
                    )
                credit = conn.execute(
                    "UPDATE bank_accounts SET balance_minor = balance_minor + ? WHERE account_id = ?",
                    (amount_minor, destination_id),
                )
                if credit.rowcount != 1:
                    raise BankError(
                        "The recipient account could not be credited.",
                        REJECTION_UNKNOWN_ACCOUNT,
                    )
                after = conn.execute(
                    "SELECT balance_minor FROM bank_accounts WHERE account_id IN (?, ?)",
                    (source_id, destination_id),
                ).fetchall()
                if sum(int(r["balance_minor"]) for r in after) != total_before:
                    # Defence in depth: an internal transfer must conserve funds.
                    raise BankError(
                        "The transfer would not conserve simulated funds, so it was refused.",
                        REJECTION_INVALID_AMOUNT,
                    )

                reference = payment_reference or transfer_reference(payment_id)
                debit_entry = self._post_ledger(
                    conn,
                    account_id=source_id,
                    counterparty_account_id=destination_id,
                    invoice_number=row["invoice_id"],
                    payment_id=payment_id,
                    amount_minor=amount_minor,
                    currency=currency,
                    entry_type="DEBIT",
                    status="SETTLED",
                    description=f"Simulated transfer to {row['payee'] or destination_id} for {row['invoice_id'] or 'payment'}",
                    idempotency_key=row["idempotency_key"],
                )
                credit_entry = self._post_ledger(
                    conn,
                    account_id=destination_id,
                    counterparty_account_id=source_id,
                    invoice_number=row["invoice_id"],
                    payment_id=payment_id,
                    amount_minor=amount_minor,
                    currency=currency,
                    entry_type="CREDIT",
                    status="SETTLED",
                    description=f"Simulated receipt from {source['display_name'] or source_id} for {row['invoice_id'] or 'payment'}",
                    idempotency_key=row["idempotency_key"],
                )

                # SETTLED only now that both entries are committed in this txn.
                now = utc_now()
                conn.execute(
                    """
                    UPDATE payment_requests
                    SET status = ?, settlement_reference = ?, processed_at = ?, updated_at = ?,
                        failure_code = NULL, failure_reason = NULL
                    WHERE payment_id = ? AND status = ?
                    """,
                    (REQUEST_SETTLED, reference, now, now, payment_id, REQUEST_APPROVED),
                )
                self._audit(
                    conn,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    action="payment_settled",
                    resource_type="payment_request",
                    resource_id=payment_id,
                    details={
                        "settlement_reference": reference,
                        "amount": from_minor(amount_minor),
                        "currency": currency,
                        "source_account_id": source_id,
                        "destination_account_id": destination_id,
                        "debit_entry_id": debit_entry,
                        "credit_entry_id": credit_entry,
                    },
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

        return {
            "status": REQUEST_SETTLED,
            "already_settled": False,
            "settlement_reference": reference,
            "debit_entry_id": debit_entry,
            "credit_entry_id": credit_entry,
            "payment": self.payment_request(payment_id),
        }

    def fail_payment(
        self,
        payment_id: str,
        *,
        code: str,
        reason: str,
        actor_id: str | None,
        actor_role: str,
    ) -> dict:
        """Record a refusal. No balance changed and no ledger entry was posted."""

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM payment_requests WHERE payment_id = ?", (payment_id,)
                ).fetchone()
                if row is None:
                    raise BankError("That payment request does not exist.", REJECTION_UNKNOWN_ACCOUNT)
                if row["status"] == REQUEST_SETTLED:
                    raise BankError(
                        "That payment already settled; it cannot be failed.",
                        REJECTION_APPROVAL_MISSING,
                    )
                now = utc_now()
                conn.execute(
                    """
                    UPDATE payment_requests
                    SET status = ?, failure_code = ?, failure_reason = ?, processed_at = ?,
                        updated_at = ?
                    WHERE payment_id = ? AND status != 'SETTLED'
                    """,
                    (REQUEST_FAILED, code, reason, now, now, payment_id),
                )
                # A refused instruction posts a failure record (no balance
                # movement) so Agent 4 and the admin dashboard can see why.
                self._post_ledger(
                    conn,
                    account_id=row["source_account_id"],
                    counterparty_account_id=row["destination_account_id"],
                    invoice_number=row["invoice_id"],
                    payment_id=payment_id,
                    amount_minor=int(row["amount_minor"] or 0),
                    currency=row["currency"],
                    entry_type="FAILED",
                    status="FAILED",
                    description=f"Simulated transfer refused: {reason}",
                    idempotency_key=row["idempotency_key"],
                    failure_code=code,
                    failure_reason=reason,
                )
                self._audit(
                    conn,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    action="payment_failed",
                    resource_type="payment_request",
                    resource_id=payment_id,
                    details={"failure_code": code, "failure_reason": reason},
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return self.payment_request(payment_id) or {}

    def mark_uncertain(
        self,
        payment_id: str,
        *,
        reason: str,
        actor_id: str | None,
        actor_role: str,
    ) -> dict:
        """Preserve a request whose outcome is unknown. It is never auto-retried."""

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """
                    UPDATE payment_requests
                    SET status = ?, failure_code = 'BANK_TIMEOUT', failure_reason = ?, updated_at = ?
                    WHERE payment_id = ? AND status IN ('APPROVED','PROCESSING')
                    """,
                    (REQUEST_UNCERTAIN, reason, utc_now(), payment_id),
                )
                if cursor.rowcount:
                    self._audit(
                        conn,
                        actor_id=actor_id,
                        actor_role=actor_role,
                        action="payment_uncertain",
                        resource_type="payment_request",
                        resource_id=payment_id,
                        details={"reason": reason},
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return self.payment_request(payment_id) or {}

    # ------------------------------------------------------------------
    # Audit reads
    # ------------------------------------------------------------------
    def audit_logs(self, limit: int = 200, resource_id: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if resource_id:
                rows = conn.execute(
                    "SELECT * FROM bank_audit_logs WHERE resource_id = ? "
                    "ORDER BY timestamp DESC, rowid DESC LIMIT ?",
                    (resource_id, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bank_audit_logs ORDER BY timestamp DESC, rowid DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def summary(self) -> dict:
        """Counts for the bank dashboard."""

        conn = self._connect()
        try:
            by_status = {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM payment_requests GROUP BY status"
                )
            }
            accounts = conn.execute("SELECT COUNT(*) FROM bank_accounts").fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM payment_requests WHERE status IN (?, ?)",
                (REQUEST_FAILED, REQUEST_REJECTED),
            ).fetchone()[0]
            uncertain = conn.execute(
                "SELECT COUNT(*) FROM payment_requests WHERE status = ?", (REQUEST_UNCERTAIN,)
            ).fetchone()[0]
        finally:
            conn.close()
        return {
            "accounts": accounts,
            "requests_by_status": by_status,
            "failed_or_rejected": failed,
            "uncertain": uncertain,
            "pending_approval": by_status.get(REQUEST_PENDING, 0),
            "awaiting_processing": by_status.get(REQUEST_APPROVED, 0),
            "settled": by_status.get(REQUEST_SETTLED, 0),
        }
