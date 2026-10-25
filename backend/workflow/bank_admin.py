"""Bank-side processing: the human authorisation and the bank's own decision.

Two separate powers, deliberately kept apart:

    Human payment authorisation  (``/workflow/cases/{id}/approve``)
        a BANK_ADMIN or DEMO_ADMIN, never the requester, approves the exact
        invoice, payee, amount and currency. This records an authorisation. It
        does not move money.

    Bank-side transaction processing  (``/bank-admin/requests/{id}/process``)
        the bank applies its deterministic rules (account status, funds,
        currency, reference) and settles or refuses the instruction.

A request can only be processed once it is APPROVED, so processing is never a
back door around authorisation. Customers can do neither, and every endpoint
here is admin-only at the backend - hiding a button is never the control.

Nothing in this module moves real money: every balance is simulated.
"""

from __future__ import annotations

from typing import Any

from .mock_bank_accounts import (
    ACCOUNT_ACTIVE,
    LIVE_REQUEST_STATUSES,
    REQUEST_APPROVED,
    REQUEST_PENDING,
    REQUEST_PROCESSING,
    REQUEST_SETTLED,
    REQUEST_UNCERTAIN,
    BankError,
    MockBankStore,
    from_minor,
)
from .schemas import AgentId, CaseStatus

DISCLAIMER = "SIMULATED BANK - NO REAL MONEY. Every account, balance and transfer on this page is fictional."


def _owners(app) -> dict[str, dict[str, str]]:
    try:
        return {
            account.account_id: {
                "display_name": account.display_name,
                "email": account.email,
                "role": account.role,
            }
            for account in app.state.accounts.list_accounts()
        }
    except Exception:  # noqa: BLE001 - the accounts table may be absent in tests
        return {}


def _agent_checks(app, case_id: str | None) -> dict[str, dict[str, Any]]:
    """Return the pre-approval evidence a bank reviewer needs.

    This information deliberately stays on the bank-admin response: it is
    derived from the workflow case and is never exposed by public bank routes.
    A payment request cannot be created until both checks have cleared, but
    displaying the evidence lets the human see *why* it is safe to decide.
    """

    unavailable = {"status": "NOT_AVAILABLE", "passed": False, "summary": "No workflow case linked."}
    if not case_id:
        return {"agent_3": unavailable, "agent_1": unavailable}
    try:
        case = app.state.orchestrator.get(case_id)
    except Exception:  # noqa: BLE001 - a bank row must still be viewable if historical case data is unavailable
        case = None
    if case is None:
        return {"agent_3": unavailable, "agent_1": unavailable}

    def run_status(agent: AgentId) -> str:
        run = case.run_for(agent)
        value = getattr(run, "status", None)
        return getattr(value, "value", str(value or "NOT_RUN")).upper()

    agent3_run = case.run_for(AgentId.agent_3)
    agent3_valid = bool(case.analysis and agent3_run and run_status(AgentId.agent_3) == "COMPLETED")
    agent3 = {
        "status": run_status(AgentId.agent_3),
        "passed": agent3_valid,
        "summary": getattr(agent3_run, "summary", None) or "Agent 3 document verification has not completed.",
        "validation_status": getattr(getattr(case, "routing", None), "validation_status", None),
    }

    report = next((r for r in reversed(case.reports) if r.agent == AgentId.agent_1), None)
    investigation = (getattr(report, "data", {}) or {}).get("investigation") or {}
    previous = investigation.get("previous_payment_status")
    agent1 = {
        "status": run_status(AgentId.agent_1),
        # For a new payable, this is Agent 1's explicit clearance.  The
        # fallback supports older already-pending cases whose report predates
        # the structured previous-payment field.
        "passed": bool(report and not report.blocking and (previous in {None, "UNPAID_ELIGIBLE_FOR_APPROVAL"})),
        "summary": getattr(report, "summary", None) or "Agent 1 duplicate and payment-history check has not completed.",
        "investigation_status": investigation.get("status") or (getattr(report, "data", {}) or {}).get("status"),
        "previous_payment_status": previous,
    }
    return {"agent_3": agent3, "agent_1": agent1}


