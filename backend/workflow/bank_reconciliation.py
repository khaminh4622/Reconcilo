"""Agent 2 reconciliation: expected payments vs received bank evidence.

Compares what GhostLedger *expects* (Agent 5 payment records, Agent 3 invoices,
Agent 1 history) against what the bank *actually received* (imported statement
rows). Every comparison is deterministic Python arithmetic on ``Decimal``; no
model decides whether money arrived.

A matching amount alone never verifies a settlement. The reference, currency,
counterparty and relationship must corroborate it, otherwise the result is
NEEDS_HUMAN_REVIEW.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .bank_importer import BankTransactionStore
from .schemas import utc_now


class MatchType(str, Enum):
    matched = "MATCHED"
    partial_payment = "PARTIAL_PAYMENT"
    overpayment = "OVERPAYMENT"
    missing_payment = "MISSING_PAYMENT"
    duplicate_bank_transaction = "DUPLICATE_BANK_TRANSACTION"
    amount_mismatch = "AMOUNT_MISMATCH"
    currency_mismatch = "CURRENCY_MISMATCH"
    unmatched_bank_transaction = "UNMATCHED_BANK_TRANSACTION"
    needs_human_review = "NEEDS_HUMAN_REVIEW"


class MatchFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    label: str
    status: str
    detail: str


class ExpectedPayment(BaseModel):
    """What GhostLedger expects to be settled, from existing records."""

    case_id: str | None = None
    invoice_number: str | None = None
    customer_name: str | None = None
    vendor_name: str | None = None
    amount_due: Decimal | None = None
    currency: str | None = None
    payment_reference: str | None = None
    payment_status: str | None = None
    attempt_id: str | None = None
    ledger_entry: dict | None = None


class ReconciliationRow(BaseModel):
    """One reconciliation comparison, shown to the human."""

    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None = None
    case_id: str | None = None
    vendor_name: str | None = None
    customer_name: str | None = None
    expected_amount: Decimal | None = None
    bank_received: Decimal | None = None
    difference: Decimal | None = None
    currency: str | None = None
    expected_currency: str | None = None
    match_type: MatchType
    final_status: str
    payment_reference: str | None = None
    bank_reference: str | None = None
    bank_transaction_id: str | None = None
    bank_date: str | None = None
    bank_counterparty: str | None = None
    evidence: list[dict] = Field(default_factory=list)
    findings: list[MatchFinding] = Field(default_factory=list)


class ReconciliationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    created_at: str = Field(default_factory=utc_now)
    expected_count: int = 0
    bank_transaction_count: int = 0
    matched: int = 0
    unmatched_bank: int = 0
    rows: list[ReconciliationRow] = Field(default_factory=list)
    unmatched_bank_transactions: list[dict] = Field(default_factory=list)
    summary: str = ""


def _money(value: Decimal | None) -> str:
    return f"{value:.2f}" if value is not None else "unknown"


def _norm(value: str | None) -> str:
    return " ".join((value or "").replace(",", " ").replace(".", " ").split()).casefold()


def _names_match(expected: str | None, actual: str | None) -> bool:
    """Lenient counterparty comparison: containment either way."""

    a, b = _norm(expected), _norm(actual)
    if not a or not b:
        return False
    return a in b or b in a


def _days_between(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        first = datetime.fromisoformat(a[:10]).date()
        second = datetime.fromisoformat(b[:10]).date()
    except ValueError:
        return None
    return abs((first - second).days)


class ReconciliationService:
    """Deterministic expected-vs-received reconciliation."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.bank = BankTransactionStore(self.path)
        # The Mock Bank is the independent ledger Agent 2 reconciles against.
        from .mock_bank import MockBank

        self.mock_bank = MockBank(self.path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20.0)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    def expected_payments(self) -> list[ExpectedPayment]:
        """Read what GhostLedger expects, newest payment per invoice."""

        conn = self._connect()
        try:
            present = {
                r[1] for r in conn.execute("PRAGMA table_info(payment_attempts)")
            }
            ref_col = "provider_reference" if "provider_reference" in present else "NULL AS provider_reference"
            rows = conn.execute(
                f"""
                SELECT attempt_id, case_id, invoice_number, payee, amount, currency,
                       status, {ref_col}, attempted_at
                FROM payment_attempts
                ORDER BY attempted_at, rowid
                """
            ).fetchall()

            obligations = {}
            try:
                for row in conn.execute(
                    "SELECT invoice_number, customer, vendor, amount, currency, state FROM payable_obligations"
                ):
                    obligations[(row["invoice_number"] or "").casefold()] = dict(row)
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()

        latest: dict[str, ExpectedPayment] = {}
        for row in rows:
            key = (row["invoice_number"] or "").casefold()
            obligation = obligations.get(key, {})
            latest[key] = ExpectedPayment(
                case_id=row["case_id"],
                invoice_number=row["invoice_number"],
                customer_name=obligation.get("customer"),
                vendor_name=obligation.get("vendor") or row["payee"],
                amount_due=Decimal(row["amount"]) if row["amount"] else None,
                currency=row["currency"],
                payment_reference=row["provider_reference"],
                payment_status=row["status"],
                attempt_id=row["attempt_id"],
                ledger_entry=dict(row),
            )
        return list(latest.values())

    # ------------------------------------------------------------------
    def reconcile(self) -> ReconciliationReport:
        from uuid import uuid4

        expected = self.expected_payments()
        bank_rows = self._bank_credits()

        report = ReconciliationReport(
            report_id=str(uuid4()),
            expected_count=len(expected),
            bank_transaction_count=len(bank_rows),
        )

        used_bank: set[str] = set()
        for item in expected:
            row = self._compare(item, bank_rows, used_bank)
            report.rows.append(row)
            # Track the stored row ids that this expected payment consumed.
            for evidence in row.evidence:
                if evidence.get("row_id"):
                    used_bank.add(evidence["row_id"])
                if evidence.get("bank_transaction_id"):
                    used_bank.add(evidence["bank_transaction_id"])
            if row.match_type == MatchType.matched:
                report.matched += 1

        # Bank credits nobody expected.
        for bank in bank_rows:
            if bank["row_id"] in used_bank:
                continue
            report.unmatched_bank_transactions.append(
                {
                    "bank_transaction_id": bank["bank_transaction_id"],
                    "payment_reference": bank["payment_reference"],
                    "amount": bank["amount"],
                    "currency": bank["currency"],
                    "transaction_date": bank["transaction_date"],
                    "counterparty": bank["counterparty"],
                    "description": bank["description"],
                }
            )
        report.unmatched_bank = len(report.unmatched_bank_transactions)
        report.summary = (
            f"{report.matched} of {report.expected_count} expected payment(s) verified against "
            f"{report.bank_transaction_count} imported bank transaction(s); "
            f"{report.unmatched_bank} unmatched bank credit(s)."
        )
        self._persist(report)
        return report

    # ------------------------------------------------------------------
    def repository_for_legacy(self):
        """The repository that owns the legacy synthetic_bank_transactions table."""

        from .repository import WorkflowRepository

        return WorkflowRepository(self.path)

    def _bank_credits(self) -> list[dict]:
        """All received money: the Mock Bank ledger plus imported statements.

        Both are external to Agent 5, so both count as bank evidence. Each row
        is normalised to the same shape the comparison needs.
        """

        rows: list[dict] = []
        for entry in self.mock_bank.ledger():
            if entry["direction"] != "credit":
                continue
            received = entry.get("received_amount")
            status = (entry["status"] or "").lower()
            rows.append(
                {
                    "row_id": entry["entry_id"],
                    "bank_transaction_id": entry["entry_id"],
                    "payment_reference": entry.get("payment_reference"),
                    # A settled or partial row contributes the amount the bank
                    # actually credited; anything else contributes nothing.
                    "amount": received if received else entry["amount"],
                    "currency": entry["currency"],
                    "status": status,
                    "settled": status in {"settled", "partial"} and bool(received),
                    "transaction_date": entry.get("transaction_date") or entry["value_date"],
                    "settlement_date": entry.get("settlement_date"),
                    "counterparty": entry["counterparty"],
                    "description": entry["description"],
                    "invoice_number": entry.get("invoice_number"),
                    "expected_amount": entry.get("expected_amount"),
                    "sender_account": entry.get("sender_account"),
                    "receiver_account": entry.get("receiver_account"),
                    "scenario": entry.get("scenario"),
                    "is_anomaly": bool(entry.get("is_anomaly")),
                    "source": "mock_bank",
                    "duplicate_of": None,
                    "is_demo": 0,
                }
            )
        # Legacy and developer-imported rows live in synthetic_bank_transactions.
        try:
            legacy = self.repository_for_legacy().list_bank_transactions(500)
        except Exception:  # noqa: BLE001
            legacy = []
        for txn in legacy:
            status = str(getattr(txn, "status", "") or "").casefold()
            rows.append(
                {
                    "row_id": getattr(txn, "transaction_id", None),
                    "bank_transaction_id": getattr(txn, "transaction_id", None),
                    "payment_reference": getattr(txn, "reference", None),
                    "amount": str(getattr(txn, "amount", "") or ""),
                    "currency": getattr(txn, "currency", None),
                    "status": status,
                    "settled": status in {"settled", "partial"},
                    "transaction_date": getattr(txn, "value_date", None),
                    "settlement_date": getattr(txn, "value_date", None),
                    "counterparty": getattr(txn, "counterparty", None),
                    "description": None,
                    "invoice_number": None,
                    "expected_amount": None,
                    "sender_account": None,
                    "receiver_account": None,
                    "scenario": None,
                    "is_anomaly": False,
                    "source": "legacy_bank",
                    "duplicate_of": None,
                    "is_demo": 0,
                }
            )

        for row in self.bank.list_transactions(include_outgoing=False):
            # An imported statement line is a completed bank record.
            rows.append(
                {
                    "row_id": row["row_id"],
                    "bank_transaction_id": row["bank_transaction_id"],
                    "payment_reference": row.get("payment_reference"),
                    "amount": row["amount"],
                    "currency": row["currency"],
                    "status": "settled",
                    "settled": True,
                    "transaction_date": row.get("transaction_date"),
                    "settlement_date": row.get("transaction_date"),
                    "counterparty": row.get("counterparty"),
                    "description": row.get("description"),
                    "invoice_number": row.get("invoice_number"),
                    "expected_amount": None,
                    "sender_account": None,
                    "receiver_account": None,
                    "scenario": None,
                    "is_anomaly": False,
                    "source": "bank_import",
                    "duplicate_of": row.get("duplicate_of"),
                    "is_demo": row.get("is_demo", 0),
                }
            )
        return rows

    def recommendation(self) -> dict:
        """A compact verdict for the orchestrator and Agent 5."""

        report = self.reconcile()
        verdict = "NEEDS_REVIEW"
        matched_reference = None
        settled_amount = None
        independent = False
        currency = None

        for row in report.rows:
            if row.match_type == MatchType.matched:
                verdict = "MATCHED"
                matched_reference = row.bank_reference
                settled_amount = str(row.bank_received) if row.bank_received is not None else None
                independent = True
                currency = row.currency
                break
        else:
            if report.rows:
                top = report.rows[0]
                verdict = top.match_type.value
                matched_reference = top.bank_reference
                settled_amount = str(top.bank_received) if top.bank_received is not None else None
                currency = top.currency

        return {
            "verdict": verdict,
            "independent_evidence": independent,
            "matched_reference": matched_reference,
            "settled_amount": settled_amount,
            "currency": currency,
            "report_id": report.report_id,
            "summary": report.summary,
            "report": report.model_dump(mode="json"),
        }

    def _compare(
        self, expected: ExpectedPayment, bank_rows: list[dict], used: set[str]
    ) -> ReconciliationRow:
        findings: list[MatchFinding] = []
        base = ReconciliationRow(
            invoice_number=expected.invoice_number,
            case_id=expected.case_id,
            vendor_name=expected.vendor_name,
            customer_name=expected.customer_name,
            expected_amount=expected.amount_due,
            expected_currency=expected.currency,
            currency=expected.currency,
            payment_reference=expected.payment_reference,
            match_type=MatchType.missing_payment,
            final_status="UNVERIFIED",
        )

        # --- Candidates: same reference first, then same amount -----------
        by_reference = [
            b for b in bank_rows
            if expected.payment_reference
            and (b["payment_reference"] or "").casefold() == expected.payment_reference.casefold()
        ]
        by_invoice = [
            b for b in bank_rows
            if expected.invoice_number
            and (
                (b["payment_reference"] or "").casefold() == expected.invoice_number.casefold()
                or (b.get("invoice_number") or "").casefold() == expected.invoice_number.casefold()
            )
        ]
        by_amount = [
            b for b in bank_rows
            if expected.amount_due is not None
            and Decimal(b["amount"]) == expected.amount_due
        ]

        candidates = by_reference or by_invoice or by_amount
        if not candidates:
            findings.append(
                MatchFinding(
                    code="no_bank_transaction", label="No matching bank credit", status="failed",
                    detail=(
                        f"No imported bank transaction references {expected.payment_reference or 'this invoice'} "
                        f"or matches {_money(expected.amount_due)} {expected.currency or ''}.".strip()
                    ),
                )
            )
            # A customer-reported payment with no bank evidence is unverified.
            base.final_status = "UNVERIFIED"
            base.match_type = (
                MatchType.needs_human_review
                if expected.payment_status == "SUCCESS" and expected.payment_reference
                else MatchType.missing_payment
            )
            if base.match_type == MatchType.needs_human_review:
                findings.append(
                    MatchFinding(
                        code="claimed_but_unverified", label="Claimed paid, not verified", status="unverified",
                        detail=(
                            "GhostLedger recorded a payment, but no bank credit corroborates it. "
                            "The invoice must not be treated as settled."
                        ),
                    )
                )
            base.findings = findings
            return base

        # --- Duplicate bank lines ------------------------------------------
        distinct_references = {
            (b["payment_reference"] or b["bank_transaction_id"]) for b in candidates
        }
        bank_transaction_ids = {b["bank_transaction_id"] for b in candidates}
        dates = {b["transaction_date"] for b in candidates}
        flagged_duplicates = [b for b in candidates if b.get("duplicate_of")]
        if len(candidates) > 1:
            same_reference = len(distinct_references) == 1
            same_ids = len(bank_transaction_ids) == 1
            same_date = len(dates) == 1 and None not in dates
            total = sum((Decimal(b["amount"]) for b in candidates), Decimal("0"))
            expected_amount = expected.amount_due or Decimal("0")

            if same_ids or flagged_duplicates:
                # The same bank line imported twice: ingestion should prevent
                # this, so it indicates duplicate evidence rather than payment.
                findings.append(
                    MatchFinding(
                        code="duplicate_import", label="Duplicate bank line", status="failed",
                        detail=(
                            f"The statement contains {len(candidates)} lines for bank transaction "
                            f"{candidates[0]['bank_transaction_id']}. The importer flagged "
                            f"{len(flagged_duplicates)} as a repeat, which indicates duplicate "
                            "evidence rather than two settlements."
                        ),
                    )
                )
                base.match_type = MatchType.duplicate_bank_transaction
                base.final_status = "NEEDS_HUMAN_REVIEW"
                base.bank_received = total
                base.difference = total - expected_amount
                base.evidence = candidates
                base.findings = findings
                return base

            if same_reference and same_date and expected_amount and total > expected_amount:
                findings.append(
                    MatchFinding(
                        code="duplicate_settlement", label="Repeated settlement", status="failed",
                        detail=(
                            f"Two distinct transfers with reference {expected.payment_reference} settled on "
                            f"{next(iter(dates))}, totalling {_money(total)} against an expected "
                            f"{_money(expected_amount)}. That is a duplicate payment, not one settlement."
                        ),
                    )
                )
                base.match_type = MatchType.duplicate_bank_transaction
                base.final_status = "DUPLICATE_PAYMENT"
                base.bank_received = total
                base.difference = total - expected_amount
                base.evidence = candidates
                base.findings = findings
                return base

            findings.append(
                MatchFinding(
                    code="multiple_candidates", label="Several possible credits", status="unverified",
                    detail=(
                        f"{len(candidates)} bank credits could belong to this invoice. "
                        "Ambiguity requires a human decision."
                    ),
                )
            )
            base.match_type = MatchType.needs_human_review
            base.final_status = "NEEDS_HUMAN_REVIEW"
            base.bank_received = sum((Decimal(b["amount"]) for b in candidates), Decimal("0"))
            base.evidence = candidates
            base.findings = findings
            return base

        # --- Exactly one candidate: compare every attribute ---------------
        bank = candidates[0]

        # An amount that has not settled is not money received. A pending,
        # failed or missing-settlement entry must never count as received.
        if not bank.get("settled", True):
            findings.append(
                MatchFinding(
                    code="not_settled", label="Bank entry not settled", status="unverified",
                    detail=(
                        f"The bank holds {bank['amount']} {bank['currency']} under "
                        f"{bank.get('payment_reference')} with status "
                        f"{bank.get('status')}. A pending transfer is not received money."
                    ),
                )
            )
            base.match_type = MatchType.needs_human_review
            base.final_status = "PENDING"
            base.bank_received = None
            base.bank_reference = bank.get("payment_reference")
            base.bank_transaction_id = bank.get("bank_transaction_id")
            base.bank_date = bank.get("transaction_date")
            base.bank_counterparty = bank.get("counterparty")
            base.evidence = [bank]
            base.findings = findings
            return base

        received = Decimal(bank["amount"])
        expected_amount = expected.amount_due or Decimal("0")

        base.bank_received = received
        base.difference = received - expected_amount
        base.bank_reference = bank["payment_reference"]
        base.bank_transaction_id = bank["bank_transaction_id"]
        base.bank_date = bank["transaction_date"]
        base.bank_counterparty = bank["counterparty"]
        base.evidence = [bank]

        reference_ok = bool(
            expected.payment_reference
            and (bank["payment_reference"] or "").casefold() == expected.payment_reference.casefold()
        )
        invoice_reference_ok = bool(
            expected.invoice_number
            and (bank["payment_reference"] or "").casefold() == expected.invoice_number.casefold()
        )
        currency_ok = (bank["currency"] or "").upper() == (expected.currency or "").upper()
        counterparty_ok = _names_match(expected.vendor_name, bank["counterparty"]) or _names_match(
            expected.vendor_name, bank["description"]
        )
        date_gap = _days_between(expected.ledger_entry.get("attempted_at"), bank["transaction_date"])
        date_ok = date_gap is None or date_gap <= 30

        findings.append(
            MatchFinding(
                code="reference_check",
                label="Payment reference",
                status="passed" if (reference_ok or invoice_reference_ok) else "failed",
                detail=(
                    f"Bank reference {bank['payment_reference']} "
                    + ("matches" if reference_ok else "matches the invoice" if invoice_reference_ok else "does not match")
                    + f" the expected reference {expected.payment_reference or '(none)'}."
                ),
            )
        )
        findings.append(
            MatchFinding(
                code="currency_check",
                label="Currency",
                status="passed" if currency_ok else "failed",
                detail=f"Expected {expected.currency}, bank shows {bank['currency']}.",
            )
        )
        findings.append(
            MatchFinding(
                code="counterparty_check",
                label="Counterparty",
                status="passed" if counterparty_ok else "unverified",
                detail=(
                    f"Bank counterparty {bank['counterparty'] or '(blank)'} "
                    + ("relates to" if counterparty_ok else "does not clearly relate to")
                    + f" vendor {expected.vendor_name or '(unknown)'}."
                ),
            )
        )

        if not currency_ok:
            base.match_type = MatchType.currency_mismatch
            base.final_status = "NEEDS_HUMAN_REVIEW"
            findings.append(
                MatchFinding(
                    code="currency_mismatch", label="Currency mismatch", status="failed",
                    detail=(
                        f"The payment is expected in {expected.currency} but the bank credit is in "
                        f"{bank['currency']}. These amounts cannot be compared without a conversion."
                    ),
                )
            )
            base.findings = findings
            return base

        if received < expected_amount:
            base.match_type = MatchType.partial_payment
            base.final_status = "PARTIAL"
            findings.append(
                MatchFinding(
                    code="partial_payment", label="Partial payment", status="unverified",
                    detail=(
                        f"Bank received {_money(received)} {bank['currency']} of the expected "
                        f"{_money(expected_amount)}; {_money(expected_amount - received)} is still outstanding."
                    ),
                )
            )
            base.findings = findings
            return base

        if received > expected_amount:
            base.match_type = MatchType.overpayment
            base.final_status = "OVERPAID"
            findings.append(
                MatchFinding(
                    code="overpayment", label="Overpayment", status="unverified",
                    detail=(
                        f"Bank received {_money(received)} {bank['currency']} against an expected "
                        f"{_money(expected_amount)}; {_money(received - expected_amount)} is excess."
                    ),
                )
            )
            base.findings = findings
            return base

        # Amounts are equal: verify everything else before calling it settled.
        if reference_ok and counterparty_ok and date_ok:
            base.match_type = MatchType.matched
            base.final_status = "SETTLEMENT_VERIFIED"
            findings.append(
                MatchFinding(
                    code="matched", label="Settlement verified", status="passed",
                    detail=(
                        f"{_money(received)} {bank['currency']} received under {bank['payment_reference']} "
                        f"from {bank['counterparty']}, matching the expected payment."
                    ),
                )
            )
            base.findings = findings
            return base

        reasons = []
        if not reference_ok:
            reasons.append("the payment reference does not match")
        if not counterparty_ok:
            reasons.append("the counterparty cannot be confirmed")
        if not date_ok:
            reasons.append("the dates are inconsistent")
        base.match_type = (
            MatchType.unmatched_bank_transaction if not reference_ok else MatchType.needs_human_review
        )
        base.final_status = "NEEDS_HUMAN_REVIEW"
        findings.append(
            MatchFinding(
                code="insufficient_evidence", label="Amount matches, evidence does not", status="failed",
                detail=(
                    "The amount is equal, but " + "; ".join(reasons)
                    + ". A matching amount alone does not verify a settlement."
                ),
            )
        )
        base.findings = findings
        return base

    # ------------------------------------------------------------------
    def _persist(self, report: ReconciliationReport) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bank_reconciliation_runs (
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
            conn.execute(
                "INSERT OR REPLACE INTO bank_reconciliation_runs VALUES (?,?,?,?,?,?,?)",
                (
                    report.report_id,
                    report.created_at,
                    report.expected_count,
                    report.bank_transaction_count,
                    report.matched,
                    report.unmatched_bank,
                    report.model_dump_json(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
