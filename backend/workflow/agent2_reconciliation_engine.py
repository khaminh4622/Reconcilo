"""Agent 2 - Bank reconciliation against the existing GhostLedger database.

Data priority (highest first):

1. The live GhostLedger database: Agent 5 payment attempts, receipts, and the
   invoice/obligation records written by Agents 3 and 1.
2. Synthetic bank evidence already stored in that same database.
3. Demo fixtures - only when ``DEMO_MODE`` is explicitly enabled.

Trust rule (deliberate and central to this design):

    A settlement record written by Agent 5 is NOT independent bank evidence.

Agent 5 writes ``SP-*`` rows on SUCCESS. Reconciling a payment against the row
Agent 5 itself created would prove nothing, so those rows are never accepted as
third-party confirmation. Agent 2 returns RECONCILIATION_REQUIRED instead of
claiming a payment was verified by a bank.

An invoice is only marked PAID when settlement is corroborated by evidence that
Agent 5 did not author. In this MVP that means no invoice can be promoted to
PAID by Agent 2 alone; it reports what it can and escalates the rest.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .schemas import utc_now


class ReconciliationOutcome(str, Enum):
    settlement_verified = "SETTLEMENT_VERIFIED"
    reconciliation_required = "RECONCILIATION_REQUIRED"
    discrepancy_detected = "DISCREPANCY_DETECTED"
    not_applicable = "NOT_APPLICABLE"


class EvidenceKind(str, Enum):
    agent5_execution = "AGENT5_EXECUTION"      # written by Agent 5 itself
    customer_report = "CUSTOMER_REPORT"        # claimed by the customer
    bank_statement = "BANK_STATEMENT"          # independent statement row
    demo_fixture = "DEMO_FIXTURE"              # generated sample data


class ReconFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    label: str
    status: str
    detail: str


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str
    case_id: str | None = None
    invoice_number: str | None = None
    payment_attempt_id: str | None = None
    provider_reference: str | None = None
    outcome: ReconciliationOutcome
    matched_reference: str | None = None
    expected_amount: Decimal | None = None
    settled_amount: Decimal | None = None
    currency: str | None = None
    confidence: float = 0.0
    independent_evidence: bool = False
    findings: list[ReconFinding] = Field(default_factory=list)
    invoice_marked_paid: bool = False
    created_at: str = Field(default_factory=utc_now)


def demo_mode_enabled() -> bool:
    return (os.getenv("DEMO_MODE") or "").strip().lower() in {"1", "true", "yes", "on"}


class ReconciliationRepository:
    """Read-only view over the existing database, plus result persistence."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20.0)
        conn.row_factory = sqlite3.Row
        return conn

    # -- Priority 1: Agent 5 payment records ----------------------------
    def payment_attempts(self, invoice_number: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            # Tolerate databases that predate the migration: fall back to the
            # JSON payload rather than failing, so Agent 2 never breaks a
            # workflow because a column is missing.
            present = {r[1] for r in conn.execute("PRAGMA table_info(payment_attempts)")}
            optional = {
                name: (name if name in present else f"NULL AS {name}")
                for name in (
                    "provider_reference",
                    "settlement_reference",
                    "provider_status",
                    "verified",
                    "settled_at",
                    "case_id",
                )
            }
            query = f"""
                SELECT attempt_id, invoice_number, payee, amount, currency,
                       status, transaction_id, failure_reason, attempted_at,
                       {optional['provider_reference']},
                       {optional['settlement_reference']},
                       {optional['provider_status']},
                       {optional['verified']},
                       {optional['settled_at']},
                       {optional['case_id']}
                FROM payment_attempts
            """
            params: tuple = ()
            if invoice_number:
                query += " WHERE invoice_number = ? COLLATE NOCASE"
                params = (invoice_number,)
            query += " ORDER BY attempted_at, rowid"
            return [dict(row) for row in conn.execute(query, params)]
        finally:
            conn.close()

    def receipts(self, invoice_number: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            query = "SELECT receipt_json FROM payment_receipts"
            params: tuple = ()
            if invoice_number:
                query += " WHERE invoice_number = ? COLLATE NOCASE"
                params = (invoice_number,)
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            try:
                out.append(json.loads(row["receipt_json"]))
            except (TypeError, ValueError):
                continue
        return out

    # -- Priority 2: bank records already in the database ---------------
    def bank_rows(self, invoice_number: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(synthetic_bank_transactions)")}
            select = "reference, amount, currency, status, counterparty, value_date, transaction_id"
            select += ", source" if "source" in columns else ", NULL AS source"
            select += ", invoice_number" if "invoice_number" in columns else ", NULL AS invoice_number"
            select += ", is_demo" if "is_demo" in columns else ", 0 AS is_demo"
            query = f"SELECT {select} FROM synthetic_bank_transactions"
            params: tuple = ()
            if invoice_number and "invoice_number" in columns:
                query += " WHERE invoice_number = ? COLLATE NOCASE"
                params = (invoice_number,)
            return [dict(row) for row in conn.execute(query, params)]
        finally:
            conn.close()

    def obligation(self, invoice_number: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM payable_obligations WHERE invoice_number = ? COLLATE NOCASE LIMIT 1",
                (invoice_number,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # -- result persistence ---------------------------------------------
    def save_result(self, result: ReconciliationResult) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO reconciliation_results (
                    result_id, case_id, invoice_number, payment_attempt_id, provider_reference,
                    outcome, matched_reference, expected_amount, settled_amount, currency,
                    confidence, findings_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.result_id,
                    result.case_id,
                    result.invoice_number,
                    result.payment_attempt_id,
                    result.provider_reference,
                    result.outcome.value,
                    result.matched_reference,
                    str(result.expected_amount) if result.expected_amount is not None else None,
                    str(result.settled_amount) if result.settled_amount is not None else None,
                    result.currency,
                    result.confidence,
                    json.dumps([f.model_dump() for f in result.findings]),
                    result.created_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def latest_result(self, invoice_number: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM reconciliation_results
                WHERE invoice_number = ? COLLATE NOCASE
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (invoice_number,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


class ReconciliationAgent:
    """Agent 2: reconcile a payment against available bank evidence."""

    def __init__(self, repository: ReconciliationRepository):
        self.repository = repository

    # ------------------------------------------------------------------
    def reconcile(self, case_id: str | None, invoice_number: str | None) -> ReconciliationResult:
        from uuid import uuid4

        findings: list[ReconFinding] = []
        result_id = str(uuid4())

        if not invoice_number:
            return self._result(
                result_id, case_id, invoice_number, None, None,
                ReconciliationOutcome.reconciliation_required, findings=[
                    self._finding("no_invoice", "No invoice number", "failed",
                                  "The case has no invoice number, so nothing can be reconciled.")
                ],
            )

        attempts = self.repository.payment_attempts(invoice_number)
        if not attempts:
            findings.append(
                self._finding(
                    "no_payment", "No payment to reconcile", "failed",
                    f"Invoice {invoice_number} has no payment attempt recorded, so there is nothing to reconcile.",
                )
            )
            return self._result(
                result_id, case_id, invoice_number, None, None,
                ReconciliationOutcome.reconciliation_required, findings=findings,
            )

        settled = [a for a in attempts if a["status"] == "SUCCESS"]
        pending = [a for a in attempts if a["status"] == "PENDING"]
        failed = [a for a in attempts if a["status"] == "FAILED"]

        if pending:
            findings.append(
                self._finding(
                    "payment_pending", "Payment not settled", "unverified",
                    f"{len(pending)} attempt(s) for this invoice are unresolved. No settlement can be verified.",
                )
            )
            return self._result(
                result_id, case_id, invoice_number, pending[-1]["attempt_id"],
                pending[-1].get("provider_reference"),
                ReconciliationOutcome.reconciliation_required, findings=findings,
            )

        if not settled:
            findings.append(
                self._finding(
                    "no_settlement", "No settled payment", "failed",
                    f"Invoice {invoice_number} has no successful payment"
                    + (f" ({len(failed)} failed attempt(s))." if failed else "."),
                )
            )
            return self._result(
                result_id, case_id, invoice_number, None, None,
                ReconciliationOutcome.reconciliation_required, findings=findings,
            )

        attempt = settled[-1]
        expected = Decimal(attempt["amount"]) if attempt["amount"] else None
        currency = attempt["currency"]
        reference = attempt.get("provider_reference")

        if not reference:
            findings.append(
                self._finding(
                    "no_provider_reference", "No payment reference", "unverified",
                    "This payment was recorded before payment references were captured, so there is "
                    "nothing to match against bank evidence. It cannot be verified as settled.",
                )
            )
            bank_candidates = self._bank_matches(invoice_number, attempt, allow_demo=False)
            if bank_candidates:
                findings.append(
                    self._finding(
                        "candidate_bank_rows", "Possible bank rows", "info",
                        f"{len(bank_candidates)} unlinked bank row(s) resemble this payment but cannot be "
                        "matched without a reference.",
                    )
                )
            return self._result(
                result_id, case_id, invoice_number, attempt["attempt_id"], None,
                ReconciliationOutcome.reconciliation_required,
                expected_amount=expected, currency=currency, findings=findings,
            )

        # --- Compare against bank evidence -----------------------------
        bank_rows = self._bank_rows_for(reference, invoice_number)
        independent = [
            row for row in bank_rows
            if self._evidence_kind(row) in {EvidenceKind.bank_statement, EvidenceKind.customer_report}
        ]
        agent5_rows = [row for row in bank_rows if self._evidence_kind(row) == EvidenceKind.agent5_execution]
        demo_rows = [row for row in bank_rows if self._evidence_kind(row) == EvidenceKind.demo_fixture]

        if demo_rows and not independent:
            findings.append(
                self._finding(
                    "demo_data_ignored", "Demo data ignored", "info",
                    f"{len(demo_rows)} demo fixture row(s) match this payment and were deliberately ignored.",
                )
            )

        if not independent:
            if agent5_rows:
                findings.append(
                    self._finding(
                        "agent5_authored_evidence", "Only Agent 5's own record exists", "unverified",
                        f"The only settlement record for {reference} was written by Agent 5 itself "
                        "(it creates a bank row on SUCCESS). That is not independent evidence, so "
                        "settlement cannot be verified from it.",
                    )
                )
            else:
                findings.append(
                    self._finding(
                        "no_bank_evidence", "No bank evidence", "unverified",
                        f"No bank record matches {reference}. The payment is claimed settled by "
                        "Agent 5 but no independent evidence corroborates it.",
                    )
                )
            return self._result(
                result_id, case_id, invoice_number, attempt["attempt_id"], reference,
                ReconciliationOutcome.reconciliation_required,
                expected_amount=expected, currency=currency, findings=findings,
            )

        # Independent evidence exists: compare amounts.
        settled_total = sum((Decimal(r["amount"]) for r in independent), Decimal("0"))
        matched = independent[-1]["reference"]
        findings.append(
            self._finding(
                "independent_evidence", "Independent bank evidence found", "passed",
                f"{len(independent)} bank record(s) corroborate {reference}, totalling {settled_total} {currency}.",
            )
        )
        if expected is not None and settled_total != expected:
            findings.append(
                self._finding(
                    "amount_mismatch", "Amount mismatch", "failed",
                    f"The payment recorded {expected} {currency} but bank evidence shows "
                    f"{settled_total} {currency}.",
                )
            )
            return self._result(
                result_id, case_id, invoice_number, attempt["attempt_id"], reference,
                ReconciliationOutcome.discrepancy_detected, matched_reference=matched,
                expected_amount=expected, settled_amount=settled_total, currency=currency,
                independent_evidence=True, findings=findings,
            )

        findings.append(
            self._finding(
                "settlement_matched", "Settlement verified", "passed",
                f"Bank evidence confirms {settled_total} {currency} settled under {matched}.",
            )
        )
        return self._result(
            result_id, case_id, invoice_number, attempt["attempt_id"], reference,
            ReconciliationOutcome.settlement_verified, matched_reference=matched,
            expected_amount=expected, settled_amount=settled_total, currency=currency,
            confidence=1.0, independent_evidence=True, findings=findings,
        )

    # ------------------------------------------------------------------
    def _bank_rows_for(self, reference: str, invoice_number: str | None) -> list[dict]:
        rows = self.repository.bank_rows()
        return [
            row for row in rows
            if (row.get("reference") or "").casefold() == reference.casefold()
            or (
                invoice_number
                and row.get("invoice_number")
                and (row["invoice_number"] or "").casefold() == invoice_number.casefold()
            )
        ]

    def _bank_matches(self, invoice_number, attempt, allow_demo: bool) -> list[dict]:
        amount = attempt.get("amount")
        currency = attempt.get("currency")
        rows = self.repository.bank_rows()
        out = []
        for row in rows:
            if str(row.get("amount")) != str(amount) or row.get("currency") != currency:
                continue
            if not allow_demo and row.get("is_demo"):
                continue
            out.append(row)
        return out

    def _evidence_kind(self, row: dict) -> EvidenceKind:
        """Classify a bank row by *who wrote it*, not by its reference prefix.

        Agent 5 derives its settlement reference from the idempotency key, so a
        genuine bank statement can legitimately quote the same ``SP-*`` value.
        What makes a row Agent 5's own work is that Agent 5 created it: its
        transaction is the very one linked to the payment attempt.
        """

        if row.get("is_demo"):
            return EvidenceKind.demo_fixture
        if (row.get("source") or "") == "customer_report":
            return EvidenceKind.customer_report
        # Rows Agent 5 wrote are marked as such by the migration/backfill.
        if (row.get("source") or "") == "agent5_settlement":
            return EvidenceKind.agent5_execution
        # A row whose transaction is linked to a payment attempt was produced by
        # that execution.
        transaction_id = row.get("transaction_id")
        if transaction_id and transaction_id in self._agent5_transaction_ids():
            return EvidenceKind.agent5_execution
        return EvidenceKind.bank_statement

    def _agent5_transaction_ids(self) -> set[str]:
        """transaction_ids that Agent 5 itself recorded for payment attempts."""

        cached = getattr(self, "_txn_cache", None)
        if cached is not None:
            return cached
        ids: set[str] = set()
        conn = self.repository._connect()
        try:
            present = {r[1] for r in conn.execute("PRAGMA table_info(payment_attempts)")}
            if "transaction_id" in present:
                for row in conn.execute(
                    "SELECT transaction_id FROM payment_attempts WHERE transaction_id IS NOT NULL"
                ):
                    ids.add(row[0])
        finally:
            conn.close()
        self._txn_cache = ids
        return ids

    # ------------------------------------------------------------------
    def _finding(self, code: str, label: str, status: str, detail: str) -> ReconFinding:
        return ReconFinding(code=code, label=label, status=status, detail=detail)

    def _result(
        self,
        result_id: str,
        case_id: str | None,
        invoice_number: str | None,
        attempt_id: str | None,
        reference: str | None,
        outcome: ReconciliationOutcome,
        *,
        matched_reference: str | None = None,
        expected_amount: Decimal | None = None,
        settled_amount: Decimal | None = None,
        currency: str | None = None,
        confidence: float = 0.0,
        independent_evidence: bool = False,
        findings: list[ReconFinding] | None = None,
    ) -> ReconciliationResult:
        result = ReconciliationResult(
            result_id=result_id,
            case_id=case_id,
            invoice_number=invoice_number,
            payment_attempt_id=attempt_id,
            provider_reference=reference,
            outcome=outcome,
            matched_reference=matched_reference,
            expected_amount=expected_amount,
            settled_amount=settled_amount,
            currency=currency,
            confidence=confidence,
            independent_evidence=independent_evidence,
            findings=findings or [],
        )
        try:
            self.repository.save_result(result)
        except sqlite3.Error:
            # Persisting the finding must never break the workflow step.
            pass
        return result