def _enrich(app, request: dict) -> dict:
    """Attach the account and owner names a bank reviewer needs."""

    store = app.state.bank_store
    owners = _owners(app)
    source = store.account(request.get("source_account_id")) or {}
    destination = store.account(request.get("destination_account_id")) or {}
    source_owner = owners.get(source.get("owner_id") or "")
    destination_owner = owners.get(destination.get("owner_id") or "")
    return {
        **request,
        "source": {
            "account_id": source.get("account_id"),
            "account_number_masked": source.get("account_number_masked"),
            "balance": source.get("balance"),
            "currency": source.get("currency"),
            "status": source.get("status"),
            "owner_name": (source_owner or {}).get("display_name") or source.get("display_name"),
        }
        if source
        else None,
        "destination": {
            "account_id": destination.get("account_id"),
            "account_number_masked": destination.get("account_number_masked"),
            "balance": destination.get("balance"),
            "currency": destination.get("currency"),
            "status": destination.get("status"),
            "owner_name": (destination_owner or {}).get("display_name") or destination.get("display_name"),
        }
        if destination
        else None,
        "requesting_account": owners.get(request.get("customer_id") or ""),
        "agent_checks": _agent_checks(app, request.get("case_id")),
    }


def request_queue(app, *, status: str | None = None) -> dict[str, Any]:
    store = app.state.bank_store
    statuses = {status.upper()} if status else None
    rows = [_enrich(app, row) for row in store.payment_requests(statuses=statuses, limit=500)]
    if not statuses:
        # Actionable work first.
        order = {
            REQUEST_PENDING: 0,
            REQUEST_APPROVED: 1,
            REQUEST_PROCESSING: 1,
            REQUEST_UNCERTAIN: 2,
        }
        rows.sort(key=lambda r: (order.get(r["status"], 3), r["created_at"]))
    return {
        "simulated": True,
        "disclaimer": DISCLAIMER,
        "count": len(rows),
        "summary": store.summary(),
        "requests": rows,
        "live_statuses": sorted(LIVE_REQUEST_STATUSES),
    }


def repair_cross_customer_payment_links(app) -> dict[str, int]:
    """Detach historical case links that point at another customer's payment.

    Earlier versions looked up an open bank request by invoice number alone.
    If two customers uploaded an invoice with the same number, a later case
    could inherit the first customer's request id.  That is both misleading in
    the customer dashboard and unsafe tenancy.  Keep every case, approval,
    request and ledger row; only remove the invalid cross-customer link and
    place the affected upload in review.

    This is idempotent and runs at application startup after the bank and
    workflow repositories have been opened.
    """

    repaired = 0
    for case in app.state.workflow_repository.list_full_cases():
        owner_id = getattr(case, "owner_account_id", None)
        if not owner_id or not case.invoice_number:
            continue
        linked = app.state.bank_store.payment_request(getattr(case, "payment_request_id", None))
        foreign_link = bool(linked and linked.get("customer_id") != owner_id)

        # Historical duplicate cases did not always retain the erroneous id.
        # Identify that form only when the invoice, vendor and currency match
        # a live request belonging to somebody else.
        foreign_live = None
        if linked is None and case.status == CaseStatus.awaiting_approval:
            candidate = app.state.bank_store.live_request_for_invoice(case.invoice_number)
            if candidate and candidate.get("customer_id") != owner_id:
                same_vendor = (candidate.get("payee") or "").strip().casefold() == (
                    case.vendor_name or ""
                ).strip().casefold()
                same_currency = (candidate.get("currency") or "").upper() == (
                    case.currency or ""
                ).upper()
                if same_vendor and same_currency:
                    foreign_live = candidate

        if not foreign_link and foreign_live is None:
            continue

        case.add_audit(
            AgentId.orchestrator,
            "cross_customer_payment_link_detached",
            "Removed an invalid link to another customer's Mock Bank request; no funds moved.",
            CaseStatus.requires_review.value,
        )
        case.payment_request_id = None
        case.bank_status = None
        case.status = CaseStatus.requires_review
        case.requires_human = True
        case.pending_reason = (
            "This upload matches a payment request owned by another customer. "
            "The historical upload is preserved, but it cannot enter bank review from this account."
        )
        case.outcome = case.pending_reason
        app.state.workflow_repository.save_case(case)
        repaired += 1
    return {"repaired": repaired}


