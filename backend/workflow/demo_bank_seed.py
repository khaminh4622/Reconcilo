"""Simulated bank demo data: accounts, funding and one end-to-end test case.

Seeded owners:

    Orion Creative Labs LLC        customer, funded so its invoice can settle
    Blue Harbor Cloud Services LLC vendor, receives the payment
    Customer A / Customer B        the scripted demo logins
    Bank admin / System admin      the bank-side roles

The test cases are:

    Invoice  INV-GL-DEMO-26024-701
    Customer Orion Creative Labs LLC
    Vendor   Blue Harbor Cloud Services LLC
    Amount   1,620.00 USD

It is created through the real workflow (case -> Agent 1 -> awaiting approval).
Nothing is paid and no authorisation is granted by the seed: a bank admin still
has to approve and process it, which is what the integration test drives.

    Invoice  INV-GL-DEMO-26024-702
    Customer Customer A
    Vendor   Blue Harbor Cloud Services LLC
    Amount   2,500.00 USD

The second case is Agent 4's support demonstration. The customer reports that a
payment failed, but the Mock Bank has no matching request. Agent 4 must say the
claim is unverified, ask for the missing reference, and offer either human review
or a new verified request. No payment is created and no money moves.

A newly registered customer gets nothing here. Their records stay isolated with
no bank account and no transactions until an explicit onboarding step
(``onboard_customer``) exists for them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .accounts import ROLE_BANK_ADMIN, ROLE_CUSTOMER, ROLE_DEMO_ADMIN, AuthError
from .mock_bank_accounts import MockBankStore
from .schemas import WorkflowCase

DEMO_INVOICE = "INV-GL-DEMO-26024-701"
DEMO_CUSTOMER = "Orion Creative Labs LLC"
DEMO_VENDOR = "Blue Harbor Cloud Services LLC"
DEMO_AMOUNT = Decimal("1620.00")
DEMO_CURRENCY = "USD"

# Agent 4's support-chat demo. The customer reports a failed payment, but the
# Mock Bank has no request for it. Agent 4 must say the claim is unverified and
# ask for details rather than inventing a bank record or a reason. The case can
# then be escalated to human review or used to prepare a new verified request.
AGENT4_DEMO_INVOICE = "INV-GL-DEMO-26024-702"
AGENT4_DEMO_CUSTOMER = "Customer A"
AGENT4_DEMO_AMOUNT = Decimal("2500.00")

DEFAULT_PASSWORD = "ghostledger-demo"

DEMO_ACCOUNTS: tuple[tuple[str, str, str], ...] = (
    ("orion@ghostledger.test", "Orion Creative Labs LLC", ROLE_CUSTOMER),
    ("blueharbor@ghostledger.test", "Blue Harbor Cloud Services LLC", ROLE_CUSTOMER),
    ("customer.a@ghostledger.test", "Customer A", ROLE_CUSTOMER),
    ("customer.b@ghostledger.test", "Customer B", ROLE_CUSTOMER),
    ("bank.admin@ghostledger.test", "Bank Administrator", ROLE_BANK_ADMIN),
    ("system.admin@ghostledger.test", "System Administrator", ROLE_DEMO_ADMIN),
)

# Starting simulated balances. Orion has comfortably more than 1,620 so the
# demo settles; Customer B is funded separately for a failure demonstration.
OPENING_BALANCES: dict[str, str] = {
    "orion@ghostledger.test": "25000.00",
    "blueharbor@ghostledger.test": "0.00",
    "customer.a@ghostledger.test": "5000.00",
    "customer.b@ghostledger.test": "5000.00",
}


def _store_credentials(app) -> str:
    import os

    return os.getenv("DEMO_ACCOUNT_PASSWORD") or DEFAULT_PASSWORD


def account_for_email(app, email: str):
    """The existing account with this email, or None."""

    try:
        for account in app.state.accounts.list_accounts():
            if account.email == email:
                return account
    except Exception:  # noqa: BLE001
        return None
    return None


def _ensure_account(app, email: str, display_name: str, role: str, password: str):
    existing = account_for_email(app, email)
    if existing is not None:
        return existing, False
    try:
        account = app.state.accounts.sign_up(
            email=email, password=password, display_name=display_name, role=role
        )
    except AuthError:
        return account_for_email(app, email), False
    return account, True


def onboard_customer(
    app,
    *,
    account_id: str,
    display_name: str,
    currency: str = "USD",
    opening_balance: Decimal | str = "0.00",
) -> dict:
    """Explicit onboarding: give a customer account a simulated bank account.

    A new signup is *not* onboarded automatically. Funding a balance is a
    separate, deliberate act (a demo or bank-admin action), so an unonboarded
    customer has no account, no balance and no transactions.
    """

    store: MockBankStore = app.state.bank_store
    account, created = store.ensure_account(
        owner_id=account_id,
        owner_type="CUSTOMER",
        display_name=display_name,
        currency=currency,
        opening_balance=opening_balance,
        actor_role="SYSTEM",
    )
    if created:
        account = store.account(account["account_id"])
    return account


def ensure_vendor_account(app, vendor_name: str, currency: str = "USD") -> dict:
    store: MockBankStore = app.state.bank_store
    account, _created = store.ensure_account(
        owner_id=f"VENDOR:{vendor_name.casefold()}",
        owner_type="VENDOR",
        display_name=vendor_name,
        currency=currency,
        opening_balance="0.00",
        actor_role="SYSTEM",
    )
    return account


def _demo_analysis_dict() -> dict[str, Any]:
    """A deterministic Agent 3 reading of the demo invoice.

    Labelled ``synthetic_fixture`` so it is never presented as a live model
    result. Everything after this point is the production pipeline.
    """

    return {
        "document_type": "invoice",
        "document_id": DEMO_INVOICE,
        "issue_date": "2026-09-24",
        "due_date": "2026-10-24",
        "currency": DEMO_CURRENCY,
        "customer": {"customer_name": DEMO_CUSTOMER, "company_name": DEMO_CUSTOMER},
        "vendor": {"vendor_name": DEMO_VENDOR},
        "financial_information": {
            "currency": DEMO_CURRENCY,
            "subtotal": "1620.00",
            "tax_amount": "0.00",
            "total_amount": "1620.00",
            "amount_paid": "0.00",
            "outstanding_balance": "1620.00",
            "payment_status": "Unpaid",
        },
        "detected_problem": {"type": "none_identified", "primary_problem": "payment_request"},
        "missing_information": [],
        "contradictions": [],
        "information_requiring_verification": [],
        "validation": {
            "status": "passed",
            "financial_consistency": "passed",
            "customer_verification": "matched",
            "missing_fields": [],
            "validation_findings": [],
            "calculated": {},
        },
        "routing": {
            "recommended_agent": "human_approval",
            "reason": "A valid, internally consistent invoice with no problem reported.",
            "priority": "normal",
            "blocking": False,
        },
        "analysis_source": "synthetic_fixture",
        "llm_call": {"invoked": False},
    }


def open_demo_case(app, owner_account_id: str) -> WorkflowCase:
    """Open the demo invoice through the real orchestrator.

    Idempotent: if the invoice already has a case, that case is returned.
    """

    orchestrator = app.state.orchestrator
    existing = [
        case
        for case in orchestrator.repository.list_cases(limit=500)
        if (case.invoice_number or "").casefold() == DEMO_INVOICE.casefold()
    ]
    if existing:
        full = orchestrator.get(existing[0].case_id)
        if full is not None:
            return full

    from agent3.schemas import AnalysisResult

    analysis = AnalysisResult.model_validate(_demo_analysis_dict())
    case = orchestrator.open_case(
        analysis, filename=f"{DEMO_INVOICE}.pdf", channel="demo"
    )
    case.owner_account_id = owner_account_id
    orchestrator.repository.save_case(case)
    routed = orchestrator.route(case)
    return routed


def _agent4_demo_analysis_dict() -> dict[str, Any]:
    """A deterministic Agent 3 reading of the Agent 4 support demo complaint.

    It is deliberately *not* an invoice. The customer says a payment failed, but
    no bank record exists; Agent 4 must treat the claim as unverified. Labelled
    ``synthetic_fixture`` so it is never presented as a live model result.
    """

    return {
        "document_type": "other",
        "document_id": AGENT4_DEMO_INVOICE,
        "issue_date": None,
        "due_date": None,
        "currency": DEMO_CURRENCY,
        "customer": {
            "customer_name": AGENT4_DEMO_CUSTOMER,
            "company_name": AGENT4_DEMO_CUSTOMER,
        },
        "vendor": {"vendor_name": DEMO_VENDOR},
        "financial_information": {
            "currency": DEMO_CURRENCY,
            "total_amount": "2500.00",
            "amount_paid": "0.00",
            "outstanding_balance": "2500.00",
            "payment_status": "Failed",
        },
        "transaction_information": {
            "transaction_id": "GL-FAIL-702",
            "transaction_date": "2026-10-24",
            "transaction_amount": "2500.00",
            "sender": AGENT4_DEMO_CUSTOMER,
            "receiver": DEMO_VENDOR,
            "reference_number": AGENT4_DEMO_INVOICE,
        },
        "detected_problem": {
            "type": "other_financial_issue",
            "primary_problem": "payment_failure",
            "description": (
                "The customer reports that a 2,500.00 USD payment for invoice "
                "INV-GL-DEMO-26024-702 failed for an unknown cause."
            ),
            "source_evidence": (
                "I attempted to pay invoice INV-GL-DEMO-26024-702 for 2,500.00 USD, "
                "but the payment did not complete."
            ),
            "status": "reported",
            "reported_payment_count": 1,
        },
        "missing_information": ["payment reference", "bank failure reason"],
        "contradictions": [],
        "information_requiring_verification": [
            "payment failure reason",
            "payment settlement status",
        ],
        "validation": {
            "status": "warning",
            "financial_consistency": "passed",
            "customer_verification": "unverified",
            "missing_fields": [],
            "validation_findings": [],
            "calculated": {},
        },
        "routing": {
            "recommended_agent": "agent_4",
            "reason": "The customer reports a payment failure that needs investigation.",
            "priority": "normal",
            "blocking": False,
        },
        "analysis_source": "synthetic_fixture",
        "llm_call": {"invoked": False},
    }


def open_agent4_demo_case(app, owner_account_id: str) -> WorkflowCase:
    """Seed INV-GL-DEMO-26024-702 as an unverified failed-payment complaint.

    This intentionally leaves the Mock Bank with no payment request for the
    invoice. Agent 4's job is to say the claim is unverified, ask for the missing
    reference, offer human review, and (if the customer still needs to pay)
    prepare a *new* verified request through the ordinary approval workflow.
    Idempotent: if the case already exists it is returned unchanged.
    """

    orchestrator = app.state.orchestrator
    existing = [
        case
        for case in orchestrator.repository.list_cases(limit=500)
        if (case.invoice_number or "").casefold() == AGENT4_DEMO_INVOICE.casefold()
    ]
    if existing:
        return orchestrator.get(existing[0].case_id) or existing[0]

    from agent3.schemas import AnalysisResult

    analysis = AnalysisResult.model_validate(_agent4_demo_analysis_dict())
    case = orchestrator.open_case(
        analysis, filename=f"{AGENT4_DEMO_INVOICE}.pdf", channel="demo"
    )
    case.owner_account_id = owner_account_id
    orchestrator.repository.save_case(case)
    return orchestrator.route(case)


def seed_demo_bank(app) -> dict[str, Any]:
    """Create the demo accounts, simulated balances and the demo case."""

    import os

    report: dict[str, Any] = {
        "accounts_created": [],
        "accounts_existing": [],
        "bank_accounts": {},
        "demo_case": None,
        "agent4_demo_case": None,
        "skipped_reason": None,
    }
    if (os.getenv("GHOSTLEDGER_ENV") or "").strip().lower() == "production":
        report["skipped_reason"] = "GHOSTLEDGER_ENV=production"
        return report

    password = _store_credentials(app)
    accounts: dict[str, Any] = {}
    for email, display_name, role in DEMO_ACCOUNTS:
        account, created = _ensure_account(app, email, display_name, role, password)
        if account is None:
            continue
        accounts[email] = account
        (report["accounts_created"] if created else report["accounts_existing"]).append(
            {"email": email, "role": role}
        )

    # Simulated bank accounts. Opening balances are only applied on creation, so
    # restarting the server never re-funds an account.
    for email, opening in OPENING_BALANCES.items():
        account = accounts.get(email)
        if account is None:
            continue
        bank_account = onboard_customer(
            app,
            account_id=account.account_id,
            display_name=account.display_name,
            opening_balance=opening,
        )
        report["bank_accounts"][email] = {
            "account_id": bank_account["account_id"],
            "balance": bank_account["balance"],
            "status": bank_account["status"],
        }
    vendor = ensure_vendor_account(app, DEMO_VENDOR)
    report["bank_accounts"]["vendor:blueharbor"] = {
        "account_id": vendor["account_id"],
        "balance": vendor["balance"],
    }

    # Bank-side accounts hold no simulated money of their own: they review and
    # process, they never pay for anyone.

    orion = accounts.get("orion@ghostledger.test")
    if orion is not None:
        case = open_demo_case(app, orion.account_id)
        report["demo_case"] = {
            "case_id": case.case_id,
            "invoice_number": case.invoice_number,
            "amount": str(case.amount) if case.amount is not None else None,
            "currency": case.currency,
            "status": case.status.value,
            "payment_request_id": case.payment_request_id,
            "customer": case.customer_name,
            "vendor": case.vendor_name,
        }

    # Agent 4's support-chat demo case: a verified invoice whose simulated bank
    # payment was rejected, leaving a real, readable bank reason and no money
    # movement. Customer A owns it so the two demo customers stay separate.
    customer_a = accounts.get("customer.a@ghostledger.test")
    if customer_a is not None:
        case = open_agent4_demo_case(app, customer_a.account_id)
        report["agent4_demo_case"] = {
            "case_id": case.case_id,
            "invoice_number": case.invoice_number,
            "amount": str(case.amount) if case.amount is not None else None,
            "currency": case.currency,
            "status": case.status.value,
            "payment_request_id": case.payment_request_id,
            "bank_status": case.bank_status,
            "customer": case.customer_name,
            "vendor": case.vendor_name,
        }
    return report
