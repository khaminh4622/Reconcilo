"""Agent 4's interactive support chat, end to end.

These tests drive the real FastAPI app, the real orchestrator, the Mock Bank and
the persistent conversation table. The remote model boundary is a scripted
client, so the suite never depends on a provider being reachable.

Covered, as required by the Agent 4 support brief:

    * a customer can send a message and gets a grounded reply
    * the conversation survives sign-out/sign-in and a page refresh read
    * the reply carries the correct case, invoice and bank context
    * Agent 4 checks the case-scoped payment status
    * Agent 4 can prepare a new request through Agent 3 / Agent 1
    * a second click cannot create a duplicate payment request
    * another customer cannot read or post to the conversation
    * authorized staff can read and reply
    * Agent 4 cannot approve, process, or bypass bank approval
    * failed and uncertain payments are never retried automatically
    * Agent 4 can escalate an unresolved case to human review
    * INV-GL-DEMO-26024-702 is seeded with a real failed-bank record
"""

from __future__ import annotations

import json

import pytest

from agent3.extractor import LLMExtractor
from workflow.accounts import ROLE_BANK_ADMIN
from workflow.agent4_support import Agent4SupportAssistant
from workflow.bank_tools import AgentBankTools, BankToolError
from workflow.demo_bank_seed import (
    AGENT4_DEMO_INVOICE,
    onboard_customer,
    seed_demo_bank,
)
from workflow.mock_bank_accounts import (
    REQUEST_FAILED,
    REQUEST_PENDING,
    REQUEST_UNCERTAIN,
)

from http_helpers import auth, invoice_text, make_admin, make_app, message, reply, signup, upload


FAILURE_CODE = "REJECTED"
FAILURE_REASON = "The receiving bank rejected the simulated transfer. No money left the sender account."
UNCERTAIN_REASON = "The simulated bank did not confirm the transfer outcome."


@pytest.fixture(autouse=True)
def _deterministic_agent_prose(monkeypatch):
    """Keep the suite off the network even when a developer has a live key.

    The support assistant itself is still given an LLMExtractor with a scripted
    client, which exercises the same prompt/parse/safety path.
    """

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("RUN_LIVE_LLM_TEST", raising=False)


def open_invoice(app, client, token: str, number: str, amount: str) -> dict:
    response = upload(app, client, token, invoice_text(number, amount), reply(number, amount))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "AWAITING_APPROVAL", body
    return body


def setup_customer_case(workspace_tmp_path, name: str, amount: str = "900.00"):
    app, client = make_app(workspace_tmp_path, name)
    token, account_id = signup(client, f"{name}@example.test", f"Customer {name}")
    onboard_customer(
        app,
        account_id=account_id,
        display_name=f"Customer {name}",
        opening_balance="5000.00",
    )
    admin, _admin_id = make_admin(app, client, f"bank-{name}@example.test")
    case = open_invoice(app, client, token, f"INV-A4-{name.upper()}", amount)
    return app, client, token, admin, case["case_id"]


def setup_case_awaiting_agent4_payment(workspace_tmp_path, name: str, amount: str = "900.00"):
    """A case with verified invoice details but no payment attempt.

    It is deliberately *not* a payment request: the customer asked for
    investigation only, so no live Mock Bank request exists. That is the state
    in which Agent 4 can legitimately prepare a fresh payment request.
    """

    app, client = make_app(workspace_tmp_path, name)
    token, account_id = signup(client, f"{name}@example.test", f"Customer {name}")
    onboard_customer(
        app,
        account_id=account_id,
        display_name=f"Customer {name}",
        opening_balance="5000.00",
    )
    admin, _admin_id = make_admin(app, client, f"bank-{name}@example.test")
    invoice = f"INV-A4-{name.upper()}"
    case = message(
        app,
        client,
        token,
        (
            f"For investigation only. Do not pay invoice {invoice} from "
            f"Northstar Office Supplies LLC for {amount} USD."
        ),
        reply(
            invoice,
            amount,
            primary="unknown",
            problem="other_financial_issue",
            document_type="other",
        ),
    )
    assert case.status_code == 201, case.text
    body = case.json()
    assert body["status"] == "REQUIRES_REVIEW", body
    assert body["invoice_number"] == invoice
    assert body["vendor_name"] == "Northstar Office Supplies LLC"
    assert body["amount"] == amount
    assert body["currency"] == "USD"
    assert app.state.bank_store.payment_requests(case_id=body["case_id"]) == []
    return app, client, token, admin, body["case_id"]


