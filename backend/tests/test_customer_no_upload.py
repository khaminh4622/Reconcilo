"""The customer workflow must never require a bank statement upload.

Bank evidence is produced automatically by GhostLedger's payment simulator. This
module pins that behaviour so the manual import can never creep back into the
customer-facing flow.

The underlying reconciliation engine, database tables and historical records are
deliberately preserved - only the customer-facing manual import is gone.
"""

from __future__ import annotations

import re
from pathlib import Path

from workflow.accounts import ROLE_BANK_ADMIN
from workflow.mock_bank import MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import CaseStatus

from test_workflow import build_analysis


STATIC = Path(__file__).resolve().parents[1] / "static" / "index.html"
VENDOR = "Summit Cloud Services LLC"
CUSTOMER = "Cedar Valley Design LLC"


def admin_headers(app) -> dict:
    """Sign in as a bank administrator for the internal ledger endpoints."""

    from workflow.accounts import ROLE_BANK_ADMIN

    email = "admin@bank.example"
    password = "adminpass123"
    try:
        app.state.accounts.sign_up(email=email, password=password, role=ROLE_BANK_ADMIN)
    except Exception:  # noqa: BLE001 - already created by an earlier test
        pass
    _account, token = app.state.accounts.sign_in(email=email, password=password)
    return {"Authorization": f"Bearer {token}"}


def build(workspace_tmp_path, name: str):
    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()
    repository = WorkflowRepository(database)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.execution.bank = MockBank(database)
    return orchestrator, database


def open_case(orchestrator, invoice="INV-RO-1", **analysis):
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=CUSTOMER,
        total="1250.00", subtotal="1250.00", tax="0.00", **analysis,
    )
    return orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))


# ---------------------------------------------------------------------------
# The manual import UI is gone
# ---------------------------------------------------------------------------
def test_customer_ui_has_no_bank_statement_upload():
    html = STATIC.read_text(encoding="utf-8")
    for forbidden in (
        'id="bankFile"',
        'id="bankAuthorized"',
        'id="bankImport"',
        'id="bankReconcile"',
        "Import bank statement",
        "Synthetic bank statement",
    ):
        assert forbidden not in html, f"customer UI still contains {forbidden}"


def test_customer_ui_has_no_import_javascript():
    html = STATIC.read_text(encoding="utf-8")
    for forbidden in (
        "importBankStatement",
        "renderBankImport",
        "runReconciliation(",
        "initBankPanel",
        "/bank/import",
    ):
        assert forbidden not in html, f"customer JS still contains {forbidden}"


def test_customer_ui_shows_a_readonly_ledger_panel():
    html = STATIC.read_text(encoding="utf-8")
    assert "Mock Bank Ledger" in html
    assert "SIMULATED" in html and "NO REAL MONEY" in html
    # The panel is read-only: display only, with no write controls.
    assert "loadReadonlyLedger" in html
    assert "renderReadonlyLedger" in html


def test_readonly_panel_has_every_required_column():
    html = STATIC.read_text(encoding="utf-8")
    block = html[html.index("function renderReadonlyLedger") : html.index("function initReadonlyLedgerPanel")]
    for column in ("Invoice", "Reference", "Expected", "Received", "Currency", "Status", "Date"):
        assert column in block, f"read-only panel is missing the {column} column"


def test_customer_ui_offers_one_main_action():
    html = STATIC.read_text(encoding="utf-8")
    # Only the customer PDF upload remains as an input.
    inputs = re.findall(r'<input id="(\w+)" type="file"', html)
    assert inputs == ["file"], f"unexpected file inputs: {inputs}"


# ---------------------------------------------------------------------------
# The backend still produces bank evidence automatically
# ---------------------------------------------------------------------------
def test_payment_creates_bank_record_with_no_upload(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "auto")
    case = open_case(orchestrator)
    paid = orchestrator.approve(case.case_id, approved_by="approver@example.test")

    assert paid.status == CaseStatus.completed
    entries = orchestrator.execution.bank.ledger()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["invoice_number"] == "INV-RO-1"
    assert entry["received_amount"] == "1250.00"
    assert entry["status"] == "SETTLED"

    # Nothing was ever imported.
    from workflow.bank_importer import BankTransactionStore

    assert BankTransactionStore(database).list_transactions() == []


