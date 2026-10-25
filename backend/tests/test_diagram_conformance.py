"""The product diagram, as executable acceptance criteria.

"GhostLedger: AI Agents for Invoice Payments and Customer Support"

    Customer uploads a PDF or sends a message
        -> Agent 3 reads, extracts, verifies, identifies intent, routes

    Path A (pay invoice):
        Agent 1 -> Human approval (bank side) -> Payment simulator
            success -> Agent 2 -> Agent 5
            failure -> Agent 4 -> Agent 5

    Path B (customer problem):
        duplicate invoice / double payment  -> Agent 1 -> Agent 5
        payment status / missing money      -> Agent 2 -> Agent 5
        rejected payment / exception        -> Agent 4 -> Agent 5
        unclear                             -> human review -> Agent 5

Every test drives the real API over HTTP. Only the remote LLM boundary is
replaced, using the suite's fake-extractor pattern (see ``http_helpers``).
Each asserts the exact order and status of every workflow step.
"""

from __future__ import annotations

from http_helpers import (
    auth,
    invoice_text,
    make_admin,
    make_app,
    message,
    reply,
    runs,
    signup,
    upload,
)

C, W, S = "completed", "waiting", "skipped"
RH, B = "requires_human", "blocked"


def setup(workspace_tmp_path, name):
    app, client = make_app(workspace_tmp_path, name)
    customer, customer_id = signup(client, f"{name}-a@example.test", "Customer A")
    admin, _admin_id = make_admin(app, client, f"{name}-bank@example.test")
    return app, client, customer, admin, customer_id


def request_payment(app, client, customer, invoice, amount):
    response = upload(app, client, customer, invoice_text(invoice, amount), reply(invoice, amount))
    assert response.status_code == 201, response.text
    return response.json()


def approve(client, admin, case_id, **body):
    response = client.post(f"/workflow/cases/{case_id}/approve", json=body, headers=auth(admin))
    assert response.status_code == 200, response.text
    return response.json()


def assert_final_answer(case, outcome, settled=False):
    response = case["customer_response"]
    assert response is not None, "Agent 5 must answer every case that ends"
    assert response["outcome"] == outcome
    assert response["payment_settled"] is settled
    assert response["answer"] and response["next_steps"]


# ---------------------------------------------------------------------------
# Path A
# ---------------------------------------------------------------------------
def test_path_a_success(workspace_tmp_path):
    app, client, customer, admin, _ = setup(workspace_tmp_path, "a-ok")
    case = request_payment(app, client, customer, "INV-DC-1", "410.00")

    # Agent 1 has reported before the case is awaiting approval.
    assert case["status"] == "AWAITING_APPROVAL"
    assert runs(case) == [
        ("agent_3", C), ("agent_1", C), ("human_approval", RH),
        ("payment_simulator", W), ("agent_2", W), ("agent_5", W),
    ]
    actions = [(e["actor"], e["action"]) for e in case["audit"]]
    assert actions.index(("agent_1", "agent_completed")) < actions.index(("orchestrator", "awaiting_approval"))
    assert any(r["agent"] == "agent_1" for r in case["reports"])

    paid = approve(client, admin, case["case_id"])

    assert paid["status"] == "COMPLETED"
    assert runs(paid) == [
        ("agent_3", C), ("agent_1", C), ("human_approval", C),
        ("payment_simulator", C), ("agent_2", C), ("agent_5", C),
    ]
    assert_final_answer(paid, "PAID", settled=True)


def test_path_a_payment_failure(workspace_tmp_path):
    app, client, customer, admin, _ = setup(workspace_tmp_path, "a-fail")
    case = request_payment(app, client, customer, "INV-DC-2", "420.00")

    failed = approve(client, admin, case["case_id"], simulate="FAILED")

    assert failed["status"] == "REQUIRES_REVIEW"
    assert runs(failed) == [
        ("agent_3", C), ("agent_1", C), ("human_approval", C), ("payment_simulator", C),
        ("agent_2", S), ("agent_4", RH), ("agent_5", C), ("human_review", RH),
    ]
    assert failed["receipt"] is None
    assert_final_answer(failed, "PAYMENT_FAILED")