def create_request_for_case(app, case) -> dict | None:
    """Create the pending payment request for a case, once.

    Called by the orchestrator when a case first reaches approval. Returns None
    when the case has no simulated bank account to pay from. A customer who has
    not been explicitly onboarded to the simulated bank is funded *on demand*
    here, because GhostLedger's demo flows have always let an approved invoice
    settle; the funding is a separate, recorded bank action, never part of the
    payment itself.
    """

    store: MockBankStore = app.state.bank_store
    if not case.owner_account_id or not case.invoice_number:
        return None
    request_identity = {
        "customer_id": case.owner_account_id,
        "payee": case.vendor_name,
        "currency": case.currency or "USD",
    }
    existing = store.live_request_for_invoice(case.invoice_number, **request_identity)
    if existing is not None:
        return existing
    if store.settled_request_for_invoice(case.invoice_number, **request_identity) is not None:
        return None

    source = store.account_for_owner(case.owner_account_id, case.currency or "USD")
    if source is None:
        source = ensure_customer_funding(app, case)
        if source is None:
            return None
    destination = _vendor_account(app, case)
    if destination is None:
        return None
    try:
        request, _created = store.create_payment_request(
            customer_id=case.owner_account_id,
            invoice_id=case.invoice_number,
            source_account_id=source["account_id"],
            destination_account_id=destination,
            amount=case.amount,
            currency=case.currency or source["currency"],
            payee=case.vendor_name,
            case_id=case.case_id,
            # Stable per case: re-running the approval step cannot add a second
            # payable request for the same case.
            idempotency_key=f"case:{case.case_id}",
            status=REQUEST_PENDING,
            actor_id=case.owner_account_id,
            actor_role="CUSTOMER",
        )
    except (BankError, ValueError) as exc:
        case.add_audit(
            _audit_actor(),
            "bank_request_failed",
            f"The simulated bank could not open a payment request: {exc}",
        )
        return None
    case.payment_request_id = request["payment_id"]
    case.add_audit(
        _audit_actor(),
        "bank_request_pending",
        (
            f"A simulated payment request {request['payment_id']} was opened for "
            f"{request['amount']} {request['currency']}. It needs human authorisation."
        ),
    )
    return request


def _audit_actor():
    from .schemas import AgentId

    return AgentId.orchestrator


# Default simulated funding for a customer who has never been onboarded. Large
# enough for the demo invoices to settle; the amount lands in an account balance
# and in the bank audit log, so it is always visible as simulated.
DEFAULT_DEMO_FUNDING = "50000.00"


def ensure_customer_funding(app, case) -> dict | None:
    """Give the case owner a simulated account, funded if the demo setting allows.

    ``GHOSTLEDGER_AUTO_FUND_CUSTOMERS=0`` turns this off, and then an unonboarded
    customer must be funded explicitly (``onboard_customer``).
    """

    import os

    store: MockBankStore = app.state.bank_store
    email = None
    try:
        email = app.state.accounts.get(case.owner_account_id).email
    except Exception:  # noqa: BLE001 - the account may be gone
        email = None
    auto = os.getenv("GHOSTLEDGER_AUTO_FUND_CUSTOMERS", "1") != "0"
    opening = DEFAULT_DEMO_FUNDING if auto else "0.00"
    account, _created = store.ensure_account(
        owner_id=case.owner_account_id,
        owner_type="CUSTOMER",
        display_name=(email or case.customer_name or case.owner_account_id),
        currency=case.currency or "USD",
        opening_balance=opening,
        actor_role="SYSTEM",
    )
    if auto:
        case.add_audit(
            _audit_actor(),
            "bank_onboarded",
            (
                f"A simulated bank account {account['account_id']} was opened for this customer "
                f"with {opening} {case.currency or 'USD'} of fictional funds."
            ),
        )
    return account