def test_reconciliation_engine_and_tables_are_preserved(workspace_tmp_path):
    """Removing the UI must not remove the reconciliation machinery."""

    orchestrator, database = build(workspace_tmp_path, "preserved")
    # Apply the schema migration, as the application does on startup.
    from workflow.migrations import migrate

    migrate(database, take_backup=False)
    open_case(orchestrator)

    from workflow.bank_reconciliation import ReconciliationService

    # The engine still exists and still works.
    service = ReconciliationService(database)
    assert hasattr(service, "reconcile")
    assert hasattr(service, "recommendation")

    # Historical tables remain available.
    import sqlite3

    conn = sqlite3.connect(database)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    for table in ("bank_transactions", "bank_imports", "mock_bank_ledger", "reconciliation_results"):
        assert table in tables, f"{table} must be preserved"


# ---------------------------------------------------------------------------
# Only the payment simulator may create bank transactions
# ---------------------------------------------------------------------------
def test_agent2_cannot_create_bank_records(workspace_tmp_path):
    orchestrator, database = build(workspace_tmp_path, "readonly")
    open_case(orchestrator)

    from workflow.bank_reconciliation import ReconciliationService

    bank = orchestrator.execution.bank
    before = bank.count()
    service = ReconciliationService(database)
    service.reconcile()
    service.recommendation()
    assert bank.count() == before, "reconciliation must never create bank records"


def test_customer_claimed_payment_does_not_create_a_settlement(workspace_tmp_path):
    """A PDF claiming payment must not conjure a bank settlement."""

    orchestrator, database = build(workspace_tmp_path, "claim")
    # The customer reports a payment problem - no money moves.
    case = open_case(
        orchestrator,
        problem="payment_status_discrepancy",
        primary="bank_reconciliation",
    )
    assert orchestrator.execution.bank.count() == 0
    assert case.approval is None


# ---------------------------------------------------------------------------
# The developer-only import gate
# ---------------------------------------------------------------------------
def test_developer_import_requires_a_key(workspace_tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from main import create_app

    orchestrator, database = build(workspace_tmp_path, "gate")
    app = create_app()
    app.state.workflow_repository = WorkflowRepository(database)
    app.state.orchestrator = orchestrator
    client = TestClient(app)

    csv_bytes = b"Transaction ID,Payment Reference,Amount,Currency\nB1,SP-X,10.00,USD\n"

    # No test-mode flag: refused.
    assert client.post("/bank/import", files={"file": ("s.csv", csv_bytes, "text/csv")}).status_code == 403

    # Test mode but no key configured: refused.
    monkeypatch.delenv("GHOSTLEDGER_DEVELOPER_KEY", raising=False)
    response = client.post(
        "/bank/import?authorized_test_mode=true",
        files={"file": ("s.csv", csv_bytes, "text/csv")},
    )
    assert response.status_code == 403

    # Wrong key: refused.
    monkeypatch.setenv("GHOSTLEDGER_DEVELOPER_KEY", "secret-dev-key")
    response = client.post(
        "/bank/import?authorized_test_mode=true&developer_key=wrong",
        files={"file": ("s.csv", csv_bytes, "text/csv")},
    )
    assert response.status_code == 403

    # Correct key: allowed, for developers only.
    response = client.post(
        "/bank/import?authorized_test_mode=true&developer_key=secret-dev-key",
        files={"file": ("s.csv", csv_bytes, "text/csv")},
    )
    assert response.status_code == 200


def test_ledger_endpoint_is_read_only(workspace_tmp_path):
    from fastapi.testclient import TestClient
    from main import create_app

    orchestrator, database = build(workspace_tmp_path, "roapi")
    case = open_case(orchestrator)
    orchestrator.approve(case.case_id, approved_by="approver@example.test")

    app = create_app()
    app.state.workflow_repository = WorkflowRepository(database)
    app.state.orchestrator = orchestrator
    client = TestClient(app)

    payload = client.get("/bank/ledger", headers=admin_headers(app)).json()
    assert payload["simulated"] is True
    assert payload["count"] == 1

    # There is no write endpoint on the ledger paths.
    for method in (client.post, client.put, client.delete):
        response = method("/bank/ledger")
        assert response.status_code in {404, 405}
