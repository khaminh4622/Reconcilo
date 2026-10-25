"""Accounts, roles and per-account payment history.

Covers the ten required scenarios. The organising rule: a customer sees only
their own records, an admin sees the Mock Bank, and nothing survives a restart
by living in browser state.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent3.database import DocumentRepository
from agent3.service import Agent3Service
from main import create_app
from workflow.accounts import ROLE_BANK_ADMIN, ROLE_CUSTOMER, AccountStore, AuthError
from workflow.mock_bank import MockBank
from workflow.orchestrator import WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.tenancy import LEGACY_STATE, migrate_tenancy

from test_workflow import build_analysis


VENDOR = "Northstar Office Supplies LLC"


def make_app(workspace_tmp_path, name: str):
    """A full app over an isolated database."""

    database = workspace_tmp_path / f"{name}.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()

    WorkflowRepository(database)
    # Create every table first, then add ownership to all of them.
    MockBank(database)
    migrate_tenancy(database, take_backup=False)

    app = create_app()
    app.state.service = Agent3Service(DocumentRepository(database), app.state.service.extractor)
    repository = WorkflowRepository(database)
    app.state.workflow_repository = repository
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.simulator.bank = MockBank(database)
    orchestrator.execution.bank = orchestrator.simulator.bank
    app.state.orchestrator = orchestrator
    return app, database, orchestrator


def client_for(app) -> TestClient:
    return TestClient(app)


def signup(client, email, name=None, password="password123"):
    response = client.post(
        "/auth/signup",
        json={"email": email, "password": password, "display_name": name or email},
    )
    assert response.status_code == 200, response.text
    return response.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def pay(client, orchestrator, token, *, invoice, amount, customer, simulate=None):
    """Open a case, stamp ownership and approve it through the real path."""

    account_id = client.get("/auth/me", headers=auth(token)).json()["account"]["account_id"]
    analysis = build_analysis(
        invoice_number=invoice, vendor=VENDOR, customer=customer,
        total=amount, subtotal=amount, tax="0.00",
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "invoice.pdf"))
    case.owner_account_id = account_id
    orchestrator.repository.save_case(case)
    return orchestrator.approve(
        case.case_id, approved_by="approver@example.test", simulate=simulate
    )


# ---------------------------------------------------------------------------
# 1. New signup shows empty history
# ---------------------------------------------------------------------------
def test_new_signup_has_empty_history(workspace_tmp_path):
    app, database, _ = make_app(workspace_tmp_path, "signup")
    response = signup(client_for(app), "new@example.com")

    assert response["account"]["role"] == ROLE_CUSTOMER
    assert response["payment_history"] == []
    assert response["cases"] == []
    assert "No payment history yet" in response["message"]
    assert app.state.accounts.get(response["account"]["account_id"]).account_id


def test_signup_does_not_inherit_other_accounts_data(workspace_tmp_path):
    app, database, orchestrator = make_app(workspace_tmp_path, "inherit")
    client = client_for(app)
    first = signup(client, "first@example.com", "Customer A")
    pay(client, orchestrator, first["token"], invoice="INV-A-1", amount="1620.00", customer="Customer A")

    second = signup(client, "second@example.com", "Customer B")

    assert second["payment_history"] == []
    assert second["cases"] == []
    # No Mock Bank row belongs to the new account.
    conn = sqlite3.connect(database)
    owned = conn.execute(
        "SELECT COUNT(*) FROM mock_bank_ledger WHERE owner_account_id = ?",
        (second["account"]["account_id"],),
    ).fetchone()[0]
    conn.close()
    assert owned == 0


# ---------------------------------------------------------------------------
# 2. First successful payment appears in that account's history
# ---------------------------------------------------------------------------
def test_first_payment_appears_in_history(workspace_tmp_path):
    app, database, orchestrator = make_app(workspace_tmp_path, "firstpay")
    client = client_for(app)
    account = signup(client, "b@example.com", "Customer B")
    assert account["payment_history"] == []

    paid = pay(client, orchestrator, account["token"], invoice="INV-B-1",
               amount="2500.00", customer="Customer B")
    assert paid.payment.status == "SUCCESS"

    history = client.get("/auth/me", headers=auth(account["token"])).json()
    assert len(history["payment_history"]) == 1
    entry = history["payment_history"][0]
    assert entry["invoice_number"] == "INV-B-1"
    assert entry["amount"] == "2500.00"
    assert entry["status"] == "SUCCESS"
    assert entry["verified"] is True


# ---------------------------------------------------------------------------
# 3. Signing out and back in preserves the payment
# ---------------------------------------------------------------------------
def test_signout_and_signin_preserves_history(workspace_tmp_path):
    app, database, orchestrator = make_app(workspace_tmp_path, "reauth")
    client = client_for(app)
    account = signup(client, "keep@example.com", "Customer A")
    pay(client, orchestrator, account["token"], invoice="INV-K-1",
        amount="1620.00", customer="Customer A")

    client.post("/auth/signout", headers=auth(account["token"]))
    # The old token no longer works.
    assert client.get("/auth/me", headers=auth(account["token"])).status_code == 401

    again = client.post(
        "/auth/signin", json={"email": "keep@example.com", "password": "password123"}
    )
    assert again.status_code == 200
    body = again.json()
    assert len(body["payment_history"]) == 1
    assert body["payment_history"][0]["amount"] == "1620.00"


# ---------------------------------------------------------------------------
# 4. Refreshing the page preserves the history
# ---------------------------------------------------------------------------
def test_refresh_preserves_history(workspace_tmp_path):
    """A new request with the same session token sees the same history."""

    app, database, orchestrator = make_app(workspace_tmp_path, "refresh")
    client = client_for(app)
    account = signup(client, "refresh@example.com")
    pay(client, orchestrator, account["token"], invoice="INV-R-1",
        amount="1620.00", customer="Customer A")

    first = client.get("/auth/me", headers=auth(account["token"])).json()
    second = client.get("/auth/me", headers=auth(account["token"])).json()

    assert len(first["payment_history"]) == 1
    assert first["payment_history"] == second["payment_history"]


# ---------------------------------------------------------------------------
# 5. Restarting the backend preserves the history
# ---------------------------------------------------------------------------
def test_restart_preserves_history(workspace_tmp_path):
    app, database, orchestrator = make_app(workspace_tmp_path, "restart")
    client = client_for(app)
    account = signup(client, "restart@example.com")
    pay(client, orchestrator, account["token"], invoice="INV-RS-1",
        amount="1620.00", customer="Customer A")

    # A brand-new app over the same file, as after a restart.
    restarted = create_app()
    restarted.state.service = Agent3Service(
        DocumentRepository(database), restarted.state.service.extractor
    )
    repository = WorkflowRepository(database)
    restarted.state.workflow_repository = repository
    fresh_orchestrator = WorkflowOrchestrator(repository)
    fresh_orchestrator.simulator.bank = MockBank(database)
    restarted.state.orchestrator = fresh_orchestrator

    body = client_for(restarted).post(
        "/auth/signin", json={"email": "restart@example.com", "password": "password123"}
    ).json()

    assert len(body["payment_history"]) == 1
    assert body["payment_history"][0]["invoice_number"] == "INV-RS-1"


# ---------------------------------------------------------------------------
# 6. Customer A cannot see Customer B's payment history
# ---------------------------------------------------------------------------
def test_customers_cannot_see_each_others_history(workspace_tmp_path):
    app, database, orchestrator = make_app(workspace_tmp_path, "isolation")
    client = client_for(app)
    a = signup(client, "iso-a@example.com", "Customer A")
    b = signup(client, "iso-b@example.com", "Customer B")

    pay(client, orchestrator, a["token"], invoice="INV-A-9", amount="1620.00", customer="Customer A")
    pay(client, orchestrator, b["token"], invoice="INV-B-9", amount="2500.00", customer="Customer B")

    history_a = client.get("/auth/me", headers=auth(a["token"])).json()
    history_b = client.get("/auth/me", headers=auth(b["token"])).json()

    assert [p["invoice_number"] for p in history_a["payment_history"]] == ["INV-A-9"]
    assert [p["invoice_number"] for p in history_b["payment_history"]] == ["INV-B-9"]

    # Listing cases is likewise scoped.
    cases_a = client.get("/workflow/cases", headers=auth(a["token"])).json()
    cases_b = client.get("/workflow/cases", headers=auth(b["token"])).json()
    assert [c["invoice_number"] for c in cases_a] == ["INV-A-9"]
    assert [c["invoice_number"] for c in cases_b] == ["INV-B-9"]


def test_anonymous_caller_sees_no_cases(workspace_tmp_path):
    app, database, orchestrator = make_app(workspace_tmp_path, "anon")
    client = client_for(app)
    a = signup(client, "a@example.com")
    pay(client, orchestrator, a["token"], invoice="INV-AN-1", amount="100.00", customer="A")

    assert client.get("/workflow/cases").json() == []
    assert client.get("/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# 7. Customer B cannot reach Customer A's records by guessing IDs
# ---------------------------------------------------------------------------
def test_customer_cannot_reach_another_account_by_id(workspace_tmp_path):
    app, database, orchestrator = make_app(workspace_tmp_path, "guess")
    client = client_for(app)
    a = signup(client, "a@example.com", "Customer A")
    b = signup(client, "b@example.com", "Customer B")

    target = pay(client, orchestrator, a["token"], invoice="INV-GUESS-1",
                 amount="1620.00", customer="Customer A")
    case_id = target.case_id

    # Knowing the exact case id is not enough.
    assert client.get(f"/workflow/cases/{case_id}", headers=auth(b["token"])).status_code == 404
    assert client.get(f"/workflow/cases/{case_id}/receipt", headers=auth(b["token"])).status_code == 404
    assert client.get(
        f"/workflow/cases/{case_id}/investigation", headers=auth(b["token"])
    ).status_code == 404
    assert client.post(
        f"/workflow/cases/{case_id}/approve",
        json={"approved_by": "attacker@example.test"},
        headers=auth(b["token"]),
    ).status_code == 404

    # The owner still can.
    assert client.get(f"/workflow/cases/{case_id}", headers=auth(a["token"])).status_code == 200


def test_anonymous_cannot_approve_a_case(workspace_tmp_path):
    app, database, orchestrator = make_app(workspace_tmp_path, "anonapprove")
    client = client_for(app)
    a = signup(client, "a@example.com")
    case = pay(client, orchestrator, a["token"], invoice="INV-AN-2", amount="10.00", customer="A")

    response = client.post(
        f"/workflow/cases/{case.case_id}/approve", json={"approved_by": "x@example.test"}
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 8. Admin can see authorized internal Mock Bank transactions
# ---------------------------------------------------------------------------
def test_admin_sees_mock_bank_transactions(workspace_tmp_path):
    app, database, orchestrator = make_app(workspace_tmp_path, "admin")
    client = client_for(app)
    customer = signup(client, "c@example.com", "Customer A")
    pay(client, orchestrator, customer["token"], invoice="INV-AD-1",
        amount="1620.00", customer="Customer A")

    admin = app.state.accounts.sign_up(
        email="admin@bank.example", password="adminpass123", role=ROLE_BANK_ADMIN
    )
    _acct, admin_token = app.state.accounts.sign_in(
        email="admin@bank.example", password="adminpass123"
    )

    ledger = client.get("/bank/ledger", headers=auth(admin_token))
    assert ledger.status_code == 200
    body = ledger.json()
    assert body["count"] >= 1
    assert body["simulated"] is True
    assert any(t["invoice_number"] == "INV-AD-1" for t in body["transactions"])

    # Detail view works for an admin too.
    entry_id = body["transactions"][0]["entry_id"]
    assert client.get(f"/bank/ledger/{entry_id}", headers=auth(admin_token)).status_code == 200


# ---------------------------------------------------------------------------
# 9. Customer cannot access the raw Mock Bank API
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/bank/ledger"),
        ("get", "/bank/transactions"),
        ("get", "/workflow/bank-transactions"),
        ("post", "/bank/reconcile"),
    ],
)
def test_customer_cannot_access_raw_bank_api(workspace_tmp_path, method, path):
    app, database, orchestrator = make_app(workspace_tmp_path, f"raw-{method}-{path.strip('/')}")
    client = client_for(app)
    customer = signup(client, "c@example.com")

    response = getattr(client, method)(path, headers=auth(customer["token"]))
    assert response.status_code == 403

    # And without signing in at all.
    assert getattr(client, method)(path).status_code == 401


def test_bank_ledger_detail_is_admin_only(workspace_tmp_path):
    app, database, orchestrator = make_app(workspace_tmp_path, "rawdetail")
    client = client_for(app)
    customer = signup(client, "c@example.com")
    assert client.get("/bank/ledger/anything", headers=auth(customer["token"])).status_code == 403


# ---------------------------------------------------------------------------
# 10. Existing historical transactions remain intact
# ---------------------------------------------------------------------------
def test_legacy_rows_are_preserved_and_restricted(workspace_tmp_path):
    """Rows that predate accounts are kept, never auto-assigned."""

    database = workspace_tmp_path / "legacy.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()

    repository = WorkflowRepository(database)
    MockBank(database)
    migrate_tenancy(database, take_backup=False)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.simulator.bank = MockBank(database)

    # A payment made before accounts existed.
    analysis = build_analysis(
        invoice_number="INV-OLD-1", vendor=VENDOR, customer="Old Customer",
        total="918.00", subtotal="918.00", tax="0.00",
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "old.pdf"))
    orchestrator.approve(case.case_id, approved_by="legacy@example.test")

    conn = sqlite3.connect(database)
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("workflow_cases", "payment_attempts", "mock_bank_ledger", "payment_receipts")
    }
    conn.close()

    report = migrate_tenancy(database, take_backup=False)

    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    legacy = conn.execute(
        "SELECT COUNT(*) FROM workflow_cases WHERE ownership_state = ?", (LEGACY_STATE,)
    ).fetchone()[0]
    unowned = conn.execute(
        "SELECT COUNT(*) FROM workflow_cases WHERE owner_account_id IS NOT NULL"
    ).fetchone()[0]
    conn.close()

    # Nothing lost, nothing claimed.
    assert after == before
    assert legacy == before["workflow_cases"]
    assert unowned == 0
    assert report["legacy_marked"]["workflow_cases"] == before["workflow_cases"]


def test_new_customer_does_not_receive_legacy_payments(workspace_tmp_path):
    """The critical guarantee: history is never handed to a new signup."""

    database = workspace_tmp_path / "legacy2.db"
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if path.exists():
            path.unlink()

    repository = WorkflowRepository(database)
    MockBank(database)
    migrate_tenancy(database, take_backup=False)
    orchestrator = WorkflowOrchestrator(repository)
    orchestrator.simulator.bank = MockBank(database)
    analysis = build_analysis(
        invoice_number="INV-LEGACY-9", vendor=VENDOR, customer="Historical",
        total="1620.00", subtotal="1620.00", tax="0.00",
    )
    case = orchestrator.route(orchestrator.open_case(analysis, "old.pdf"))
    orchestrator.approve(case.case_id, approved_by="legacy@example.test")

    app = create_app()
    app.state.service = Agent3Service(
        DocumentRepository(database), app.state.service.extractor
    )
    app.state.workflow_repository = repository
    app.state.orchestrator = orchestrator

    client = client_for(app)
    fresh = signup(client, "brand-new@example.com")

    assert fresh["payment_history"] == []
    assert fresh["cases"] == []


# ---------------------------------------------------------------------------
# Account safety
# ---------------------------------------------------------------------------
def test_password_is_never_returned_or_stored_in_plain_text(workspace_tmp_path):
    app, database, _ = make_app(workspace_tmp_path, "pw")
    client = client_for(app)
    body = signup(client, "pw@example.com")

    assert "password" not in {k.lower() for k in body}
    assert "password" not in {k.lower() for k in body["account"]}

    # Query the same database the app actually wrote to.
    conn = sqlite3.connect(app.state.accounts.path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT password_hash, password_salt FROM accounts WHERE email = 'pw@example.com'"
    ).fetchone()
    conn.close()
    assert row is not None, "the account must be persisted"
    assert "password123" not in row["password_hash"]
    assert row["password_salt"]


def test_duplicate_signup_is_rejected(workspace_tmp_path):
    app, database, _ = make_app(workspace_tmp_path, "dup")
    client = client_for(app)
    signup(client, "dup@example.com")

    response = client.post(
        "/auth/signup", json={"email": "dup@example.com", "password": "password123"}
    )
    assert response.status_code == 400


def test_short_password_is_rejected(workspace_tmp_path):
    app, database, _ = make_app(workspace_tmp_path, "short")
    client = client_for(app)
    response = client.post("/auth/signup", json={"email": "s@example.com", "password": "short"})
    assert response.status_code == 400