def _vendor_account(app, case) -> str | None:
    """The simulated recipient account for the case's vendor."""

    store: MockBankStore = app.state.bank_store
    vendor = (case.vendor_name or "").strip()
    if not vendor:
        return None
    if not hasattr(app.state, "vendor_accounts"):
        app.state.vendor_accounts = {}
    cached = app.state.vendor_accounts.get(vendor.casefold())
    if cached:
        account = store.account(cached)
        if account is not None:
            return account["account_id"]
    owner_id = f"VENDOR:{vendor.casefold()}"
    account, _created = store.ensure_account(
        owner_id=owner_id,
        owner_type="VENDOR",
        display_name=vendor,
        currency=case.currency or "USD",
        actor_id=None,
        actor_role="SYSTEM",
    )
    app.state.vendor_accounts[vendor.casefold()] = account["account_id"]
    return account["account_id"]


def authorise_request(app, case, *, approver_id: str, approver_role: str, note: str | None) -> dict | None:
    """Record the human authorisation against the bank's own request row."""

    store: MockBankStore = app.state.bank_store
    payment_id = getattr(case, "payment_request_id", None)
    if not payment_id:
        existing = store.live_request_for_invoice(
            case.invoice_number,
            customer_id=case.owner_account_id,
            payee=case.vendor_name,
            currency=case.currency or "USD",
        )
        payment_id = existing["payment_id"] if existing else None
    if not payment_id:
        return None
    try:
        return store.record_approval(
            payment_id=payment_id,
            approver_id=approver_id,
            approver_role=approver_role,
            decision="APPROVED",
            reason=note,
        )
    except BankError:
        return store.payment_request(payment_id)


def reject_request(app, case, *, approver_id: str, approver_role: str, reason: str | None) -> dict | None:
    store: MockBankStore = app.state.bank_store
    payment_id = getattr(case, "payment_request_id", None)
    if not payment_id:
        existing = store.live_request_for_invoice(
            case.invoice_number,
            customer_id=case.owner_account_id,
            payee=case.vendor_name,
            currency=case.currency or "USD",
        )
        payment_id = existing["payment_id"] if existing else None
    if not payment_id:
        return None
    try:
        return store.record_approval(
            payment_id=payment_id,
            approver_id=approver_id,
            approver_role=approver_role,
            decision="REJECTED",
            reason=reason,
        )
    except BankError:
        return store.payment_request(payment_id)


def process_request(app, payment_id: str, *, account) -> dict:
    """Apply the bank's deterministic rules to an approved request.

    Returns a result dict; ``status`` is SETTLED, FAILED or UNCERTAIN. This is
    the only place in the system where a simulated balance moves.
    """

    store: MockBankStore = app.state.bank_store
    request = store.payment_request(payment_id)
    if request is None:
        raise BankError("That payment request does not exist.")
    if request["status"] == REQUEST_SETTLED:
        # Idempotent: processing a settled request reports it, moves nothing.
        return {
            "status": REQUEST_SETTLED,
            "already_settled": True,
            "settlement_reference": request.get("settlement_reference"),
            "request": _enrich(app, request),
        }
    refusal = _preflight(app, request)
    if refusal is not None:
        failed = store.fail_payment(
            payment_id,
            code=refusal["code"],
            reason=refusal["reason"],
            actor_id=account.account_id,
            actor_role=account.role,
        )
        return {
            "status": failed["status"] if failed else "FAILED",
            "already_settled": False,
            "failure_code": refusal["code"],
            "failure_reason": refusal["reason"],
            "request": _enrich(app, failed or request),
        }
    try:
        result = store.process_payment(
            payment_id,
            actor_id=account.account_id,
            actor_role=account.role,
            # The bank issues its own MB- settlement reference; the invoice
            # number is carried in its own column, never as the reference.
            payment_reference=None,
        )
    except BankError as exc:
        failed = store.fail_payment(
            payment_id,
            code=exc.code,
            reason=str(exc),
            actor_id=account.account_id,
            actor_role=account.role,
        )
        return {
            "status": failed["status"] if failed else "FAILED",
            "already_settled": False,
            "failure_code": exc.code,
            "failure_reason": str(exc),
            "request": _enrich(app, failed or request),
        }
    return {
        **result,
        "request": _enrich(app, result.get("payment") or store.payment_request(payment_id) or request),
    }


