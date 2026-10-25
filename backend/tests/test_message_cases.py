"""POST /workflow/cases/message: a customer writes instead of uploading a PDF."""

from __future__ import annotations

from workflow.accounts import ROLE_BANK_ADMIN

from http_helpers import auth, make_admin, make_app, message, reply, signup


def test_message_opens_an_owned_case_through_agent3(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "msg-ok")
    token, account_id = signup(client, "c@example.test")
    text = "Please pay invoice INV-MSG-1 from Northstar Office Supplies LLC for 120.00 USD."

    response = message(app, client, token, text, reply("INV-MSG-1", "120.00", document_type="other"))

    assert response.status_code == 201, response.text
    case = response.json()
    assert case["owner_account_id"] == account_id
    assert case["source_channel"] == "message"
    assert case["invoice_number"] == "INV-MSG-1"
    assert case["analysis"]["analysis_source"] == "live_llm"
    # Same pipeline as an upload: Agent 1 has reported and a person must approve.
    assert case["status"] == "AWAITING_APPROVAL"
    assert any(r["agent"] == "agent_1" for r in case["reports"])
    listing = client.get("/workflow/cases", headers=auth(token)).json()
    assert any(item["case_id"] == case["case_id"] for item in listing)


def test_message_without_invoice_number_goes_to_review_saying_so(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "msg-noid")
    token, _ = signup(client, "c@example.test")

    case = message(
        app, client, token, "I was charged twice last month, please help.",
        reply(None, None, primary="duplicate_payment", problem="payment_status_discrepancy",
              document_type="other"),
    ).json()

    assert case["status"] == "REQUIRES_REVIEW"
    assert case["invoice_number"] is None
    assert "invoice number" in case["pending_reason"]
    assert [r["agent"] for r in sorted(case["agent_runs"], key=lambda r: r["order"])] == [
        "agent_3", "human_review", "agent_5"
    ]
    assert "invoice number" in " ".join(case["customer_response"]["next_steps"])


def test_unclear_message_names_what_is_missing(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "msg-unclear")
    token, _ = signup(client, "c@example.test")

    case = message(
        app, client, token, "Something is wrong, call me.",
        reply(None, None, primary="unknown", document_type="other"),
    ).json()

    assert case["status"] == "REQUIRES_REVIEW"
    assert "invoice number" in case["pending_reason"]
    assert "clear description of the problem" in case["pending_reason"]
    assert case["customer_response"]["outcome"] == "NEEDS_REVIEW"


def test_payment_message_without_amount_is_not_payable(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "msg-noamount")
    token, _ = signup(client, "c@example.test")

    case = message(
        app, client, token, "Please pay invoice INV-MSG-4 from Northstar Office Supplies LLC.",
        reply("INV-MSG-4", None, document_type="other"),
    ).json()

    assert case["status"] == "REQUIRES_REVIEW"
    assert "the amount to pay" in case["pending_reason"]


def test_identifier_not_in_the_message_is_never_used(workspace_tmp_path):
    """Never act on an invoice number the customer did not write."""

    app, client = make_app(workspace_tmp_path, "msg-invented")
    token, _ = signup(client, "c@example.test")

    case = message(
        app, client, token, "My payment for last month's office supplies did not go through.",
        reply("INV-INVENTED-9", None, primary="payment_failure", problem="payment_status_discrepancy",
              document_type="other"),
    ).json()

    assert case["invoice_number"] is None
    assert "INV-INVENTED-9" not in str(case["customer_response"])
    assert case["status"] == "REQUIRES_REVIEW"
    assert "invoice number" in case["pending_reason"]


def test_orchestrator_discards_identifier_missing_from_message(workspace_tmp_path):
    """Defence in depth behind Agent 3's grounding, at the orchestrator."""

    from workflow.orchestrator import WorkflowOrchestrator
    from workflow.repository import WorkflowRepository

    from test_workflow import build_analysis

    orchestrator = WorkflowOrchestrator(WorkflowRepository(workspace_tmp_path / "guard.db"))
    analysis = build_analysis(invoice_number="INV-NOT-WRITTEN", primary="payment_request")

    case = orchestrator.route(orchestrator.open_case(
        analysis, "customer message", channel="message",
        source_text="Please pay the Northstar invoice for 1000.00 USD.",
    ))

    assert case.invoice_number is None
    assert any(e.action == "identifier_discarded" for e in case.audit)
    assert case.status.value == "REQUIRES_REVIEW"
    assert "invoice number" in case.pending_reason

    written = orchestrator.open_case(
        build_analysis(invoice_number="INV-WRITTEN-1", primary="payment_request"),
        "customer message", channel="message", source_text="Please pay inv-written-1 for 1000.00 USD.",
    )
    assert written.invoice_number == "INV-WRITTEN-1"


def test_empty_or_missing_message_is_400(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "msg-empty")
    token, _ = signup(client, "c@example.test")

    for body in ({"message": ""}, {"message": "   "}, {}, {"message": 42}):
        response = client.post("/workflow/cases/message", json=body, headers=auth(token))
        assert response.status_code == 400, body
    assert app.state.model.calls == []


def test_message_requires_a_signed_in_customer(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "msg-auth")
    admin, _ = make_admin(app, client, "bank@example.test", role=ROLE_BANK_ADMIN)

    assert client.post("/workflow/cases/message", json={"message": "hi"}).status_code == 401
    response = client.post("/workflow/cases/message", json={"message": "hi"}, headers=auth(admin))
    assert response.status_code == 403
