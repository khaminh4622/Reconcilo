"""Bank statement importer.

Parses synthetic bank statements (CSV or PDF) and stores them as *evidence*, not
as facts about payment. Two separations matter here:

1. A bank statement never passes through Agent 3. It has no customer intent, so
   classifying it as a customer request would be meaningless.
2. An imported statement is simulated evidence. It is labelled ``simulated`` and
   only accepted when an operator explicitly requested an authorized test-mode
   import.

The importer is deliberately strict: a row with an unparseable amount or a
missing reference is rejected rather than guessed at.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


class BankImportError(ValueError):
    """A statement could not be imported."""


@dataclass
class BankTransactionRow:
    """One parsed statement line."""

    bank_transaction_id: str
    payment_reference: str | None
    transaction_date: str | None
    description: str | None
    counterparty: str | None
    amount: Decimal
    currency: str
    direction: str          # "credit" (money received) or "debit"
    raw: dict = field(default_factory=dict)

    @property
    def is_credit(self) -> bool:
        return self.direction == "credit"


# Column aliases, matched case-insensitively after normalisation.
ALIASES = {
    "bank_transaction_id": {
        "transaction id", "transaction_id", "txn id", "txn_id", "bank transaction id",
        "bank_transaction_id", "id", "reference id",
    },
    "payment_reference": {
        "payment reference", "payment_reference", "reference", "ref", "invoice reference",
        "invoice_reference", "invoice number", "invoice_number", "payment ref",
    },
    "transaction_date": {
        "transaction date", "transaction_date", "date", "value date", "value_date", "posted",
    },
    "description": {"description", "details", "narrative", "memo", "particulars"},
    "counterparty": {
        "counterparty", "sender", "payer", "from", "payee", "name", "originator",
        "debtor", "creditor",
    },
    "amount": {"amount", "value", "txn amount", "transaction amount"},
    "currency": {"currency", "ccy", "curr"},
    "direction": {"direction", "type", "dr/cr", "credit/debit", "debit/credit"},
}


def _normalise_header(value: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (value or "").strip().casefold())


def _map_headers(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        key = _normalise_header(header)
        for canonical, aliases in ALIASES.items():
            if key in aliases and canonical not in mapping:
                mapping[canonical] = index
    return mapping


DATE_PATTERNS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y", "%b %d %Y",
    "%d %B %Y", "%B %d %Y", "%Y/%m/%d",
)


def _parse_date(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    from datetime import datetime

    for pattern in DATE_PATTERNS:
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_amount(value: str | None) -> Decimal | None:
    """Parse money without ever rounding silently."""

    text = (value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    if cleaned in {"", "-", ".", "-."}:
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if negative:
        amount = -abs(amount)
    return amount


def _infer_direction(amount: Decimal, raw_direction: str | None) -> str:
    text = (raw_direction or "").strip().casefold()
    if text:
        if text in {"cr", "credit", "in", "received", "deposit", "inflow"}:
            return "credit"
        if text in {"dr", "debit", "out", "paid", "withdrawal", "outflow"}:
            return "debit"
    return "credit" if amount >= 0 else "debit"


def document_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ----------------------------------------------------------------------
def parse_csv(content: bytes) -> list[BankTransactionRow]:
    """Parse a CSV statement. Header names are matched leniently."""

    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise BankImportError("The CSV file could not be decoded as text.")

    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any((cell or "").strip() for cell in row)]
    if not rows:
        raise BankImportError("The CSV file is empty.")

    headers = rows[0]
    mapping = _map_headers(headers)
    if "amount" not in mapping:
        raise BankImportError(
            "The CSV has no amount column. Expected a header such as 'Amount'."
        )

    parsed: list[BankTransactionRow] = []
    problems: list[str] = []
    for line_number, row in enumerate(rows[1:], start=2):
        def cell(name: str) -> str | None:
            index = mapping.get(name)
            if index is None or index >= len(row):
                return None
            return (row[index] or "").strip() or None

        raw_amount = cell("amount")
        amount = _parse_amount(raw_amount)
        if amount is None:
            problems.append(f"line {line_number}: amount {raw_amount!r} is not a number")
            continue

        reference = cell("payment_reference")
        transaction_id = cell("bank_transaction_id")
        if not reference and not transaction_id:
            problems.append(f"line {line_number}: no reference or transaction id")
            continue

        direction = _infer_direction(amount, cell("direction"))
        parsed.append(
            BankTransactionRow(
                bank_transaction_id=transaction_id
                or f"CSV-{document_hash(json.dumps(row).encode())[:10].upper()}",
                payment_reference=reference,
                transaction_date=_parse_date(cell("transaction_date")),
                description=cell("description"),
                counterparty=cell("counterparty"),
                amount=abs(amount),
                currency=(cell("currency") or "USD").upper(),
                direction=direction,
                raw=dict(zip(headers, row)),
            )
        )

    if not parsed:
        detail = ("; ".join(problems[:3])) if problems else "no usable rows"
        raise BankImportError(f"No valid transactions found in the CSV ({detail}).")
    return parsed


REFERENCE_RE = re.compile(r"\b(SP-[A-Z0-9]{6,}|PMT-[A-Z0-9]{4,}|TRF-[A-Z0-9]{4,}|INV-[A-Z0-9\-]{4,})\b")
AMOUNT_RE = re.compile(r"(?:USD|EUR|GBP|\$|€|£)?\s*(-?\d[\d,]*(?:\.\d{2})?)")


def parse_pdf_text(text: str) -> list[BankTransactionRow]:
    """Parse a bank statement from extracted text.

    Statement layouts vary, so this reads line by line: a row must contain an
    amount, and is only accepted when a reference or counterparty is present on
    the same line.
    """

    rows: list[BankTransactionRow] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        reference_match = REFERENCE_RE.search(stripped)
        amounts = [m for m in AMOUNT_RE.findall(stripped)]
        if not amounts:
            continue
        amount = _parse_amount(amounts[-1])
        if amount is None:
            continue
        reference = reference_match.group(1) if reference_match else None

        # The description is what remains once the reference and amount go.
        remainder = stripped
        if reference:
            remainder = remainder.replace(reference, " ")
        remainder = AMOUNT_RE.sub(" ", remainder)
        description = " ".join(remainder.split()) or None
        if not reference and not description:
            continue

        direction = "credit" if amount >= 0 else "debit"
        date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", stripped)
        rows.append(
            BankTransactionRow(
                bank_transaction_id=f"PDF-{hashlib.sha256(stripped.encode()).hexdigest()[:10].upper()}",
                payment_reference=reference,
                transaction_date=date_match.group(1) if date_match else None,
                description=description,
                counterparty=description,
                amount=abs(amount),
                currency="USD" if "$" in stripped or "USD" in stripped.upper() else "USD",
                direction=direction,
                raw={"line": stripped},
            )
        )
    if not rows:
        raise BankImportError(
            "No transactions could be read from the statement text. "
            "Expected one transaction per line with an amount and a reference."
        )
    return rows


# ----------------------------------------------------------------------
class BankTransactionStore:
    """Persistent, deduplicated bank evidence."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bank_transactions (
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bank_imports (
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
            conn.commit()
        finally:
            conn.close()

    def already_imported(self, digest: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM bank_imports WHERE document_hash = ?", (digest,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def import_rows(
        self,
        rows: list[BankTransactionRow],
        *,
        filename: str | None,
        source_type: str,
        digest: str,
    ) -> dict:
        """Insert rows, skipping duplicates. Returns a summary."""

        from uuid import uuid4

        existing_import = self.already_imported(digest)
        if existing_import is not None:
            return {
                "import_id": existing_import["import_id"],
                "imported": 0,
                "skipped_duplicate_file": True,
                "row_count": existing_import["row_count"],
                "filename": existing_import["filename"],
            }

        import_id = str(uuid4())
        inserted = 0
        duplicates = 0
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    # A duplicate line is one with the same bank id, reference,
                    # amount, currency and date. Two genuinely separate
                    # settlements differ in at least one of those.
                    duplicate_key = hashlib.sha256(
                        "|".join([
                            row.bank_transaction_id or "",
                            (row.payment_reference or "").casefold(),
                            f"{row.amount:.2f}",
                            row.currency or "",
                            row.transaction_date or "",
                        ]).encode("utf-8")
                    ).hexdigest()
                    # A repeated line inside one statement is *evidence of a
                    # problem*, not noise. It is stored and flagged so Agent 2
                    # can investigate whether it is a duplicate import or two
                    # genuinely distinct settlements.
                    prior = conn.execute(
                        "SELECT row_id FROM bank_transactions WHERE duplicate_key = ? LIMIT 1",
                        (duplicate_key,),
                    ).fetchone()
                    conn.execute(
                        """
                        INSERT INTO bank_transactions (
                            row_id, bank_transaction_id, payment_reference, transaction_date,
                            description, counterparty, amount, currency, direction,
                            duplicate_key, import_id, duplicate_of, simulated, raw_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            str(uuid4()),
                            row.bank_transaction_id,
                            row.payment_reference,
                            row.transaction_date,
                            row.description,
                            row.counterparty,
                            f"{row.amount:.2f}",
                            row.currency,
                            row.direction,
                            duplicate_key,
                            import_id,
                            prior[0] if prior else None,
                            1,
                            json.dumps(row.raw, default=str),
                        ),
                    )
                    inserted += 1
                    if prior is not None:
                        duplicates += 1
                conn.execute(
                    """
                    INSERT INTO bank_imports
                        (import_id, filename, source_type, document_hash, row_count, simulated)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (import_id, filename, source_type, digest, inserted, 1),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

        return {
            "import_id": import_id,
            "imported": inserted,
            "duplicates_skipped": duplicates,
            "skipped_duplicate_file": False,
            "row_count": inserted,
            "filename": filename,
        }

    def list_transactions(
        self,
        *,
        payment_reference: str | None = None,
        include_outgoing: bool = False,
    ) -> list[dict]:
        conn = self._connect()
        try:
            query = "SELECT * FROM bank_transactions"
            clauses: list[str] = []
            params: list = []
            if not include_outgoing:
                clauses.append("direction = 'credit'")
            if payment_reference:
                clauses.append("payment_reference = ? COLLATE NOCASE")
                params.append(payment_reference)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY created_at, row_id"
            return [dict(row) for row in conn.execute(query, params)]
        finally:
            conn.close()

    def import_history(self) -> list[dict]:
        conn = self._connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM bank_imports ORDER BY imported_at DESC, rowid DESC"
                )
            ]
        finally:
            conn.close()
