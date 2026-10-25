"""Integration tests for the simulated banking workflow.

Each test drives the real API: accounts, the orchestrator, every agent, the
Mock Bank's accounts ledger and the restricted bank tools. Only the remote LLM
is replaced (the suite's usual boundary).

Covered, as required:

    * successful $1,620 simulated payment (the INV-GL-DEMO-26024-701 case)
    * insufficient funds
    * duplicate invoice
    * duplicate payment submission
    * payment rejection
    * reconciliation mismatch
    * unauthorised payment execution
    * customer attempting to access the bank ledger
    * Customer A attempting to access Customer B's records
    * agent attempting to access an unrelated bank case
    * concurrent payment requests
    * retry after an uncertain transaction
    * total simulated funds consistent across internal transfers
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from workflow.accounts import ROLE_BANK_ADMIN, ROLE_DEMO_ADMIN
from workflow.bank_tools import AgentBankTools, BankToolError, CaseContext
from workflow.demo_bank_seed import (
    DEMO_AMOUNT,
    DEMO_CUSTOMER,
    DEMO_INVOICE,
    DEMO_VENDOR,
    onboard_customer,
    seed_demo_bank,
)
from workflow.mock_bank_accounts import (
    ACCOUNT_FROZEN,
    REQUEST_APPROVED,
    REQUEST_FAILED,
    REQUEST_PENDING,
    REQUEST_SETTLED,
    REQUEST_UNCERTAIN,
    MockBankStore,
    from_minor,
    to_minor,
)

from http_helpers import auth, invoice_text, make_admin, make_app, reply, signup, upload

DEMO_PASSWORD = "ghostledger-demo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def store_of(app) -> MockBankStore:
    return app.state.bank_store


def bank_accounts(app) -> dict[str, dict]:
    """Simulated accounts indexed by account_id."""

    return {a["account_id"]: a for a in store_of(app).accounts()}


def fund(app, account_id: str, amount: str) -> dict:
    return store_of(app).top_up(account_id, amount, actor_id="test", actor_role="SYSTEM")


def fund_customer(app, client, email: str, amount: str = "5000.00") -> tuple[str, str]:
    """Sign up a customer and give them a funded simulated account."""

    token, account_id = signup(client, email)
    account = onboard_customer(
        app, account_id=account_id, display_name=email, opening_balance=amount
    )
    return token, account["account_id"]


def open_invoice(app, client, token: str, number: str, amount: str) -> dict:
    response = upload(app, client, token, invoice_text(number, amount), reply(number, amount))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "AWAITING_APPROVAL", body
    return body


def approve(client, admin: str, case_id: str, **body):
    # Two-step flow: authorise only; the bank processes in a separate call.
    body.setdefault("process", False)
    return client.post(f"/workflow/cases/{case_id}/approve", json=body, headers=auth(admin))


def process(app, client, admin: str, payment_id: str):
    return client.post(f"/bank-admin/requests/{payment_id}/process", headers=auth(admin))


def case_of(client, token: str, case_id: str) -> dict:
    response = client.get(f"/workflow/cases/{case_id}", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def payment_of(app, case_id: str) -> dict:
    rows = store_of(app).payment_requests(case_id=case_id)
    assert rows, f"no payment request for case {case_id}"
    return rows[0]


def total_funds(app, currency: str = "USD") -> int:
    return store_of(app).total_funds_minor(currency)


# ---------------------------------------------------------------------------
# 1. The complete successful workflow
# ---------------------------------------------------------------------------
def test_demo_case_settles_end_to_end(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "demo-case")
    demo = seed_demo_bank(app)
    assert demo["demo_case"]["invoice_number"] == DEMO_INVOICE
    assert demo["demo_case"]["amount"] == "1620.00"

    # Sign in as the seeded Orion customer and find the case through the API.
    token = client.post(
        "/auth/signin", json={"email": "orion@ghostledger.test", "password": DEMO_PASSWORD}
    ).json()["token"]
    admin, admin_id = make_admin(app, client, "bank@example.test", name="Riley Bank")

    cases = client.get("/workflow/cases", headers=auth(token)).json()
    target = next(c for c in cases if c["invoice_number"] == DEMO_INVOICE)
    case_id = target["case_id"]
    assert target["amount"] == "1620.00"

    # Agent 3 read the invoice and Agent 1 cleared it: awaiting approval.
    before = case_of(client, token, case_id)
    assert before["status"] == "AWAITING_APPROVAL"
    assert before["customer_name"] == DEMO_CUSTOMER
    assert before["vendor_name"] == DEMO_VENDOR
    assert before["payment_request_id"]
    request = payment_of(app, case_id)
    assert request["status"] == REQUEST_PENDING
    assert request["amount"] == "1620.00"

    funds_before = total_funds(app)
    orion_before = bank_accounts(app)[
        store_of(app).payment_request(request["payment_id"])["source_account_id"]
    ]["balance_minor"]

    # A human authorises it.
    approved = approve(client, admin, case_id, note="Invoice verified")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert payment_of(app, case_id)["status"] == REQUEST_APPROVED

    # The bank processes the authorised request.
    result = process(app, client, admin, request["payment_id"])
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == REQUEST_SETTLED
    assert body["settlement_reference"]

    # Both sides committed and the money actually moved between the accounts.
    entries = store_of(app).ledger_rows_for_payment(request["payment_id"])
    assert {e["direction"] for e in entries} == {"debit", "credit"}
    assert all(e["status"] == "SETTLED" for e in entries)
    assert {e["amount"] for e in entries} == {"1620.00"}
    assert total_funds(app) == funds_before  # an internal transfer conserves funds
    source_id = payment_of(app, case_id)["source_account_id"]
    assert bank_accounts(app)[source_id]["balance_minor"] == orion_before - 162_000

    # Agent 2 reconciled, Agent 5 confirmed, the case completed.
    final = case_of(client, token, case_id)
    assert final["status"] == "COMPLETED"
    assert final["reconciliation"]["verdict"] == "MATCHED"
    assert final["customer_response"]["payment_settled"] is True
    assert final["payment"]["status"] == "SUCCESS"
    agents = {r["agent"] for r in final["reports"]}
    assert {"agent_2", "agent_5"} <= agents

    # A receipt exists and the timeline is entirely backend-derived.
    receipt = client.get(f"/workflow/cases/{case_id}/receipt", headers=auth(token))
    assert receipt.status_code == 200
    timeline = client.get(f"/cases/{case_id}/timeline", headers=auth(token)).json()
    states = {s["key"]: s["state"] for s in timeline["steps"]}
    for key in (
        "document_uploaded",
        "agent3_verified",
        "agent1_duplicate_check",
        "approved",
        "bank_processing",
        "bank_settled",
        "agent2_reconciled",
        "agent5_confirmed",
    ):
        assert states[key] == "done", (key, states)
    assert states["bank_settled"] == "done"

    # The customer's own view shows the settled payment, without the full bank
    # ledger or any other customer's rows.
    me = client.get("/auth/me", headers=auth(token)).json()
    mine = next(p for p in me["bank_payments"] if p["invoice_number"] == DEMO_INVOICE)
    assert mine["status"] == REQUEST_SETTLED
    # Only their own bank account (masked number) is exposed, never the ledger
    # and never another account.
    assert "bank_account" in me and me["bank_account"] is not None
    assert all(p["invoice_number"] == DEMO_INVOICE for p in me["bank_payments"])
    assert "transactions" not in me and "accounts" not in me


# ---------------------------------------------------------------------------
# 2. Insufficient funds
# ---------------------------------------------------------------------------
def test_insufficient_funds_fails_without_moving_money(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "nsf")
    token, account_id = fund_customer(app, client, "poor@example.test", "100.00")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = open_invoice(app, client, token, "INV-NSF-1", "250.00")
    request = payment_of(app, case["case_id"])
    approve(client, admin, case["case_id"])

    funds_before = total_funds(app)
    balance_before = bank_accounts(app)[account_id]["balance_minor"]

    result = process(app, client, admin, request["payment_id"]).json()

    assert result["status"] == REQUEST_FAILED
    assert result["failure_code"] == "INSUFFICIENT_FUNDS"
    # No debit, no credit, no funds moved — only a FAILED record is posted.
    assert bank_accounts(app)[account_id]["balance_minor"] == balance_before
    assert total_funds(app) == funds_before
    rows = store_of(app).ledger_rows_for_payment(request["payment_id"])
    assert all(r["status"] == "FAILED" for r in rows)

    # Agent 4 investigated and Agent 5 explained it to the customer.
    final = case_of(client, token, case["case_id"])
    assert final["status"] == "REQUIRES_REVIEW"
    assert any(r["agent"] == "agent_4" for r in final["reports"])
    assert final["customer_response"]["payment_settled"] is False
    timeline = client.get(f"/cases/{case['case_id']}/timeline", headers=auth(token)).json()
    assert any(s["key"] == "agent4_exception" for s in timeline["steps"])


# ---------------------------------------------------------------------------
# 3. Duplicate invoice
# ---------------------------------------------------------------------------
def test_duplicate_invoice_never_reaches_a_second_payment(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "dupe-invoice")
    token, account_id = fund_customer(app, client, "dupe@example.test", "10000.00")
    admin, _ = make_admin(app, client, "bank@example.test")

    first = open_invoice(app, client, token, "INV-DUP-1", "400.00")
    request = payment_of(app, first["case_id"])
    approve(client, admin, first["case_id"])
    assert process(app, client, admin, request["payment_id"]).json()["status"] == REQUEST_SETTLED

    second = upload(
        app, client, token, invoice_text("INV-DUP-1", "400.00"), reply("INV-DUP-1", "400.00")
    )
    assert second.status_code == 201, second.text
    repeat = second.json()

    # The repeated invoice is recognised, not payable again.
    assert repeat["status"] != "AWAITING_APPROVAL"
    assert not store_of(app).live_request_for_invoice("INV-DUP-1")
    settled = store_of(app).settled_request_for_invoice("INV-DUP-1")
    assert settled["payment_id"] == request["payment_id"]

    response = approve(client, admin, repeat["case_id"])
    assert response.status_code == 409
    # Exactly one settled payment exists for the invoice.
    assert len(store_of(app).payment_requests(invoice_id="INV-DUP-1", statuses={REQUEST_SETTLED})) == 1
    assert bank_accounts(app)[account_id]["balance_minor"] == to_minor("10000.00") - to_minor("400.00")


# ---------------------------------------------------------------------------
# 4. Duplicate payment submission (the same request processed twice)
# ---------------------------------------------------------------------------
def test_duplicate_payment_submission_settles_once(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "dupe-payment")
    token, account_id = fund_customer(app, client, "twice@example.test", "8000.00")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = open_invoice(app, client, token, "INV-TWICE-1", "300.00")
    request = payment_of(app, case["case_id"])
    approve(client, admin, case["case_id"])

    first = process(app, client, admin, request["payment_id"]).json()
    second = process(app, client, admin, request["payment_id"]).json()

    assert first["status"] == REQUEST_SETTLED and second["status"] == REQUEST_SETTLED
    assert second.get("already_settled") is True
    # Exactly one debit and one credit, and the balance moved once.
    entries = store_of(app).ledger_rows_for_payment(request["payment_id"])
    assert len([e for e in entries if e["direction"] == "debit"]) == 1
    assert len([e for e in entries if e["direction"] == "credit"]) == 1
    assert bank_accounts(app)[account_id]["balance_minor"] == to_minor("8000.00") - to_minor("300.00")


# ---------------------------------------------------------------------------
# 5. Payment rejection by the human approver
# ---------------------------------------------------------------------------
def test_rejected_payment_never_reaches_the_bank(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "reject")
    token, account_id = fund_customer(app, client, "reject@example.test", "2000.00")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = open_invoice(app, client, token, "INV-REJ-1", "150.00")
    request = payment_of(app, case["case_id"])

    response = client.post(
        f"/workflow/cases/{case['case_id']}/reject",
        json={"reason": "Vendor bank details are unverified."},
        headers=auth(admin),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "FAILED"

    stored = store_of(app).payment_request(request["payment_id"])
    assert stored["status"] == "REJECTED"
    # The bank cannot process a rejected request.
    assert process(app, client, admin, request["payment_id"]).json()["status"] == REQUEST_FAILED
    rows = store_of(app).ledger_rows_for_payment(request["payment_id"])
    assert all(r["status"] == "FAILED" for r in rows)  # a refusal record, never a credit
    assert bank_accounts(app)[account_id]["balance_minor"] == to_minor("2000.00")


# ---------------------------------------------------------------------------
# 6. Reconciliation mismatch
# ---------------------------------------------------------------------------
def test_reconciliation_mismatch_is_caught(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "mismatch")
    token, _account_id = fund_customer(app, client, "mismatch@example.test", "5000.00")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = open_invoice(app, client, token, "INV-MIS-1", "500.00")
    request = payment_of(app, case["case_id"])
    approve(client, admin, case["case_id"])
    assert process(app, client, admin, request["payment_id"]).json()["status"] == REQUEST_SETTLED

    # Corrupt the bank's settled credit so it no longer matches the debit: the
    # bank's own ledger now contradicts itself, and Agent 2 must catch it.
    conn = store_of(app)._connect()
    try:
        conn.execute(
            "UPDATE mock_bank_ledger SET amount = ?, received_amount = ? "
            "WHERE instruction_id = ? AND direction = 'credit'",
            ("300.00", "300.00", request["payment_id"]),
        )
    finally:
        conn.close()

    recommendation = app.state.orchestrator.reconciliation.reconcile_payment(
        app.state.orchestrator.get(case["case_id"])
    )
    verdict = recommendation.get("verdict")
    assert verdict in {
        "NEEDS_HUMAN_REVIEW",
        "PARTIAL_PAYMENT",
        "OVERPAYMENT",
        "DISCREPANCY_DETECTED",
        "MISSING_PAYMENT",
    }, recommendation


# ---------------------------------------------------------------------------
# 7. Unauthorised payment execution
# ---------------------------------------------------------------------------
def test_unapproved_payment_cannot_be_processed(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "unauth-exec")
    token, account_id = fund_customer(app, client, "unauth@example.test", "3000.00")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = open_invoice(app, client, token, "INV-UNAUTH-1", "200.00")
    request = payment_of(app, case["case_id"])

    # A customer cannot ask the bank to process anything.
    assert process(app, client, token, request["payment_id"]).status_code == 403

    # Even an admin cannot process a request nobody has approved.
    refused = process(app, client, admin, request["payment_id"]).json()
    assert refused["status"] == REQUEST_FAILED
    assert refused["failure_code"] == "APPROVAL_MISSING"
    assert bank_accounts(app)[account_id]["balance_minor"] == to_minor("3000.00")
    rows = store_of(app).ledger_rows_for_payment(request["payment_id"])
    assert all(r["status"] == "FAILED" for r in rows)

    # Direct bank-service calls obey the same rule.
    from workflow.bank_service import BankService

    service = BankService(store_of(app).path)
    outcome = service.submit(request["payment_id"], actor_id="x", actor_role="SYSTEM")
    assert outcome.settled is False and outcome.failure_code == "APPROVAL_MISSING"


# ---------------------------------------------------------------------------
# 8. Customer attempting to access the bank ledger
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/bank/ledger",
        "/bank/transactions",
        "/bank/ledger/BK-ANYTHING",
        "/bank-admin/overview",
        "/bank-admin/requests",
        "/bank-admin/transactions",
        "/bank-admin/audit",
        "/workflow/approvals",
        "/bank-tools/catalogue",
    ],
)
def test_customer_cannot_read_the_bank_ledger(workspace_tmp_path, path):
    app, client = make_app(workspace_tmp_path, "customer-ledger")
    token, _ = fund_customer(app, client, "ledger@example.test")

    assert client.get(path).status_code == 401
    assert client.get(path, headers=auth(token)).status_code == 403

    admin, _ = make_admin(app, client, "bank@example.test")
    assert client.get(path, headers=auth(admin)).status_code in {200, 404}


def test_invoice_history_is_scoped_to_the_owner(workspace_tmp_path):
    """A customer may read invoice history, but only their own invoices."""

    app, client = make_app(workspace_tmp_path, "invoice-history-scope")
    a_token, _ = fund_customer(app, client, "a@example.test", "4000.00")
    _b_token, _ = fund_customer(app, client, "b@example.test", "4000.00")
    open_invoice(app, client, a_token, "INV-OWNED-A", "100.00")

    mine = client.get("/workflow/invoice-history", headers=auth(a_token))
    assert mine.status_code == 200
    assert all(r["invoice_number"] == "INV-OWNED-A" for r in mine.json())
    assert mine.json()  # not empty: the customer sees their own record


# ---------------------------------------------------------------------------
# 9. Customer A attempting to access Customer B's records
# ---------------------------------------------------------------------------
def test_customer_a_cannot_read_customer_b(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "cross-customer")
    a_token, _ = fund_customer(app, client, "a@example.test", "4000.00")
    b_token, _ = fund_customer(app, client, "b@example.test", "4000.00")
    admin, _ = make_admin(app, client, "bank@example.test")

    case_b = open_invoice(app, client, b_token, "INV-B-ONLY", "325.00")
    request_b = payment_of(app, case_b["case_id"])

    for path in (
        f"/workflow/cases/{case_b['case_id']}",
        f"/workflow/cases/{case_b['case_id']}/investigation",
        f"/workflow/cases/{case_b['case_id']}/receipt",
        f"/cases/{case_b['case_id']}/timeline",
    ):
        response = client.get(path, headers=auth(a_token))
        assert response.status_code in {403, 404}, (path, response.status_code, response.text)

    # /workflow/invoices/{n}/payments is scoped: Customer A sees no attempts for
    # B's invoice (an empty, leak-free result), not the rows themselves.
    scoped = client.get("/workflow/invoices/INV-B-ONLY/payments", headers=auth(a_token))
    assert scoped.status_code == 200
    assert scoped.json()["count"] == 0 and scoped.json()["attempts"] == []

    # The bank tools endpoint is scoped the same way.
    tool = client.get(
        f"/bank-tools/agent_4/get_payment_status/{request_b['payment_id']}", headers=auth(a_token)
    )
    assert tool.status_code == 404

    # Customer A's own view contains nothing of Customer B's.
    me_a = client.get("/auth/me", headers=auth(a_token)).json()
    assert all(p["invoice_number"] != "INV-B-ONLY" for p in me_a["bank_payments"])
    assert all(c["invoice_number"] != "INV-B-ONLY" for c in me_a["cases"])

    # Admins may see both, which is what makes the restriction meaningful.
    assert client.get(f"/workflow/cases/{case_b['case_id']}", headers=auth(admin)).status_code == 200


# ---------------------------------------------------------------------------
# 10. Agent attempting to access an unrelated bank case
# ---------------------------------------------------------------------------
def test_agent_tool_refuses_an_unrelated_case(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "agent-scope")
    a_token, _ = fund_customer(app, client, "a@example.test", "4000.00")
    b_token, _ = fund_customer(app, client, "b@example.test", "4000.00")
    admin, _ = make_admin(app, client, "bank@example.test")

    case_a = open_invoice(app, client, a_token, "INV-A-SCOPE", "210.00")
    case_b = open_invoice(app, client, b_token, "INV-B-SCOPE", "220.00")
    request_b = payment_of(app, case_b["case_id"])

    tools = AgentBankTools(store_of(app).path)
    foreign_context = CaseContext(
        agent_id="agent_4",
        case_id=case_a["case_id"],
        customer_id=case_a["owner_account_id"] if "owner_account_id" in case_a else None,
        invoice_id="INV-A-SCOPE",
    )
    for name in (
        "get_payment_status",
        "get_bank_transaction",
        "verify_payment_settlement",
        "get_reconciliation_evidence",
        "get_payment_exception_details",
    ):
        with pytest.raises(BankToolError) as refused:
            getattr(tools, name)(request_b["payment_id"], foreign_context)
        # Every refusal is a denial; the specific reason varies with the tool's
        # permission set for the calling agent.
        assert refused.value.reason in {
            "not_found",
            "invoice_mismatch",
            "customer_mismatch",
            "tool_not_permitted",
        }

    # Least privilege: Agent 1 may only read status; Agent 4 may not reconcile.
    own_context = CaseContext(
        agent_id="agent_1",
        case_id=case_b["case_id"],
        customer_id=None,
        invoice_id="INV-B-SCOPE",
    )
    with pytest.raises(BankToolError) as refused:
        tools.get_reconciliation_evidence(request_b["payment_id"], own_context)
    assert refused.value.reason == "tool_not_permitted"
    with pytest.raises(BankToolError) as refused:
        AgentBankTools(store_of(app).path).get_payment_status(
            request_b["payment_id"],
            CaseContext(agent_id="made_up_agent", case_id=case_b["case_id"], customer_id=None),
        )
    assert refused.value.reason == "unknown_agent"

    # An admin may inspect the same tool call for the right case.
    allowed = client.get(
        f"/bank-tools/agent_4/get_payment_status/{request_b['payment_id']}", headers=auth(admin)
    )
    assert allowed.status_code == 200
    assert allowed.json()["result"]["payment_id"] == request_b["payment_id"]


# ---------------------------------------------------------------------------
# 11. Concurrent payment requests
# ---------------------------------------------------------------------------
def test_concurrent_processing_settles_exactly_once(workspace_tmp_path):
    import threading

    app, client = make_app(workspace_tmp_path, "concurrent")
    token, account_id = fund_customer(app, client, "race@example.test", "9000.00")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = open_invoice(app, client, token, "INV-RACE-1", "700.00")
    request = payment_of(app, case["case_id"])
    approve(client, admin, case["case_id"])

    from workflow.bank_service import BankService

    service = BankService(store_of(app).path)
    outcomes: list[str] = []
    lock = threading.Lock()

    def run():
        result = service.submit(request["payment_id"], actor_id="t", actor_role="SYSTEM")
        with lock:
            outcomes.append(result.status)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes, "no outcome recorded"
    entries = store_of(app).ledger_rows_for_payment(request["payment_id"])
    assert len([e for e in entries if e["direction"] == "debit"]) == 1
    assert len([e for e in entries if e["direction"] == "credit"]) == 1
    assert bank_accounts(app)[account_id]["balance_minor"] == to_minor("9000.00") - to_minor("700.00")


def test_concurrent_request_creation_is_idempotent(workspace_tmp_path):
    app, _client = make_app(workspace_tmp_path, "concurrent-create")
    store = store_of(app)
    source, _ = store.ensure_account(
        owner_id="cust-1", owner_type="CUSTOMER", opening_balance="1000.00"
    )
    payee, _ = store.ensure_account(owner_id="vendor-1", owner_type="VENDOR")

    import threading

    created: list[str] = []
    lock = threading.Lock()

    def run():
        rows, made = store.create_payment_request(
            customer_id="cust-1",
            invoice_id="INV-CONCURRENT-1",
            source_account_id=source["account_id"],
            destination_account_id=payee["account_id"],
            amount="100.00",
            currency="USD",
            idempotency_key="concurrent-key",
        )
        with lock:
            created.append((rows["payment_id"], made))

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({payment_id for payment_id, _ in created}) == 1
    assert sum(1 for _, made in created if made) == 1


# ---------------------------------------------------------------------------
# 12. Retry after an uncertain transaction
# ---------------------------------------------------------------------------
def test_uncertain_payment_is_investigated_not_retried(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "uncertain")
    token, account_id = fund_customer(app, client, "uncertain@example.test", "6000.00")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = open_invoice(app, client, token, "INV-UNC-1", "450.00")
    request = payment_of(app, case["case_id"])
    approve(client, admin, case["case_id"])

    store = store_of(app)
    store.mark_uncertain(
        request["payment_id"], reason="The bank did not respond.", actor_id="t", actor_role="SYSTEM"
    )
    assert store.payment_request(request["payment_id"])["status"] == REQUEST_UNCERTAIN

    from workflow.bank_service import BankService

    service = BankService(store.path)
    # Processing an uncertain request is refused and creates nothing.
    outcome = service.submit(request["payment_id"], actor_id="t", actor_role="SYSTEM")
    assert outcome.settled is False
    assert outcome.status == REQUEST_UNCERTAIN
    assert store.ledger_rows_for_payment(request["payment_id"]) == []
    assert bank_accounts(app)[account_id]["balance_minor"] == to_minor("6000.00")

    # A customer cannot push it through either, and no second request exists.
    assert process(app, client, admin, request["payment_id"]).json()["status"] in {
        REQUEST_FAILED,
        REQUEST_UNCERTAIN,
    }
    assert len(store.payment_requests(invoice_id="INV-UNC-1")) == 1

    # The case is closed with an investigation, not a retry.
    final = case_of(client, token, case["case_id"])
    assert final["status"] in {"PENDING", "REQUIRES_REVIEW"}
    assert any(r["agent"] == "agent_4" for r in final["reports"])
    assert final["customer_response"]["payment_settled"] is False


# ---------------------------------------------------------------------------
# 13. Money is conserved
# ---------------------------------------------------------------------------
def test_total_simulated_funds_are_conserved(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "conservation")
    token_a, _ = fund_customer(app, client, "a@example.test", "7000.00")
    token_b, _ = fund_customer(app, client, "b@example.test", "3000.00")
    admin, _ = make_admin(app, client, "bank@example.test")

    before = total_funds(app)
    assert before == to_minor("10000.00")

    for number, amount in (("INV-CONS-1", "120.00"), ("INV-CONS-2", "850.00")):
        case = open_invoice(app, client, token_a if number.endswith("1") else token_b, number, amount)
        request = payment_of(app, case["case_id"])
        approve(client, admin, case["case_id"])
        assert process(app, client, admin, request["payment_id"]).json()["status"] == REQUEST_SETTLED

    assert total_funds(app) == before  # every cent is still in some simulated account

    # A duplicate processing run cannot mint money.
    settled = store_of(app).payment_requests(statuses={REQUEST_SETTLED}, limit=10)
    for row in settled:
        process(app, client, admin, row["payment_id"])
    assert total_funds(app) == before

    # A failed payment cannot destroy or create money either.
    token_c, _ = fund_customer(app, client, "c@example.test", "50.00")
    funded = total_funds(app)
    broken = open_invoice(app, client, token_c, "INV-CONS-3", "900.00")
    req = payment_of(app, broken["case_id"])
    approve(client, admin, broken["case_id"])
    assert process(app, client, admin, req["payment_id"]).json()["status"] == REQUEST_FAILED
    assert total_funds(app) == funded


# ---------------------------------------------------------------------------
# Money primitives
# ---------------------------------------------------------------------------
def test_money_is_integer_minor_units():
    assert to_minor("1620.00") == 162_000
    assert to_minor(Decimal("0.01")) == 1
    assert from_minor(162_000) == "1620.00"
    assert from_minor(1) == "0.01"
    with pytest.raises(ValueError):
        to_minor("1.005")  # more than two decimals is refused, never rounded


def test_frozen_account_blocks_the_payment(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "frozen")
    token, account_id = fund_customer(app, client, "frozen@example.test", "4000.00")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = open_invoice(app, client, token, "INV-FRZ-1", "300.00")
    request = payment_of(app, case["case_id"])
    approve(client, admin, case["case_id"])

    store_of(app).set_account_status(
        account_id, ACCOUNT_FROZEN, actor_id="admin", actor_role=ROLE_BANK_ADMIN
    )
    result = process(app, client, admin, request["payment_id"]).json()
    assert result["status"] == REQUEST_FAILED
    assert result["failure_code"] == "ACCOUNT_BLOCKED"
    rows = store_of(app).ledger_rows_for_payment(request["payment_id"])
    assert all(r["status"] == "FAILED" for r in rows)  # no credit was posted


def test_demo_admin_can_process_and_audit_is_written(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "audit")
    token, _ = fund_customer(app, client, "audit@example.test", "4000.00")
    demo, _ = make_admin(app, client, "demo@example.test", role=ROLE_DEMO_ADMIN, name="Demo")
    case = open_invoice(app, client, token, "INV-AUD-1", "180.00")
    request = payment_of(app, case["case_id"])
    approve(client, demo, case["case_id"])
    assert process(app, client, demo, request["payment_id"]).json()["status"] == REQUEST_SETTLED

    logs = client.get("/bank-admin/audit", headers=auth(demo)).json()["logs"]
    actions = {entry["action"] for entry in logs}
    assert {"payment_requested", "payment_approved", "payment_settled"} <= actions
    settled = next(e for e in logs if e["action"] == "payment_settled")
    assert settled["actor_role"] == ROLE_DEMO_ADMIN
