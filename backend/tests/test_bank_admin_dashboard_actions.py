"""Acceptance coverage for the Bank Admin page's approve/reject actions.

The existing mock-bank suite exercises the lower-level two-step API.  These
tests pin the dashboard-specific endpoints: one recorded human decision starts
the full, persisted settlement path and a rejection never creates a transfer.
"""

from __future__ import annotations

from workflow.mock_bank_accounts import (
    REQUEST_REJECTED,
    REQUEST_SETTLED,
    MockBankStore,
    to_minor,
)
from workflow.bank_admin import repair_cross_customer_payment_links
from workflow.accounts import ROLE_DEMO_ADMIN
from workflow.repository import WorkflowRepository
from workflow.schemas import CaseStatus, Intent, WorkflowCase

from http_helpers import auth, invoice_text, make_admin, reply, signup, upload
from workflow.demo_bank_seed import onboard_customer


def _open_payable(app, client, token: str, invoice: str, amount: str) -> dict:
    response = upload(app, client, token, invoice_text(invoice, amount), reply(invoice, amount))
    assert response.status_code == 201, response.text
    case = response.json()
    assert case["status"] == "AWAITING_APPROVAL"
    return case


def test_bank_admin_approval_settles_once_and_persists(workspace_tmp_path):
    from http_helpers import make_app

    app, client = make_app(workspace_tmp_path, "dashboard-approve")
    customer_token, customer_id = signup(client, "dashboard.customer@example.test")
    source = onboard_customer(
        app,
        account_id=customer_id,
        display_name="Dashboard Customer",
        opening_balance="10000.00",
    )
    admin_token, _ = make_admin(app, client, "dashboard.bank@example.test")
    case = _open_payable(app, client, customer_token, "INV-DASH-1620", "1620.00")

    request = app.state.bank_store.payment_requests(case_id=case["case_id"])[0]
    destination = app.state.bank_store.account(request["destination_account_id"])
    assert destination is not None
    source_before = source["balance_minor"]
    destination_before = destination["balance_minor"]

    # The work is visible to a bank admin immediately, never to the customer.
    queue = client.get("/bank-admin/requests", headers=auth(admin_token))
    assert queue.status_code == 200
    queued = next(row for row in queue.json()["requests"] if row["payment_id"] == request["payment_id"])
    assert queued["agent_checks"]["agent_3"]["passed"] is True
    assert queued["agent_checks"]["agent_1"]["passed"] is True
    assert client.get("/bank-admin/requests", headers=auth(customer_token)).status_code == 403

    settled = client.post(
        f"/bank-admin/requests/{request['payment_id']}/approve",
        json={"note": "Invoice and payee verified."},
        headers=auth(admin_token),
    )
    assert settled.status_code == 200, settled.text
    body = settled.json()
    assert body["approval_recorded"] is True
    assert body["status"] == REQUEST_SETTLED
    assert body["settlement_reference"]

    # One atomic internal transfer: debit, credit, permanent double-entry rows.
    source_after = app.state.bank_store.account(source["account_id"])
    destination_after = app.state.bank_store.account(destination["account_id"])
    assert source_after["balance_minor"] == source_before - to_minor("1620.00")
    assert destination_after["balance_minor"] == destination_before + to_minor("1620.00")
    entries = app.state.bank_store.ledger_rows_for_payment(request["payment_id"])
    assert {entry["direction"] for entry in entries} == {"debit", "credit"}
    assert all(entry["status"] == "SETTLED" for entry in entries)

    # Agent 2 verifies the committed settlement and Agent 5 gives the outcome.
    final = client.get(f"/workflow/cases/{case['case_id']}", headers=auth(customer_token)).json()
    assert final["status"] == "COMPLETED"
    assert final["reconciliation"]["verdict"] == "MATCHED"
    assert {report["agent"] for report in final["reports"]} >= {"agent_2", "agent_5"}
    assert final["customer_response"]["payment_settled"] is True

    history = client.get("/bank-admin/transactions", headers=auth(admin_token)).json()["transactions"]
    row = next(item for item in history if item["payment_id"] == request["payment_id"])
    assert row["transaction_id"]
    assert row["customer_name"] == "Dashboard Customer"
    assert row["vendor_name"]

    # A browser retry reports the old settlement and cannot charge again.
    repeated = client.post(
        f"/bank-admin/requests/{request['payment_id']}/approve", headers=auth(admin_token)
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["already_settled"] is True
    assert app.state.bank_store.account(source["account_id"])["balance_minor"] == source_after["balance_minor"]
    assert len(app.state.bank_store.ledger_rows_for_payment(request["payment_id"])) == 2

    # Reopening the persistent mock-bank database retains the completed transfer.
    reloaded = MockBankStore(app.state.bank_store.path)
    assert reloaded.payment_request(request["payment_id"])["status"] == REQUEST_SETTLED
    assert len(reloaded.ledger_rows_for_payment(request["payment_id"])) == 2


def test_bank_admin_rejection_requires_reason_and_moves_no_money(workspace_tmp_path):
    from http_helpers import make_app

    app, client = make_app(workspace_tmp_path, "dashboard-reject")
    customer_token, customer_id = signup(client, "reject.customer@example.test")
    source = onboard_customer(
        app,
        account_id=customer_id,
        display_name="Reject Customer",
        opening_balance="2000.00",
    )
    admin_token, _ = make_admin(app, client, "reject.bank@example.test")
    case = _open_payable(app, client, customer_token, "INV-DASH-REJECT", "150.00")
    request = app.state.bank_store.payment_requests(case_id=case["case_id"])[0]

    missing_reason = client.post(
        f"/bank-admin/requests/{request['payment_id']}/reject", headers=auth(admin_token)
    )
    assert missing_reason.status_code == 422

    rejected = client.post(
        f"/bank-admin/requests/{request['payment_id']}/reject",
        json={"reason": "Vendor banking details need correction."},
        headers=auth(admin_token),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == REQUEST_REJECTED
    stored = app.state.bank_store.payment_request(request["payment_id"])
    assert stored["failure_code"] == "REJECTED_BY_BANK_ADMIN"
    assert stored["failure_reason"] == "Vendor banking details need correction."
    assert app.state.bank_store.account(source["account_id"])["balance_minor"] == to_minor("2000.00")
    assert app.state.bank_store.ledger_rows_for_payment(request["payment_id"]) == []


def test_bank_admin_portal_is_restricted_to_bank_admin_role(workspace_tmp_path):
    """Neither a customer nor the isolated demo role can read the bank queue."""

    from http_helpers import make_app

    app, client = make_app(workspace_tmp_path, "bank-admin-role")
    customer_token, _ = signup(client, "queue.customer@example.test")
    demo_token, _ = make_admin(
        app, client, "queue.demo@example.test", role=ROLE_DEMO_ADMIN, name="Demo Admin"
    )

    assert client.get("/bank-admin/requests", headers=auth(customer_token)).status_code == 403
    assert client.get("/bank-admin/requests", headers=auth(demo_token)).status_code == 403


def test_my_invoices_is_canonical_and_preserves_upload_history(workspace_tmp_path):
    """A repeat upload has history but never becomes a second invoice row."""

    from http_helpers import make_app

    app, client = make_app(workspace_tmp_path, "canonical-invoices")
    customer_token, customer_id = signup(client, "invoices.owner@example.test")
    other_token, _ = signup(client, "invoices.other@example.test")
    onboard_customer(
        app,
        account_id=customer_id,
        display_name="Invoice Owner",
        opening_balance="10000.00",
    )

    first = _open_payable(app, client, customer_token, "INV-CANONICAL-1", "1620.00")
    second = upload(
        app, client, customer_token,
        invoice_text("INV-CANONICAL-1", "1620.00"), reply("INV-CANONICAL-1", "1620.00"),
    )
    assert second.status_code == 201, second.text

    invoices = client.get("/workflow/invoices", headers=auth(customer_token))
    assert invoices.status_code == 200
    body = invoices.json()
    assert len(body) == 1
    assert body[0]["invoice_number"] == "INV-CANONICAL-1"
    assert body[0]["upload_count"] == 2
    assert {item["case_id"] for item in body[0]["uploads"]} == {
        first["case_id"], second.json()["case_id"],
    }
    assert len(app.state.bank_store.payment_requests(customer_id=customer_id)) == 1

    # The canonical read model is ownership-scoped; another customer does not
    # see either the invoice or its case/upload history.
    assert client.get("/workflow/invoices", headers=auth(other_token)).json() == []


def test_payment_identity_is_customer_scoped_and_blocks_repeat_requests(workspace_tmp_path):
    """Same invoice number is not shared across customers, but re-uploads are."""

    store = MockBankStore(workspace_tmp_path / "customer-scoped-bank.db")
    source_a, _ = store.ensure_account(owner_id="customer-a", owner_type="CUSTOMER", currency="USD")
    source_b, _ = store.ensure_account(owner_id="customer-b", owner_type="CUSTOMER", currency="USD")
    destination, _ = store.ensure_account(owner_id="vendor", owner_type="VENDOR", currency="USD")

    first, created_first = store.create_payment_request(
        customer_id="customer-a", invoice_id="INV-SHARED-1", source_account_id=source_a["account_id"],
        destination_account_id=destination["account_id"], amount="10.00", currency="USD",
        payee="Shared Vendor", case_id="case-a", idempotency_key="case:case-a",
    )
    other_customer, created_other = store.create_payment_request(
        customer_id="customer-b", invoice_id="INV-SHARED-1", source_account_id=source_b["account_id"],
        destination_account_id=destination["account_id"], amount="10.00", currency="USD",
        payee="Shared Vendor", case_id="case-b", idempotency_key="case:case-b",
    )
    repeat, created_repeat = store.create_payment_request(
        customer_id="customer-a", invoice_id="INV-SHARED-1", source_account_id=source_a["account_id"],
        destination_account_id=destination["account_id"], amount="10.00", currency="USD",
        payee="Shared Vendor", case_id="case-a-repeat", idempotency_key="case:case-a-repeat",
    )

    assert created_first is True
    assert created_other is True
    assert first["payment_id"] != other_customer["payment_id"]
    assert created_repeat is False
    assert repeat["payment_id"] == first["payment_id"]


def test_standalone_mock_bank_1620_payment_is_atomic_and_idempotent(workspace_tmp_path):
    """The persistent bank store can settle the requested demo transfer by itself."""

    store = MockBankStore(workspace_tmp_path / "bank-1620.db")
    customer, _ = store.ensure_account(
        owner_id="orion", owner_type="CUSTOMER", display_name="Orion Creative Labs LLC",
        currency="USD", opening_balance="10000.00",
    )
    vendor, _ = store.ensure_account(
        owner_id="blue-harbor", owner_type="VENDOR", display_name="Blue Harbor Cloud Services LLC",
        currency="USD", opening_balance="2000.00",
    )
    request, _ = store.create_payment_request(
        customer_id="orion", invoice_id="INV-GL-DEMO-26024-701",
        source_account_id=customer["account_id"], destination_account_id=vendor["account_id"],
        amount="1620.00", currency="USD", payee="Blue Harbor Cloud Services LLC",
        case_id="demo-1620", idempotency_key="case:demo-1620",
    )
    store.record_approval(
        payment_id=request["payment_id"], approver_id="bank-admin", approver_role="BANK_ADMIN",
        decision="APPROVED", reason="Agent 3 and Agent 1 passed.",
    )

    settled = store.process_payment(request["payment_id"], actor_id="bank-admin", actor_role="BANK_ADMIN")
    retried = store.process_payment(request["payment_id"], actor_id="bank-admin", actor_role="BANK_ADMIN")

    assert settled["status"] == REQUEST_SETTLED
    assert retried["already_settled"] is True
    assert store.account(customer["account_id"])["balance_minor"] == to_minor("8380.00")
    assert store.account(vendor["account_id"])["balance_minor"] == to_minor("3620.00")
    assert {entry["direction"] for entry in store.ledger_rows_for_payment(request["payment_id"])} == {"debit", "credit"}


def test_repair_preserves_history_but_detaches_foreign_payment_link(workspace_tmp_path):
    """Old invoice-only lookups cannot expose another customer's request."""

    from types import SimpleNamespace

    database = workspace_tmp_path / "cross-customer-link.db"
    repository = WorkflowRepository(database)
    store = MockBankStore(database)
    source, _ = store.ensure_account(owner_id="orion-owner", owner_type="CUSTOMER", opening_balance="10000.00")
    destination, _ = store.ensure_account(owner_id="vendor-owner", owner_type="VENDOR")
    request, _ = store.create_payment_request(
        customer_id="orion-owner", invoice_id="INV-SHARED-HISTORY", source_account_id=source["account_id"],
        destination_account_id=destination["account_id"], amount="25.00", currency="USD",
        payee="Blue Harbor Cloud Services LLC", case_id="orion-case", idempotency_key="case:orion-case",
    )
    historical = WorkflowCase(
        case_id="customer-a-upload", owner_account_id="customer-a", status=CaseStatus.awaiting_approval,
        intent=Intent.new_payment_request, customer_name="Orion Creative Labs LLC",
        vendor_name="Blue Harbor Cloud Services LLC", invoice_number="INV-SHARED-HISTORY",
        amount="25.00", currency="USD", payment_request_id=request["payment_id"],
    )
    repository.save_case(historical)
    app = SimpleNamespace(state=SimpleNamespace(workflow_repository=repository, bank_store=store))

    assert repair_cross_customer_payment_links(app) == {"repaired": 1}
    repaired = repository.get_case(historical.case_id)
    assert repaired is not None
    assert repaired.status == CaseStatus.requires_review
    assert repaired.payment_request_id is None
    assert repaired.pending_reason and "another customer" in repaired.pending_reason
    assert any(entry.action == "cross_customer_payment_link_detached" for entry in repaired.audit)
    # The original bank request/evidence is untouched.
    assert store.payment_request(request["payment_id"])["customer_id"] == "orion-owner"
