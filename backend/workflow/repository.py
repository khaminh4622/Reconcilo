"""Persistence for workflow cases, approvals and synthetic bank records.

Reuses the SQLite database Agent 3 already writes to, adding new tables without
touching the existing ``document_records`` and ``customers`` tables, so all
prior functionality keeps working.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .schemas import (
    ApprovalRecord,
    AgentId,
    CaseStatus,
    CaseSummary,
    Intent,
    PaymentAttempt,
    SyntheticTransaction,
    WorkflowCase,
    WorkflowType,
)


def _stable_transaction_id(txn: "SyntheticTransaction") -> str:
    """Deterministic id so seeding the same bank record twice is a no-op."""

    material = "|".join(
        [
            (txn.reference or "").casefold(),
            f"{txn.amount:.2f}",
            (txn.currency or "").upper(),
            txn.direction,
            (txn.value_date or ""),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def summarise_for_human(case: WorkflowCase) -> tuple[str, list[str], str]:
    """Explain why a case needs a human, what is wrong, and what to do next.

    Returns (why, problems, next_action). Everything is derived from the case's
    own reports and validation, so the case list is enough to triage a case
    without opening it.
    """

    problems: list[str] = []
    why = ""
    next_action = ""

    analysis = case.analysis or {}
    validation = analysis.get("validation") or {}
    problem = analysis.get("detected_problem") or {}

    # 1. What the customer reported.
    ptype = (problem.get("type") or "none_identified").replace("_", " ")
    primary = (problem.get("primary_problem") or "unknown").replace("_", " ")
    if problem.get("type") and problem.get("type") != "none_identified":
        why = f"The customer reported: {ptype}."
        problems.append(f"Reported problem: {ptype} (primary: {primary})")

    # 2. What deterministic validation found.
    checks = validation.get("validation_findings") or []
    for check in checks:
        if check.get("status") == "failed":
            problems.append(f"Failed check - {check.get('label')}: {check.get('explanation')}")
        elif check.get("status") == "unverified":
            problems.append(f"Unconfirmed - {check.get('label')}: {check.get('explanation')}")

    missing = validation.get("missing_fields") or []
    if missing:
        problems.append("Missing required information: " + ", ".join(missing))

    calc = validation.get("calculated") or {}
    if calc.get("potential_duplicate_amount"):
        problems.append(
            f"Possible duplicate payment: {calc['potential_duplicate_amount']} {case.currency or ''}".strip()
        )
    if calc.get("potential_overpayment"):
        problems.append(
            f"Possible overpayment: {calc['potential_overpayment']} {case.currency or ''}".strip()
        )

    # 3. What the specialist agents concluded.
    for report in case.reports:
        if report.requires_human or not report.resolved:
            why = why or report.summary
            next_action = next_action or (report.next_action or "")
            short = report.agent_name.split("·")[-1].strip() or report.agent_name
            for finding in report.findings:
                if finding.status in {"failed", "unverified"}:
                    problems.append(f"{short} - {finding.detail}")

    # 4. Status-specific guidance.
    if case.status == CaseStatus.awaiting_approval:
        why = why or "The invoice is valid and needs a human to approve payment."
        next_action = next_action or (
            f"Approve {case.amount} {case.currency or ''} to {case.vendor_name or 'the vendor'}, "
            f"or reject it."
        ).strip()
    elif case.status == CaseStatus.pending:
        # A pending payment needs a human to check the bank. This guidance is
        # authoritative: an earlier agent's summary must not replace it.
        why = "The payment is not yet confirmed: the bank has not settled it."
        # Authoritative for this status: a pending payment must never be retried.
        next_action = (
            "Check the payment provider for the result of the existing attempt before doing anything "
            "else. Do not retry: a pending payment may still settle."
        )
    elif case.status == CaseStatus.requires_review:
        if not why:
            if case.intent == Intent.unknown:
                why = "Agent 3 could not classify what the customer is asking for, or found the document unsound."
            else:
                why = case.outcome or "The specialist agent could not resolve the case."
        if not next_action:
            if case.intent == Intent.unknown:
                next_action = (
                    "Read the document and confirm what the customer wants, or ask them for a clearer document."
                )
            elif case.intent in {Intent.duplicate_payment, Intent.duplicate_invoice}:
                next_action = (
                    "Confirm with the bank whether the suspected duplicate payment settled, then decide "
                    "whether a corrective refund is owed. Nothing is refunded automatically."
                )
            elif case.intent in {Intent.bank_reconciliation, Intent.payment_status_dispute}:
                next_action = (
                    "Reconcile the reported payment against the bank statement. If it settled, apply it "
                    "to the invoice; if not, ask the customer for proof of payment."
                )
            elif case.intent == Intent.payment_failure:
                next_action = (
                    "Confirm the earlier attempt did not settle, then re-request approval if a new payment "
                    "is genuinely needed."
                )
            else:
                next_action = "Review the case and decide the next step."
    elif case.status == CaseStatus.failed:
        next_action = next_action or "The payment failed or was rejected. Resolve the cause before any new attempt."
    elif case.status == CaseStatus.completed:
        next_action = "No action required."

    problems = list(dict.fromkeys(problems))
    return why, problems, next_action


class WorkflowRepository:
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
                CREATE TABLE IF NOT EXISTS workflow_cases (
                    case_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workflow TEXT,
                    intent TEXT NOT NULL,
                    customer_name TEXT,
                    vendor_name TEXT,
                    invoice_number TEXT,
                    amount TEXT,
                    currency TEXT,
                    requires_human INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT,
                    case_json TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_cases_status ON workflow_cases(status, updated_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_cases_invoice ON workflow_cases(invoice_number, vendor_name)"
            )
            # Human approvals are stored separately so they can be validated
            # against the exact payment terms at execution time.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_approvals (
                    approval_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    invoice_number TEXT,
                    payee TEXT,
                    amount TEXT,
                    currency TEXT,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    approval_json TEXT NOT NULL
                )
                """
            )
            # Every execution attempt. The idempotency key is unique, which is
            # what prevents the same approved payment being paid twice.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    approval_id TEXT,
                    invoice_number TEXT,
                    payee TEXT,
                    amount TEXT,
                    currency TEXT,
                    status TEXT NOT NULL,
                    failure_reason TEXT,
                    transaction_id TEXT,
                    attempted_at TEXT NOT NULL,
                    attempt_json TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_attempts_key ON payment_attempts(idempotency_key)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_attempts_invoice ON payment_attempts(invoice_number, status)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS synthetic_bank_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    reference TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    counterparty TEXT,
                    status TEXT NOT NULL,
                    value_date TEXT,
                    source TEXT NOT NULL,
                    transaction_json TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_bank_ref ON synthetic_bank_transactions(reference)")
            conn.commit()
        finally:
            conn.close()

    # -- cases -------------------------------------------------------------
    def save_case(self, case: WorkflowCase) -> None:
        conn = self._connect()
        try:
            has_owner = "owner_account_id" in {
                row[1] for row in conn.execute("PRAGMA table_info(workflow_cases)")
            }
            # Databases created before accounts existed simply skip the column.
            owner_column = ", owner_account_id" if has_owner else ""
            owner_slot = ", ?" if has_owner else ""
            owner_update = (
                ",\n                    owner_account_id = COALESCE("
                "excluded.owner_account_id, workflow_cases.owner_account_id)"
                if has_owner else ""
            )
            conn.execute(
                f"""
                INSERT INTO workflow_cases (
                    case_id, created_at, updated_at, status, workflow, intent, customer_name,
                    vendor_name, invoice_number, amount, currency, requires_human, outcome,
                    case_json{owner_column}
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?{owner_slot})
                ON CONFLICT(case_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    status = excluded.status,
                    workflow = excluded.workflow,
                    intent = excluded.intent,
                    customer_name = excluded.customer_name,
                    vendor_name = excluded.vendor_name,
                    invoice_number = excluded.invoice_number,
                    amount = excluded.amount,
                    currency = excluded.currency,
                    requires_human = excluded.requires_human,
                    outcome = excluded.outcome,
                    case_json = excluded.case_json{owner_update}
                """,
                (
                    case.case_id,
                    case.created_at,
                    case.updated_at,
                    case.status.value,
                    case.workflow.value if case.workflow else None,
                    case.intent.value,
                    case.customer_name,
                    case.vendor_name,
                    case.invoice_number,
                    str(case.amount) if case.amount is not None else None,
                    case.currency,
                    1 if case.requires_human else 0,
                    case.outcome,
                    case.model_dump_json(),
                    *([case.owner_account_id] if has_owner else []),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_case(self, case_id: str) -> WorkflowCase | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT case_json FROM workflow_cases WHERE case_id = ?", (case_id,)
            ).fetchone()
        finally:
            conn.close()
        return WorkflowCase.model_validate_json(row["case_json"]) if row else None

    def list_cases(self, limit: int = 50) -> list[CaseSummary]:
        conn = self._connect()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_cases)")}
            owner_select = "owner_account_id" if "owner_account_id" in columns else "NULL AS owner_account_id"
            rows = conn.execute(
                f"SELECT case_json, {owner_select} FROM workflow_cases "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        summaries: list[CaseSummary] = []
        for row in rows:
            case = WorkflowCase.model_validate_json(row["case_json"])
            why, problems, next_action = summarise_for_human(case)
            summaries.append(
                CaseSummary(
                    case_id=case.case_id,
                    created_at=case.created_at,
                    updated_at=case.updated_at,
                    status=case.status,
                    workflow=case.workflow,
                    intent=case.intent,
                    customer_name=case.customer_name,
                    invoice_number=case.invoice_number,
                    vendor_name=case.vendor_name,
                    amount=case.amount,
                    currency=case.currency,
                    outcome=case.outcome,
                    requires_human=case.requires_human,
                    owner_account_id=row["owner_account_id"],
                    why=why,
                    problems=problems,
                    next_action=next_action,
                    path=[
                        run.agent.value
                        for run in sorted(case.agent_runs, key=lambda r: r.order)
                        if run.status.value != "skipped"
                    ],
                )
            )
        return summaries

    def find_invoice_payments(self, invoice_number: str | None, vendor: str | None) -> list[dict]:
        """Return prior successful payments for the same invoice and payee.

        Used by Agent 1 and Agent 5 to refuse paying an invoice twice.
        """

        if not invoice_number:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT attempt_json FROM payment_attempts
                WHERE invoice_number = ? AND status IN ('SUCCESS', 'PENDING')
                """,
                (invoice_number,),
            ).fetchall()
        finally:
            conn.close()
        attempts = [json.loads(row["attempt_json"]) for row in rows]
        if vendor:
            same_vendor = [a for a in attempts if (a.get("payee") or "").strip().casefold() == vendor.strip().casefold()]
            if same_vendor:
                return same_vendor
        return attempts

    # -- approvals ---------------------------------------------------------
    def save_approval(self, case_id: str, approval: ApprovalRecord) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO workflow_approvals (
                    approval_id, case_id, approved_by, approved_at, invoice_number,
                    payee, amount, currency, revoked, approval_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    case_id,
                    approval.approved_by,
                    approval.approved_at,
                    approval.invoice_number,
                    approval.payee,
                    str(approval.amount) if approval.amount is not None else None,
                    approval.currency,
                    1 if approval.revoked else 0,
                    approval.model_dump_json(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT approval_json FROM workflow_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        finally:
            conn.close()
        return ApprovalRecord.model_validate_json(row["approval_json"]) if row else None

    # -- payment attempts --------------------------------------------------
    def find_attempt_by_key(self, idempotency_key: str) -> PaymentAttempt | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT attempt_json FROM payment_attempts WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        finally:
            conn.close()
        return PaymentAttempt.model_validate_json(row["attempt_json"]) if row else None

    def list_attempts(self, case_id: str | None = None) -> list[PaymentAttempt]:
        conn = self._connect()
        try:
            if case_id:
                rows = conn.execute(
                    "SELECT attempt_json FROM payment_attempts WHERE case_id = ? ORDER BY attempted_at",
                    (case_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT attempt_json FROM payment_attempts ORDER BY attempted_at DESC"
                ).fetchall()
        finally:
            conn.close()
        return [PaymentAttempt.model_validate_json(row["attempt_json"]) for row in rows]

    def save_attempt(self, case_id: str, attempt: PaymentAttempt) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO payment_attempts (
                    attempt_id, case_id, idempotency_key, approval_id, invoice_number, payee,
                    amount, currency, status, failure_reason, transaction_id, attempted_at, attempt_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.attempt_id,
                    case_id,
                    attempt.idempotency_key,
                    attempt.approval_id,
                    attempt.invoice_number,
                    attempt.payee,
                    str(attempt.amount) if attempt.amount is not None else None,
                    attempt.currency,
                    attempt.status,
                    attempt.failure_reason,
                    attempt.transaction_id,
                    attempt.attempted_at,
                    attempt.model_dump_json(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # -- synthetic bank ----------------------------------------------------
    def seed_bank_transactions(self, transactions: Iterable[SyntheticTransaction]) -> int:
        added = 0
        conn = self._connect()
        try:
            for txn in transactions:
                # Persist the derived id so lookups return a populated record.
                txn.transaction_id = txn.transaction_id or _stable_transaction_id(txn)
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO synthetic_bank_transactions (
                        transaction_id, reference, amount, currency, direction,
                        counterparty, status, value_date, source, transaction_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        txn.transaction_id,
                        txn.reference,
                        str(txn.amount),
                        txn.currency,
                        txn.direction,
                        txn.counterparty,
                        txn.status,
                        txn.value_date,
                        txn.source,
                        txn.model_dump_json(),
                    ),
                )
                added += cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return added

    def find_bank_transactions(
        self,
        reference: str | None = None,
        amount: object | None = None,
        counterparty: str | None = None,
        value_date: str | None = None,
    ) -> list[SyntheticTransaction]:
        clauses: list[str] = []
        params: list[object] = []
        if reference:
            clauses.append("reference = ? COLLATE NOCASE")
            params.append(reference)
        if amount is not None:
            clauses.append("amount = ?")
            params.append(str(amount))
        if counterparty:
            clauses.append("counterparty = ? COLLATE NOCASE")
            params.append(counterparty)
        if value_date:
            clauses.append("value_date = ?")
            params.append(value_date)
        if not clauses:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT transaction_json FROM synthetic_bank_transactions WHERE "
                + " OR ".join(clauses),
                params,
            ).fetchall()
        finally:
            conn.close()
        return [SyntheticTransaction.model_validate_json(row["transaction_json"]) for row in rows]

    def list_bank_transactions(self, limit: int = 100) -> list[SyntheticTransaction]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT transaction_json FROM synthetic_bank_transactions ORDER BY value_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [SyntheticTransaction.model_validate_json(row["transaction_json"]) for row in rows]

    def record_bank_transaction(self, txn: SyntheticTransaction) -> None:
        self.seed_bank_transactions([txn])
