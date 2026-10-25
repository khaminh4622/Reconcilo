"""Shared helpers for tests that drive the real API over HTTP.

Only the remote LLM boundary is replaced (the suite's usual pattern): the app
runs Agent 3's real PDF reading, grounding and validation, the orchestrator and
every agent, against an isolated SQLite database.
"""

from __future__ import annotations

import json

import pymupdf
from fastapi.testclient import TestClient

from agent3.database import DocumentRepository
from agent3.extractor import LLMExtractor
from agent3.service import Agent3Service
from main import create_app
from workflow.accounts import ROLE_BANK_ADMIN

VENDOR = "Northstar Office Supplies LLC"
CUSTOMER = "Blue Harbor Technologies Inc."
PASSWORD = "password123"


class ScriptedModel:
    """Stands in for the provider: replies with whatever the test queued."""

    def __init__(self):
        self.reply: dict = {}
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        return json.dumps(self.reply)


def make_app(workspace_tmp_path, name: str):
    model = ScriptedModel()
    database = workspace_tmp_path / f"{name}.db"
    app = create_app(service=Agent3Service(DocumentRepository(database), LLMExtractor(model)))
    app.state.model = model
    return app, TestClient(app)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def signup(client, email: str, name: str | None = None) -> tuple[str, str]:
    """Sign up a customer; returns (token, account_id)."""

    body = client.post(
        "/auth/signup", json={"email": email, "password": PASSWORD, "display_name": name or email}
    )
    assert body.status_code == 200, body.text
    data = body.json()
    return data["token"], data["account"]["account_id"]


def make_admin(app, client, email: str, role: str = ROLE_BANK_ADMIN, name: str = "Bank Admin") -> tuple[str, str]:
    """Bank-side accounts never come from public signup; create one directly."""

    account = app.state.accounts.sign_up(email=email, password=PASSWORD, display_name=name, role=role)
    token = client.post("/auth/signin", json={"email": email, "password": PASSWORD}).json()["token"]
    return token, account.account_id


def make_pdf(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(pymupdf.Rect(50, 50, 560, 740), text, fontsize=10, lineheight=1.3)
    content = document.tobytes()
    document.close()
    return content


def invoice_text(number: str, amount: str, note: str = "") -> str:
    return (
        f"INVOICE\nVendor: {VENDOR}\nCustomer: {CUSTOMER}\nInvoice Number: {number}\n"
        f"Invoice Date: 2026-03-01\nCurrency: USD\nSubtotal: {amount}\nTax: 0.00\n"
        f"Invoice Total: {amount}\n{note}"
    )


def reply(
    number: str | None,
    amount: str | None,
    *,
    primary: str = "payment_request",
    problem: str = "none_identified",
    paid: str | None = None,
    count: int | None = None,
    evidence: str | None = None,
    document_type: str = "invoice",
) -> dict:
    """A model reply describing what the document says."""

    financial = {"currency": "USD"}
    if amount is not None:
        financial.update({"subtotal": amount, "tax_amount": "0.00", "total_amount": amount})
    if paid is not None:
        financial["amount_paid"] = paid
    detected = {"type": problem, "primary_problem": primary, "status": "reported"}
    if problem != "none_identified":
        detected["description"] = "Reported by the customer."
    if evidence:
        detected["source_evidence"] = evidence
    if count is not None:
        detected["reported_payment_count"] = count
    return {
        "document_type": document_type,
        "document_id": number,
        "issue_date": "2026-03-01",
        "currency": "USD",
        "customer": {"customer_name": CUSTOMER},
        "vendor": {"vendor_name": VENDOR},
        "financial_information": financial,
        "detected_problem": detected,
        "missing_information": [],
        "contradictions": [],
        "information_requiring_verification": [],
    }


def upload(app, client, token: str, text: str, model_reply: dict):
    app.state.model.reply = model_reply
    return client.post(
        "/workflow/cases",
        files={"file": ("document.pdf", make_pdf(text), "application/pdf")},
        headers=auth(token),
    )


def message(app, client, token: str, text: str, model_reply: dict):
    app.state.model.reply = model_reply
    return client.post("/workflow/cases/message", json={"message": text}, headers=auth(token))


def runs(case: dict) -> list[tuple[str, str]]:
    """(agent, status) for every step, in workflow order."""

    return [(r["agent"], r["status"]) for r in sorted(case["agent_runs"], key=lambda r: r["order"])]