def test_path_a_duplicate_invoice_is_flagged_before_approval(workspace_tmp_path):
    """The same invoice, already paid, is submitted for payment again."""

    app, client, customer, admin, _ = setup(workspace_tmp_path, "a-dup")
    approve(client, admin, request_payment(app, client, customer, "INV-DC-3", "430.00")["case_id"])

    again = request_payment(app, client, customer, "INV-DC-3", "430.00")

    assert again["status"] == "REQUIRES_REVIEW"
    assert runs(again) == [
        ("agent_3", C), ("agent_1", B), ("human_approval", S),
        ("payment_simulator", S), ("agent_2", S), ("agent_5", C), ("human_review", RH),
    ]
    assert "already paid" in again["pending_reason"]
    assert_final_answer(again, "BLOCKED_DUPLICATE")
    # It never became approvable.
    response = client.post(f"/workflow/cases/{again['case_id']}/approve", json={}, headers=auth(admin))
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Path B
# ---------------------------------------------------------------------------
def test_path_b_duplicate_invoice_reported_by_customer(workspace_tmp_path):
    app, client, customer, admin, _ = setup(workspace_tmp_path, "b-dupinv")
    approve(client, admin, request_payment(app, client, customer, "INV-DC-4", "440.00")["case_id"])

    note = "I received this invoice twice."
    case = upload(
        app, client, customer, invoice_text("INV-DC-4", "440.00", note),
        reply("INV-DC-4", "440.00", primary="duplicate_invoice",
              problem="other_financial_issue", evidence=note),
    ).json()

    assert runs(case) == [
        ("agent_3", C), ("agent_1", RH), ("agent_2", S), ("agent_5", RH), ("human_review", RH),
    ]
    assert_final_answer(case, "DUPLICATE_CHECKED")


def test_path_b_duplicate_payment(workspace_tmp_path):
    app, client, customer, admin, _ = setup(workspace_tmp_path, "b-duppay")
    approve(client, admin, request_payment(app, client, customer, "INV-DC-5", "450.00")["case_id"])

    note = "I paid this invoice twice. Amount paid: 900.00"
    case = upload(
        app, client, customer, invoice_text("INV-DC-5", "450.00", note),
        reply("INV-DC-5", "450.00", primary="duplicate_payment", problem="payment_status_discrepancy",
              paid="900.00", count=2, evidence="I paid this invoice twice."),
    ).json()

    assert case["intent"] == "duplicate_payment"
    assert runs(case) == [
        ("agent_3", C), ("agent_1", RH), ("agent_2", S), ("agent_5", RH), ("human_review", RH),
    ]
    assert_final_answer(case, "DUPLICATE_CHECKED")
    assert "1 settled payment" in case["customer_response"]["answer"]


def test_path_b_payment_status_dispute_missing_money(workspace_tmp_path):
    app, client, customer, _admin, _ = setup(workspace_tmp_path, "b-status")

    note = "I already paid this invoice. Amount paid: 460.00"
    case = upload(
        app, client, customer, invoice_text("INV-DC-6", "460.00", note),
        reply("INV-DC-6", "460.00", primary="bank_reconciliation", problem="payment_status_discrepancy",
              paid="460.00", evidence="I already paid this invoice."),
    ).json()

    assert case["intent"] == "payment_status_dispute"
    assert runs(case) == [("agent_3", C), ("agent_2", RH), ("agent_5", RH), ("human_review", RH)]
    assert_final_answer(case, "PAYMENT_NOT_FOUND")


def test_path_b_rejected_payment(workspace_tmp_path):
    app, client, customer, admin, _ = setup(workspace_tmp_path, "b-reject")
    approve(client, admin, request_payment(app, client, customer, "INV-DC-7", "470.00")["case_id"],
            simulate="FAILED")

    note = "My payment was rejected by the bank."
    case = upload(
        app, client, customer, invoice_text("INV-DC-7", "470.00", note),
        reply("INV-DC-7", "470.00", primary="payment_failure", problem="payment_status_discrepancy",
              evidence=note),
    ).json()

    assert case["intent"] == "payment_failure"
    assert runs(case) == [("agent_3", C), ("agent_4", RH), ("agent_5", RH), ("human_review", RH)]
    assert_final_answer(case, "FAILURE_CHECKED")


