"""Agent 2 bank reconciliation integration tests.

Each test builds an isolated matrix database containing:
  * an expected payment written the way Agent 5 records one, and
  * an imported simulated bank statement.

The scenarios mirror the specification exactly: $1,250 expected against bank
credits of $1,250, $1,000 and $1,400, plus duplicates, wrong references,
double imports and claims that have no bank evidence at all.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from workflow.bank_importer import (
    BankImportError,
    BankTransactionStore,
    document_hash,
    parse_csv,
    parse_pdf_text,
)
from workflow.bank_reconciliation import MatchType, ReconciliationService
from workflow.migrations import migrate
from workflow.obligations import ObligationRepository
from workflow.repository import WorkflowRepository


INVOICE = "INV-A2-WORKFLOW"
VENDOR = "Acme Supply Ltd"
REFERENCE = "SP-REF0001XYZ"
EXPECTED = "1250.00"

HEADER = (
    "Transaction ID,Payment Reference,Transaction Date,Description,"
    "Counterparty,Amount,Currency,Direction\n"
)


def matrix(workspace_tmp_path, name: str = "matrix") -> Path:
    """An isolated database with one expected payment of 1250.00 USD."""

    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    WorkflowRepository(database)
    migrate(database, take_backup=False)
    ObligationRepository(database)

    conn = sqlite3.connect(database)
    try:
        conn.execute(
            """
            INSERT INTO payment_attempts (
                attempt_id, case_id, idempotency_key, invoice_number, payee, amount,
                currency, status, attempted_at, attempt_json, provider_reference
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("attempt-1", "case-1", "key-1", INVOICE, VENDOR, EXPECTED, "USD",
             "SUCCESS", "2026-10-20T10:00:00+00:00", "{}", REFERENCE),
        )
        conn.execute(
            """
            INSERT INTO payable_obligations (
                obligation_id, obligation_key, invoice_number, vendor, customer,
                currency, amount, state, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            ("obl-1", "okey-1", INVOICE, VENDOR, "Cust Co", "USD", EXPECTED, "PAID",
             "2026-01-01", "2026-01-01"),
        )
        conn.commit()
    finally:
        conn.close()
    return database


def import_statement(database: Path, rows: str, name: str = "statement.csv") -> dict:
    content = (HEADER + rows).encode("utf-8")
    parsed = parse_csv(content)
    return BankTransactionStore(database).import_rows(
        parsed, filename=name, source_type="csv", digest=document_hash(content)
    )


def reconcile(database: Path):
    report = ReconciliationService(database).reconcile()
    row = next(r for r in report.rows if r.invoice_number == INVOICE)
    return report, row


# ---------------------------------------------------------------------------
# 1. $1,250 expected / $1,250 received -> MATCHED
# ---------------------------------------------------------------------------
def test_exact_payment_is_matched(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    import_statement(
        database,
        f"B1,{REFERENCE},2026-10-21,Wire received,{VENDOR},1250.00,USD,credit\n",
    )
    _, row = reconcile(database)

    assert row.match_type == MatchType.matched
    assert row.final_status == "SETTLEMENT_VERIFIED"
    assert row.expected_amount == Decimal("1250.00")
    assert row.bank_received == Decimal("1250.00")
    assert row.difference == Decimal("0.00")
    assert row.bank_transaction_id == "B1"


# ---------------------------------------------------------------------------
# 2. $1,250 expected / $1,000 received -> PARTIAL_PAYMENT
# ---------------------------------------------------------------------------
def test_partial_payment(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    import_statement(
        database,
        f"B1,{REFERENCE},2026-10-21,Wire received,{VENDOR},1000.00,USD,credit\n",
    )
    _, row = reconcile(database)

    assert row.match_type == MatchType.partial_payment
    assert row.difference == Decimal("-250.00")
    assert row.final_status == "PARTIAL"
    assert any(f.code == "partial_payment" for f in row.findings)


# ---------------------------------------------------------------------------
# 3. $1,250 expected / $1,400 received -> OVERPAYMENT
# ---------------------------------------------------------------------------
def test_overpayment(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    import_statement(
        database,
        f"B1,{REFERENCE},2026-10-21,Wire received,{VENDOR},1400.00,USD,credit\n",
    )
    _, row = reconcile(database)

    assert row.match_type == MatchType.overpayment
    assert row.difference == Decimal("150.00")
    assert row.final_status == "OVERPAID"


# ---------------------------------------------------------------------------
# 4. $1,250 expected / no bank transaction
# ---------------------------------------------------------------------------
def test_no_bank_transaction_is_missing_or_unverified(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    report, row = reconcile(database)

    assert row.match_type in {MatchType.missing_payment, MatchType.needs_human_review}
    assert row.bank_received is None
    assert row.final_status != "SETTLEMENT_VERIFIED"
    assert report.bank_transaction_count == 0


def test_invoice_without_any_payment_is_missing(workspace_tmp_path):
    """Nothing expected and nothing received -> MISSING_PAYMENT."""

    database = matrix(workspace_tmp_path)
    conn = sqlite3.connect(database)
    conn.execute("UPDATE payment_attempts SET provider_reference = NULL, status = 'PENDING'")
    conn.commit()
    conn.close()

    _, row = reconcile(database)
    assert row.match_type == MatchType.missing_payment


# ---------------------------------------------------------------------------
# 5. Duplicate bank lines
# ---------------------------------------------------------------------------
def test_duplicate_import_line_is_flagged_not_settled(workspace_tmp_path):
    """The same bank line twice is duplicate evidence, not two settlements."""

    database = matrix(workspace_tmp_path)
    result = import_statement(
        database,
        f"B1,{REFERENCE},2026-10-21,Wire,{VENDOR},1250.00,USD,credit\n"
        f"B1,{REFERENCE},2026-10-21,Wire,{VENDOR},1250.00,USD,credit\n",
    )
    # Both lines are stored so the problem stays visible.
    assert result["imported"] == 2
    assert result["duplicates_skipped"] == 1

    _, row = reconcile(database)
    assert row.match_type == MatchType.duplicate_bank_transaction
    assert row.final_status != "SETTLEMENT_VERIFIED"
    assert any(f.code == "duplicate_import" for f in row.findings)


def test_two_distinct_settlements_are_a_duplicate_payment(workspace_tmp_path):
    """Two separate transfers under the same reference is a real over-payment."""

    database = matrix(workspace_tmp_path)
    import_statement(
        database,
        f"B1,{REFERENCE},2026-10-21,Wire,{VENDOR},1250.00,USD,credit\n"
        f"B2,{REFERENCE},2026-10-21,Wire,{VENDOR},1250.00,USD,credit\n",
    )
    _, row = reconcile(database)

    assert row.match_type == MatchType.duplicate_bank_transaction
    assert row.final_status == "DUPLICATE_PAYMENT"
    assert row.bank_received == Decimal("2500.00")
    assert any(f.code == "duplicate_settlement" for f in row.findings)


# ---------------------------------------------------------------------------
# 6. Incorrect payment reference
# ---------------------------------------------------------------------------
def test_wrong_reference_does_not_verify(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    import_statement(
        database,
        f"B1,SP-TOTALLY-DIFFERENT,2026-10-21,Wire,{VENDOR},1250.00,USD,credit\n",
    )
    _, row = reconcile(database)

    assert row.match_type in {
        MatchType.unmatched_bank_transaction, MatchType.needs_human_review
    }
    assert row.final_status != "SETTLEMENT_VERIFIED"
    assert any(f.code in {"insufficient_evidence", "reference_check"} for f in row.findings)


def test_matching_amount_alone_is_not_enough(workspace_tmp_path):
    """Equal amount + wrong reference + unknown counterparty must not verify."""

    database = matrix(workspace_tmp_path)
    import_statement(
        database,
        "B1,SP-UNRELATED0001,2026-10-21,Unrelated,Someone Else Ltd,1250.00,USD,credit\n",
    )
    _, row = reconcile(database)

    assert row.expected_amount == row.bank_received
    assert row.final_status != "SETTLEMENT_VERIFIED"


def test_currency_mismatch(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    import_statement(
        database,
        f"B1,{REFERENCE},2026-10-21,Wire,{VENDOR},1250.00,EUR,credit\n",
    )
    _, row = reconcile(database)

    assert row.match_type == MatchType.currency_mismatch
    assert row.final_status == "NEEDS_HUMAN_REVIEW"


# ---------------------------------------------------------------------------
# 7. Importing the same statement twice
# ---------------------------------------------------------------------------
def test_same_statement_cannot_be_imported_twice(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    rows = f"B1,{REFERENCE},2026-10-21,Wire,{VENDOR},1250.00,USD,credit\n"

    first = import_statement(database, rows)
    second = import_statement(database, rows)

    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["skipped_duplicate_file"] is True
    assert len(BankTransactionStore(database).list_transactions()) == 1


def test_slightly_different_file_with_same_rows_is_deduped(workspace_tmp_path):
    """A re-exported statement has a different hash but the same transactions."""

    database = matrix(workspace_tmp_path)
    rows = f"B1,{REFERENCE},2026-10-21,Wire,{VENDOR},1250.00,USD,credit\n"
    import_statement(database, rows)

    # Re-export with a trailing blank line: different bytes, same transactions.
    different = (HEADER + rows + "\n").encode("utf-8")
    parsed = parse_csv(different)
    Store = BankTransactionStore(database)
    result = Store.import_rows(
        parsed, filename="reexport.csv", source_type="csv",
        digest=document_hash(different),
    )
    assert result["skipped_duplicate_file"] is False
    # The row is stored but flagged, so the repeat stays visible to Agent 2.
    assert result["duplicates_skipped"] == 1
    stored = Store.list_transactions()
    assert len(stored) == 2
    assert sum(1 for r in stored if r["duplicate_of"]) == 1


# ---------------------------------------------------------------------------
# 8. Customer claim without bank evidence -> UNVERIFIED
# ---------------------------------------------------------------------------
def test_claim_without_bank_evidence_is_unverified(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    report, row = reconcile(database)

    assert row.final_status == "UNVERIFIED"
    assert row.bank_received is None
    assert report.matched == 0
    # The invoice is never promoted to PAID by reconciliation alone.
    assert row.final_status != "SETTLEMENT_VERIFIED"


def test_reconciliation_never_marks_invoice_paid(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    conn = sqlite3.connect(database)
    conn.execute("UPDATE payable_obligations SET state = 'UNPAID'")
    conn.commit()
    conn.close()

    import_statement(
        database,
        f"B1,{REFERENCE},2026-10-21,Wire,{VENDOR},1250.00,USD,credit\n",
    )
    reconcile(database)

    conn = sqlite3.connect(database)
    state = conn.execute(
        "SELECT state FROM payable_obligations WHERE invoice_number = ?", (INVOICE,)
    ).fetchone()[0]
    conn.close()
    # Verification is reported; state changes remain an explicit human action.
    assert state == "UNPAID"


# ---------------------------------------------------------------------------
# Importer validation
# ---------------------------------------------------------------------------
def test_csv_without_amount_column_is_rejected():
    with pytest.raises(BankImportError) as excinfo:
        parse_csv(b"Reference,Date\nSP-1,2026-01-01\n")
    assert "amount" in str(excinfo.value).lower()


def test_csv_row_with_bad_amount_is_rejected():
    with pytest.raises(BankImportError):
        parse_csv(b"Reference,Amount,Currency\nSP-1,not-a-number,USD\n")


def test_pdf_text_statement_is_parsed():
    text = (
        "SIMULATED BANK STATEMENT\n"
        f"2026-10-21  Wire received  {REFERENCE}  {VENDOR}  1250.00 USD\n"
        "2026-10-22  Fee  TRF-990215  Bank charge  12.00 USD\n"
    )
    rows = parse_pdf_text(text)
    assert rows
    assert any(r.payment_reference == REFERENCE for r in rows)


def test_bank_evidence_is_labelled_simulated(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    import_statement(
        database,
        f"B1,{REFERENCE},2026-10-21,Wire,{VENDOR},1250.00,USD,credit\n",
    )
    rows = BankTransactionStore(database).list_transactions()
    assert all(row["simulated"] == 1 for row in rows)


def test_outgoing_debits_are_not_counted_as_received(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    import_statement(
        database,
        f"B1,{REFERENCE},2026-10-21,Outgoing,{VENDOR},-1250.00,USD,debit\n",
    )
    store = BankTransactionStore(database)
    assert store.list_transactions() == []
    assert len(store.list_transactions(include_outgoing=True)) == 1

    _, row = reconcile(database)
    assert row.final_status != "SETTLEMENT_VERIFIED"


def test_unmatched_bank_credit_is_reported(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    import_statement(
        database,
        f"B1,{REFERENCE},2026-10-21,Wire,{VENDOR},1250.00,USD,credit\n"
        "B9,SP-NOBODY0001,2026-10-22,Unknown payer,Stranger Ltd,99.00,USD,credit\n",
    )
    report, _ = reconcile(database)
    assert report.unmatched_bank == 1
    assert report.unmatched_bank_transactions[0]["payment_reference"] == "SP-NOBODY0001"


def test_reconciliation_report_is_persisted(workspace_tmp_path):
    database = matrix(workspace_tmp_path)
    report, _ = reconcile(database)

    conn = sqlite3.connect(database)
    stored = conn.execute(
        "SELECT matched, bank_transaction_count FROM bank_reconciliation_runs WHERE report_id = ?",
        (report.report_id,),
    ).fetchone()
    conn.close()
    assert stored is not None