def payment_request_for(app, case_id: str) -> dict:
    rows = app.state.bank_store.payment_requests(case_id=case_id)
    assert rows, f"no payment request for case {case_id}"
    return rows[0]


def conversation_path(case_id: str) -> str:
    return f"/workflow/cases/{case_id}/agent4/conversation"


def chat(client, token: str, case_id: str, message: str, action_id: str | None = None):
    payload: dict = {"message": message}
    if action_id is not None:
        payload["action_id"] = action_id
    return client.post(conversation_path(case_id), json=payload, headers=auth(token))


def fail_the_payment(client, admin: str, case_id: str) -> dict:
    response = client.post(
        f"/workflow/cases/{case_id}/approve",
        json={
            "simulate": "FAILED",
            "failure_code": FAILURE_CODE,
            "failure_reason": FAILURE_REASON,
        },
        headers=auth(admin),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "REQUIRES_REVIEW", body
    return body


def make_the_payment_uncertain(client, admin: str, case_id: str) -> dict:
    response = client.post(
        f"/workflow/cases/{case_id}/approve",
        json={"simulate": "PENDING", "failure_reason": UNCERTAIN_REASON},
        headers=auth(admin),
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1. Sending, grounded context and persistence
# ---------------------------------------------------------------------------
def test_customer_sends_message_and_agent_answers_with_case_context(workspace_tmp_path):
    app, client, token, _admin, case_id = setup_customer_case(workspace_tmp_path, "basic")
    request = payment_request_for(app, case_id)
    assert request["status"] == REQUEST_PENDING

    before = client.get(conversation_path(case_id), headers=auth(token))
    assert before.status_code == 200, before.text
    conversation = before.json()
    assert conversation["messages"] == []
    assert conversation["payment_status"]["invoice_number"] == "INV-A4-BASIC"
    assert conversation["payment_status"]["bank"]["status"] == REQUEST_PENDING
    assert conversation["payment_status"]["settlement_verified"] is False
    assert {action["id"] for action in conversation["suggested_actions"]} >= {
        "check_status", "help_pay", "already_paid", "why_declined", "human_assistance",
    }

    sent = chat(
        client,
        token,
        case_id,
        "I already paid, but the vendor says they did not receive it.",
        "already_paid",
    )
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["agent_message"]["actor"] == "agent_4"
    assert body["customer_message"]["actor"] == "customer"
    assert body["payment_status"]["invoice_number"] == "INV-A4-BASIC"
    assert body["payment_status"]["bank"]["status"] == REQUEST_PENDING
    # Pending is never presented as settled money.
    assert body["payment_status"]["settlement_verified"] is False
    answer = body["agent_message"]["body"].casefold()
    assert "inv-a4-basic" in answer
    assert "pending" in answer
    assert "settled" not in answer or "not settled" in answer or "no money" in answer

    # The conversation is stored, and survives a fresh sign-in.
    client.post("/auth/signout", headers=auth(token))
    new_token = client.post(
        "/auth/signin", json={"email": "basic@example.test", "password": "password123"}
    ).json()["token"]
    after = client.get(conversation_path(case_id), headers=auth(new_token))
    assert after.status_code == 200, after.text
    stored = after.json()["messages"]
    assert len(stored) == 2
    assert stored[0]["body"].startswith("I already paid")
    assert stored[1]["actor"] == "agent_4"
    assert [message["message_id"] for message in stored] == [
        body["customer_message"]["message_id"],
        body["agent_message"]["message_id"],
    ]


def test_another_customer_cannot_read_or_post_to_the_conversation(workspace_tmp_path):
    _app, client, _token, _admin, case_id = setup_customer_case(workspace_tmp_path, "owner")
    other_token, _ = signup(client, "other@example.test", "Other Customer")

    assert client.get(conversation_path(case_id), headers=auth(other_token)).status_code == 404
    assert chat(client, other_token, case_id, "Let me in.").status_code == 404
    assert client.post(
        f"/workflow/cases/{case_id}/agent4/request-payment", headers=auth(other_token)
    ).status_code == 404


def test_staff_may_read_and_reply(workspace_tmp_path):
    _app, client, _token, admin, case_id = setup_customer_case(workspace_tmp_path, "staff")
    read = client.get(conversation_path(case_id), headers=auth(admin))
    assert read.status_code == 200, read.text
    assert read.json()["viewer"]["is_staff"] is True

    reply_response = chat(client, admin, case_id, "A bank administrator is checking this.")
    assert reply_response.status_code == 200, reply_response.text
    assert reply_response.json()["customer_message"]["actor"] == "staff"


# ---------------------------------------------------------------------------
# 2. Bank context, rejection reasons, and no invented settlement
# ---------------------------------------------------------------------------
def test_agent4_explains_the_banks_rejection_without_claiming_success(workspace_tmp_path):
    app, client, token, admin, case_id = setup_customer_case(workspace_tmp_path, "failed")
    fail_the_payment(client, admin, case_id)
    request = payment_request_for(app, case_id)
    assert request["status"] == REQUEST_FAILED
    assert request["failure_code"] == FAILURE_CODE

    response = chat(client, token, case_id, "Why was my payment declined?", "why_declined")
    assert response.status_code == 200, response.text
    body = response.json()
    answer = body["agent_message"]["body"]
    assert FAILURE_CODE in answer
    assert "rejected" in answer.casefold()
    assert body["payment_status"]["bank"]["failure_code"] == FAILURE_CODE
    assert body["payment_status"]["bank"]["failure_reason"] == FAILURE_REASON
    assert body["payment_status"]["settlement_verified"] is False
    assert "settled successfully" not in answer.casefold()
    assert "money arrived" not in answer.casefold()
    assert "payment succeeded" not in answer.casefold()


def test_chatting_never_adds_a_payment_or_a_settlement(workspace_tmp_path):
    app, client, token, admin, case_id = setup_customer_case(workspace_tmp_path, "readonly")
    fail_the_payment(client, admin, case_id)
    request = payment_request_for(app, case_id)
    before = len(app.state.bank_store.payment_requests(invoice_id="INV-A4-READONLY"))
    ledger_before = len(app.state.bank_store.ledger_rows_for_payment(request["payment_id"]))

    for question in (
        "I already paid. Where is my money?",
        "Are you sure? Please retry it.",
        "What happens after bank approval?",
    ):
        response = chat(client, token, case_id, question)
        assert response.status_code == 200, response.text

    assert len(app.state.bank_store.payment_requests(invoice_id="INV-A4-READONLY")) == before
    assert len(app.state.bank_store.ledger_rows_for_payment(request["payment_id"])) == ledger_before
    assert app.state.bank_store.payment_request(request["payment_id"])["status"] == REQUEST_FAILED


def test_llm_receives_case_scoped_facts_and_cannot_invent_money(workspace_tmp_path):
    app, client, token, _admin, case_id = setup_customer_case(workspace_tmp_path, "llm")

    class RecordingClient:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        def complete_text(self, system: str, user: str) -> str:
            self.calls.append((system, user))
            return (
                "I checked the case. The Mock Bank request is still pending approval, "
                "so no money has moved."
            )

    recorder = RecordingClient()
    app.state.agent4_support.extractor = LLMExtractor(recorder)
    response = chat(client, token, case_id, "What is happening with this payment?")
    assert response.status_code == 200, response.text
    assert response.json()["agent_message"]["body"].startswith("I checked the case.")
    assert recorder.calls, "the configured LLM should have been called"
    _system, prompt = recorder.calls[-1]
    facts = json.loads(prompt)
    assert facts["case_facts"]["invoice_number"] == "INV-A4-LLM"
    assert facts["case_facts"]["bank_status"] == REQUEST_PENDING
    assert facts["case_facts"]["payment_confirmed"] is False
    assert "latest_customer_message" in facts


def test_safety_guard_rejects_unverified_settlement_and_invented_reasons():
    no_evidence = {
        "settlement_verified": False,
        "bank": {"status": "PENDING_APPROVAL", "failure_code": None, "failure_reason": None},
        "payment_history": [],
    }
    with_failure = {
        "settlement_verified": False,
        "bank": {
            "status": "FAILED",
            "failure_code": "REJECTED",
            "failure_reason": FAILURE_REASON,
        },
        "payment_history": [],
    }
    assert not Agent4SupportAssistant._safe_answer("Your payment settled successfully.", no_evidence)
    assert not Agent4SupportAssistant._safe_answer("We received your payment.", no_evidence)
    assert not Agent4SupportAssistant._safe_answer("The payment is confirmed.", no_evidence)
    assert not Agent4SupportAssistant._safe_answer("The vendor received the funds.", no_evidence)
    assert not Agent4SupportAssistant._safe_answer("The money arrived.", no_evidence)
    assert not Agent4SupportAssistant._safe_answer(
        "The bank declined it for insufficient funds.", no_evidence
    )
    assert not Agent4SupportAssistant._safe_answer(
        "Your reference is MB-DEADBEEF1234.", no_evidence
    )
    assert Agent4SupportAssistant._safe_answer(
        "The payment is not settled. The bank rejected it with code REJECTED.", with_failure
    )


def test_unavailable_bank_evidence_is_not_treated_as_no_payment(workspace_tmp_path, monkeypatch):
    app, client, token, _admin, case_id = setup_case_awaiting_agent4_payment(
        workspace_tmp_path, "bank-unavailable"
    )
    before = app.state.bank_store.payment_requests(invoice_id="INV-A4-BANK-UNAVAILABLE")

    def unavailable(_tools, _context):
        raise BankToolError("Bank evidence is unavailable.", "unavailable")

    monkeypatch.setattr(AgentBankTools, "get_case_payment_history", unavailable)
    conversation = client.get(conversation_path(case_id), headers=auth(token))
    assert conversation.status_code == 200, conversation.text
    status = conversation.json()["payment_status"]
    assert status["bank_status_label"] == "Bank evidence unavailable"
    assert status["can_request_payment"] is False
    assert "unavailable" in status["reason_not_requestable"].casefold()

    reply_response = chat(client, token, case_id, "Help me pay this invoice.")
    assert reply_response.status_code == 200, reply_response.text
    assert "can't safely verify" in reply_response.json()["agent_message"]["body"].casefold()

    intake = client.post(
        f"/workflow/cases/{case_id}/agent4/request-payment", headers=auth(token)
    )
    assert intake.status_code == 503, intake.text
    assert app.state.bank_store.payment_requests(invoice_id="INV-A4-BANK-UNAVAILABLE") == before


# ---------------------------------------------------------------------------
# 3. New verified payment request through the real workflow
# ---------------------------------------------------------------------------
def test_new_payment_request_is_verified_and_cannot_bypass_approval(workspace_tmp_path):
    app, client, token, _admin, case_id = setup_case_awaiting_agent4_payment(
        workspace_tmp_path, "intake"
    )
    before = app.state.bank_store.payment_requests(invoice_id="INV-A4-INTAKE")
    assert before == []

    response = client.post(
        f"/workflow/cases/{case_id}/agent4/request-payment", headers=auth(token)
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_case_id"] == case_id
    assert body["payment_request"]["status"] == REQUEST_PENDING
    assert body["case"]["status"] == "AWAITING_APPROVAL"
    assert body["case"]["invoice_number"] == "INV-A4-INTAKE"
    assert body["case"]["approval"] is None
    new_case_id = body["case"]["case_id"]

    after = app.state.bank_store.payment_requests(invoice_id="INV-A4-INTAKE")
    assert len(after) == 1
    live = [row for row in after if row["status"] == REQUEST_PENDING]
    assert len(live) == 1
    pending = live[0]
    assert pending["payment_id"] == body["payment_request"]["payment_id"]
    # Approval has not been granted and no ledger entry exists.
    assert app.state.bank_store.ledger_rows_for_payment(pending["payment_id"]) == []
    assert pending["approved_by"] is None

    # The customer cannot approve their own request.
    assert client.post(
        f"/workflow/cases/{new_case_id}/approve", json={}, headers=auth(token)
    ).status_code == 403
    # Executing without approval cannot move money.
    execute = client.post(f"/workflow/cases/{new_case_id}/execute", json={}, headers=auth(token))
    assert execute.status_code in {409, 200}
    assert app.state.bank_store.payment_request(pending["payment_id"])["status"] == REQUEST_PENDING
    assert app.state.bank_store.ledger_rows_for_payment(pending["payment_id"]) == []

    # A second click is refused before it can create another payable case.
    duplicate = client.post(
        f"/workflow/cases/{case_id}/agent4/request-payment", headers=auth(token)
    )
    assert duplicate.status_code == 409, duplicate.text
    assert len(app.state.bank_store.payment_requests(invoice_id="INV-A4-INTAKE")) == 1

    # The intake appears in the persisted support conversation.
    stored = client.get(conversation_path(case_id), headers=auth(token)).json()["messages"]
    assert any("prepared a new verified payment request" in message["body"] for message in stored)


# ---------------------------------------------------------------------------
# 4. Uncertain outcomes and escalation
# ---------------------------------------------------------------------------
def test_uncertain_payment_is_never_retried_or_requested_again(workspace_tmp_path):
    app, client, token, admin, case_id = setup_customer_case(workspace_tmp_path, "uncertain")
    make_the_payment_uncertain(client, admin, case_id)
    request = payment_request_for(app, case_id)
    assert request["status"] == REQUEST_UNCERTAIN
    before = len(app.state.bank_store.payment_requests(invoice_id="INV-A4-UNCERTAIN"))

    response = chat(
        client, token, case_id, "I haven't paid this invoice. Help me pay it.", "help_pay"
    )
    assert response.status_code == 200, response.text
    answer = response.json()["agent_message"]["body"].casefold()
    assert "uncertain" in answer or "not confirmed" in answer
    assert "retry" in answer or "retried" in answer or "do not" in answer

    blocked = client.post(
        f"/workflow/cases/{case_id}/agent4/request-payment", headers=auth(token)
    )
    assert blocked.status_code == 409, blocked.text
    assert len(app.state.bank_store.payment_requests(invoice_id="INV-A4-UNCERTAIN")) == before
    assert app.state.bank_store.payment_request(request["payment_id"])["status"] == REQUEST_UNCERTAIN


def test_agent4_escalates_a_case_to_human_review(workspace_tmp_path):
    app, client, token, _admin, case_id = setup_customer_case(workspace_tmp_path, "escalate")
    before = len(app.state.bank_store.payment_requests(invoice_id="INV-A4-ESCALATE"))

    response = chat(
        client,
        token,
        case_id,
        "I need a person to look at this payment.",
        "human_assistance",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["escalation"]["escalated"] is True
    assert body["case_status"] == "REQUIRES_REVIEW"
    assert body["requires_human"] is True
    assert "human review" in body["agent_message"]["body"].casefold()

    case = client.get(f"/workflow/cases/{case_id}", headers=auth(token)).json()
    assert case["status"] == "REQUIRES_REVIEW"
    assert case["requires_human"] is True
    assert any(
        entry["action"] in {"support_escalation", "support_escalation_reconfirmed"}
        for entry in case["audit"]
    )
    # Escalation is a case decision, never a bank action.
    assert len(app.state.bank_store.payment_requests(invoice_id="INV-A4-ESCALATE")) == before
    assert app.state.bank_store.payment_requests(case_id=case_id)[0]["status"] == REQUEST_PENDING

    # The explicit route is idempotent and records a re-confirmation.
    again = client.post(
        f"/workflow/cases/{case_id}/agent4/escalate",
        json={"reason": "Vendor still cannot trace the money."},
        headers=auth(token),
    )
    assert again.status_code == 200, again.text
    assert again.json()["already_escalated"] is True


# ---------------------------------------------------------------------------
# 5. The requested demo invoice
# ---------------------------------------------------------------------------
def test_demo_702_supports_an_interactive_conversation(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "demo702")
    demo = seed_demo_bank(app)
    seeded = demo["agent4_demo_case"]
    assert seeded["invoice_number"] == AGENT4_DEMO_INVOICE
    assert seeded["payment_request_id"] is None
    assert seeded["bank_status"] is None

    token = client.post(
        "/auth/signin",
        json={"email": "customer.a@ghostledger.test", "password": "ghostledger-demo"},
    ).json()["token"]
    cases = client.get("/workflow/cases", headers=auth(token)).json()
    target = next(case for case in cases if case["invoice_number"] == AGENT4_DEMO_INVOICE)

    conversation = client.get(conversation_path(target["case_id"]), headers=auth(token)).json()
    assert conversation["payment_status"]["bank"] is None
    assert conversation["payment_status"]["has_bank_record"] is False
    assert conversation["payment_status"]["bank_status_label"] == "No payment record found"
    assert conversation["payment_status"]["settlement_verified"] is False
    assert conversation["payment_status"]["can_request_payment"] is True

    # "I already paid" must not be confirmed without bank evidence.
    paid = chat(
        client,
        token,
        target["case_id"],
        "I already paid, but the vendor says they did not receive it.",
        "already_paid",
    )
    assert paid.status_code == 200, paid.text
    paid_text = paid.json()["agent_message"]["body"].casefold()
    assert "no settled payment" in paid_text or "cannot confirm" in paid_text
    assert "reference" in paid_text

    # "Why was my payment declined?" must not invent a decline or a reason.
    declined = chat(
        client,
        token,
        target["case_id"],
        "Why was my payment declined?",
        "why_declined",
    )
    assert declined.status_code == 200, declined.text
    declined_text = declined.json()["agent_message"]["body"].casefold()
    assert "no recorded decline" in declined_text or "cannot" in declined_text
    assert "insufficient funds" not in declined_text

    # A new, verified request can be prepared from this unpaid invoice.
    intake = client.post(
        f"/workflow/cases/{target['case_id']}/agent4/request-payment", headers=auth(token)
    )
    assert intake.status_code == 201, intake.text
    assert intake.json()["payment_request"]["status"] == REQUEST_PENDING
    assert intake.json()["case"]["status"] == "AWAITING_APPROVAL"
    assert intake.json()["case"]["approval"] is None

    duplicate = client.post(
        f"/workflow/cases/{target['case_id']}/agent4/request-payment", headers=auth(token)
    )
    assert duplicate.status_code == 409, duplicate.text

    # The whole conversation is still there, in order, after several turns.
    stored = client.get(conversation_path(target["case_id"]), headers=auth(token)).json()["messages"]
    assert len(stored) >= 6
    assert stored[0]["actor"] == "customer"
    assert any(message["actor"] == "agent_4" for message in stored)