def _preflight(app, request: dict) -> dict | None:
    """Bank rules that can be decided without moving anything."""

    store: MockBankStore = app.state.bank_store
    if request["status"] != REQUEST_APPROVED:
        return {
            "code": ("APPROVAL_MISSING" if request["status"] != REQUEST_UNCERTAIN else "BANK_TIMEOUT"),
            "reason": (
                f"The request is {request['status']}; only an APPROVED request can be processed. "
                "An uncertain payment must be investigated before anything else happens."
            ),
        }
    source = store.account(request.get("source_account_id"))
    destination = store.account(request.get("destination_account_id"))
    if source is None or destination is None:
        return {"code": "UNKNOWN_ACCOUNT", "reason": "The payer or recipient account does not exist."}
    if source["status"] != ACCOUNT_ACTIVE:
        return {
            "code": "ACCOUNT_BLOCKED",
            "reason": f"The payer account is {source['status']}, so it cannot send funds.",
        }
    if destination["status"] != ACCOUNT_ACTIVE:
        return {
            "code": "ACCOUNT_BLOCKED",
            "reason": f"The recipient account is {destination['status']}, so it cannot receive funds.",
        }
    return None


def bank_overview(app) -> dict[str, Any]:
    store: MockBankStore = app.state.bank_store
    accounts = [_with_owner(app, row) for row in store.accounts()]
    summary = store.summary()
    minor_totals: dict[str, int] = {}
    for row in store.accounts():
        currency = row["currency"]
        minor_totals[currency] = minor_totals.get(currency, 0) + int(row.get("balance_minor") or 0)
    totals = {currency: from_minor(minor) for currency, minor in minor_totals.items()}
    return {
        "simulated": True,
        "disclaimer": DISCLAIMER,
        "summary": summary,
        "total_balances": totals,
        "accounts": accounts,
    }


def _with_owner(app, row: dict) -> dict:
    owners = _owners(app)
    owner = owners.get(row.get("owner_id") or "")
    return {
        **row,
        "owner_name": (owner or {}).get("display_name") or row.get("display_name"),
        "owner_email": (owner or {}).get("email"),
        "owner_role": (owner or {}).get("role"),
    }


def bank_transactions(app, *, limit: int = 200, account_id: str | None = None) -> dict[str, Any]:
    store: MockBankStore = app.state.bank_store
    requests = store.payment_requests(limit=limit)
    if account_id:
        requests = [
            r for r in requests
            if account_id in {r.get("source_account_id"), r.get("destination_account_id")}
        ]
    transactions = []
    for row in requests:
        enriched = _enrich(app, row)
        entries = store.ledger_rows_for_payment(row["payment_id"])
        # A payment owns two permanent ledger rows after settlement.  The debit
        # entry is a stable, bank-generated transaction identifier; before a
        # decision, the payment id is the traceable request identifier.
        debit = next((entry for entry in entries if entry.get("direction") == "debit"), None)
        enriched["transaction_id"] = (
            (debit or {}).get("entry_id")
            or row.get("settlement_reference")
            or row["payment_id"]
        )
        enriched["ledger_entry_ids"] = [entry.get("entry_id") for entry in entries if entry.get("entry_id")]
        enriched["customer_name"] = (enriched.get("source") or {}).get("owner_name")
        enriched["vendor_name"] = (enriched.get("destination") or {}).get("owner_name")
        transactions.append(enriched)
    return {
        "simulated": True,
        "disclaimer": DISCLAIMER,
        "count": len(transactions),
        "transactions": transactions,
    }


def audit_log(app, limit: int = 200) -> dict[str, Any]:
    store: MockBankStore = app.state.bank_store
    logs = store.audit_logs(limit=limit)
    return {
        "simulated": True,
        "disclaimer": DISCLAIMER,
        "count": len(logs),
        "logs": logs,
    }