def test_text_message_runs_the_same_pipeline(workspace_tmp_path):
    app, client, customer, _admin, customer_id = setup(workspace_tmp_path, "msg")
    text = "I paid invoice INV-DC-8 for 480.00 USD to Northstar Office Supplies LLC but it still shows unpaid."

    response = message(
        app, client, customer, text,
        reply("INV-DC-8", "480.00", primary="bank_reconciliation", problem="payment_status_discrepancy",
              paid="480.00", document_type="other"),
    )

    assert response.status_code == 201, response.text
    case = response.json()
    assert case["source_channel"] == "message"
    assert case["owner_account_id"] == customer_id
    assert app.state.model.calls[-1].count(text) == 1, "Agent 3 read the message text"
    assert runs(case) == [("agent_3", C), ("agent_2", RH), ("agent_5", RH), ("human_review", RH)]
    assert_final_answer(case, "PAYMENT_NOT_FOUND")


def test_human_review_for_unclear_document(workspace_tmp_path):
    app, client, customer, _admin, _ = setup(workspace_tmp_path, "review")

    case = upload(
        app, client, customer, "FIELD SERVICE REPORT\nReference: FSR-DC-9\nNo amounts on this form.",
        reply("FSR-DC-9", None, primary="unknown", document_type="other"),
    ).json()

    assert case["status"] == "REQUIRES_REVIEW"
    assert runs(case) == [("agent_3", C), ("human_review", RH), ("agent_5", C)]
    assert_final_answer(case, "NEEDS_REVIEW")


# ---------------------------------------------------------------------------
# Roles and isolation
# ---------------------------------------------------------------------------
def test_customer_cannot_approve_or_see_the_ledger(workspace_tmp_path):
    app, client, customer, _admin, _ = setup(workspace_tmp_path, "roles")
    case = request_payment(app, client, customer, "INV-DC-10", "500.00")

    approve_response = client.post(f"/workflow/cases/{case['case_id']}/approve", json={}, headers=auth(customer))
    assert approve_response.status_code == 403
    assert "cannot approve" in approve_response.json()["detail"]
    assert client.post(
        f"/workflow/cases/{case['case_id']}/reject", json={}, headers=auth(customer)
    ).status_code == 403
    assert client.get("/bank/ledger", headers=auth(customer)).status_code == 403
    assert client.get("/workflow/approvals", headers=auth(customer)).status_code == 403


def test_customer_a_cannot_read_customer_b_case(workspace_tmp_path):
    app, client, customer_a, _admin, _ = setup(workspace_tmp_path, "isolation")
    customer_b, _ = signup(client, "isolation-b@example.test", "Customer B")
    case = request_payment(app, client, customer_b, "INV-DC-11", "510.00")

    assert client.get(f"/workflow/cases/{case['case_id']}", headers=auth(customer_a)).status_code == 404
    assert client.get(f"/workflow/cases/{case['case_id']}", headers=auth(customer_b)).status_code == 200
    listing = client.get("/workflow/cases", headers=auth(customer_a)).json()
    assert all(item["case_id"] != case["case_id"] for item in listing)


def test_model_reply_payment_status_dispute_routes_to_agent2(workspace_tmp_path):
    """Regression: the enum allows this value although the prompt never asks for
    it; Agent 3 used to crash with KeyError (HTTP 500) when a model sent it."""

    app, client, customer, _admin, _ = setup(workspace_tmp_path, "psd")
    note = "I already paid this invoice. Amount paid: 520.00"

    response = upload(
        app, client, customer, invoice_text("INV-DC-12", "520.00", note),
        reply("INV-DC-12", "520.00", primary="payment_status_dispute",
              problem="payment_status_discrepancy", paid="520.00", evidence="I already paid this invoice."),
    )

    assert response.status_code == 201, response.text
    case = response.json()
    assert case["analysis"]["routing"]["recommended_agent"] == "agent_2"
    assert case["intent"] == "payment_status_dispute"
    assert runs(case) == [("agent_3", C), ("agent_2", RH), ("agent_5", RH), ("human_review", RH)]
