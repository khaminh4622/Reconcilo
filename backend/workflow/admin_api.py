"""Bank-side and demo views across every account.

BANK_ADMIN and DEMO_ADMIN see payment failures, agent activity, customers and an
overview. DEMO_ADMIN can also run the fixture scenarios through the real
pipeline. Customers get 403 on all of these. Nothing here ever returns password
hashes, salts or session tokens: accounts are read through ``Account.public``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import HTTPException, Request

from .accounts import ROLE_CUSTOMER, AuthError
from .auth_api import require_admin, require_demo_admin
from .mock_bank import MockBank
from .payment_store import PaymentRecordStore
from .schemas import AgentId, AgentRunStatus, CaseStatus

# A step counts as having run once it reached one of these states.
RAN = {AgentRunStatus.completed, AgentRunStatus.blocked, AgentRunStatus.requires_human}
MAX_ACTIVITY = 500


def _owner_names(app) -> dict[str, dict[str, str]]:
    return {
        account.account_id: {"display_name": account.display_name, "email": account.email}
        for account in app.state.accounts.list_accounts()
    }


def _customer(case, owners: dict[str, dict[str, str]]) -> dict[str, Any]:
    owner = owners.get(case.owner_account_id or "") or {}
    return {
        "account_id": case.owner_account_id,
        "display_name": owner.get("display_name"),
        "name_on_document": case.customer_name,
    }


def payment_failures(app) -> dict[str, Any]:
    repository = app.state.workflow_repository
    cases = {case.case_id: case for case in repository.list_full_cases()}
    owners = _owner_names(app)
    attempts = PaymentRecordStore(repository.path).list_all()
    by_reference = {a.provider_reference: a for a in attempts if a.provider_reference}
    by_invoice = {a.invoice_number: a for a in attempts if a.invoice_number}

    failures: list[dict[str, Any]] = []
    for attempt in attempts:
        if attempt.status != "FAILED":
            continue
        case = cases.get(attempt.case_id or "")
        failures.append(
            {
                "source": "payment_attempt",
                "attempt_id": attempt.attempt_id,
                "failure_code": attempt.failure_code,
                "failure_reason": attempt.failure_reason,
                "invoice_number": attempt.invoice_number,
                "amount": str(attempt.amount) if attempt.amount is not None else None,
                "currency": attempt.currency,
                "payment_reference": attempt.provider_reference,
                "at": attempt.attempted_at,
                "case_id": attempt.case_id,
                "case_status": case.status.value if case else None,
                "customer": _customer(case, owners) if case else None,
            }
        )
    for row in MockBank(repository.path).ledger():
        if (row.get("status") or "").upper() != "FAILED":
            continue
        attempt = by_reference.get(row.get("payment_reference")) or by_invoice.get(
            row.get("invoice_number")
        )
        case = cases.get(attempt.case_id) if attempt and attempt.case_id else None
        failures.append(
            {
                "source": "ledger",
                "entry_id": row.get("entry_id"),
                "failure_code": row.get("failure_code"),
                "failure_reason": row.get("failure_reason"),
                "invoice_number": row.get("invoice_number"),
                "amount": row.get("amount"),
                "currency": row.get("currency"),
                "payment_reference": row.get("payment_reference"),
                "at": row.get("audit_timestamp") or row.get("created_at"),
                "case_id": case.case_id if case else None,
                "case_status": case.status.value if case else None,
                "customer": _customer(case, owners) if case else {
                    "account_id": None, "display_name": None, "name_on_document": row.get("customer_name"),
                },
            }
        )
    failures.sort(key=lambda item: item["at"] or "", reverse=True)
    return {"simulated": True, "count": len(failures), "failures": failures}


def activity(app, limit: int) -> dict[str, Any]:
    limit = max(1, min(int(limit), MAX_ACTIVITY))
    owners = _owner_names(app)
    entries: list[dict[str, Any]] = []
    # A case's updated_at is stamped by its newest audit entry, so the newest
    # ``limit`` entries all come from the ``limit`` most recently updated cases.
    for case in app.state.workflow_repository.list_full_cases(limit):
        customer = _customer(case, owners)
        for entry in case.audit:
            entries.append(
                {
                    "at": entry.at,
                    "case_id": case.case_id,
                    "invoice_number": case.invoice_number,
                    "customer": customer,
                    "actor": entry.actor.value,
                    "actor_account_id": entry.actor_account_id,
                    "action": entry.action,
                    "detail": entry.detail,
                    "status_after": entry.status_after,
                }
            )
    entries.sort(key=lambda item: item["at"], reverse=True)
    return {"count": min(len(entries), limit), "activity": entries[:limit]}


def customers(app) -> dict[str, Any]:
    cases = app.state.workflow_repository.list_full_cases()
    counts: Counter[str] = Counter()
    last: dict[str, str] = {}
    for case in cases:
        if not case.owner_account_id:
            continue
        counts[case.owner_account_id] += 1
        if case.updated_at > last.get(case.owner_account_id, ""):
            last[case.owner_account_id] = case.updated_at
    rows = []
    for account in app.state.accounts.list_accounts(role=ROLE_CUSTOMER):
        public = account.public()
        rows.append(
            {
                "account_id": public["account_id"],
                "display_name": public["display_name"],
                "email": public["email"],
                "created_at": public["created_at"],
                "case_count": counts.get(account.account_id, 0),
                "last_activity": last.get(account.account_id),
            }
        )
    return {"count": len(rows), "customers": rows}


def overview(app) -> dict[str, Any]:
    cases = app.state.workflow_repository.list_full_cases()
    by_status = Counter(case.status.value for case in cases)
    needing_person = [
        case.case_id for case in cases
        if case.requires_human
        or case.status in {CaseStatus.requires_review, CaseStatus.awaiting_approval, CaseStatus.pending}
    ]
    attempts = PaymentRecordStore(app.state.workflow_repository.path).list_all()
    payments = {
        # Settled means Agent 2 verified it; a simulator success alone is pending.
        "settled": sum(1 for a in attempts if a.status == "SUCCESS" and a.verified),
        "failed": sum(1 for a in attempts if a.status == "FAILED"),
        "pending": sum(
            1 for a in attempts if a.status == "PENDING" or (a.status == "SUCCESS" and not a.verified)
        ),
    }
    runs: Counter[str] = Counter()
    for case in cases:
        for run in case.agent_runs:
            if run.status in RAN and run.agent not in {AgentId.orchestrator}:
                runs[run.agent.value] += 1
    return {
        "cases_total": len(cases),
        "cases_by_status": dict(sorted(by_status.items())),
        "cases_needing_a_person": len(needing_person),
        "payments": payments,
        "agent_runs": dict(sorted(runs.items())),
    }


def install_admin_routes(app) -> None:
    """Register /admin/* (both admin roles) and /demo/* (DEMO_ADMIN only)."""

    @app.get("/admin/payment-failures")
    def admin_payment_failures(request: Request) -> dict[str, Any]:
        require_admin(request)
        return payment_failures(app)

    @app.get("/admin/activity")
    def admin_activity(request: Request, limit: int = 50) -> dict[str, Any]:
        require_admin(request)
        return activity(app, limit)

    @app.get("/admin/customers")
    def admin_customers(request: Request) -> dict[str, Any]:
        require_admin(request)
        return customers(app)

    @app.get("/admin/overview")
    def admin_overview(request: Request) -> dict[str, Any]:
        require_admin(request)
        return overview(app)

    @app.get("/demo/scenarios")
    def demo_scenarios(request: Request) -> dict[str, Any]:
        from .demo_scenarios import list_scenarios

        require_demo_admin(request)
        return {"simulated": True, "scenarios": list_scenarios()}

    @app.post("/demo/scenarios/{name}/run")
    def run_demo_scenario(name: str, request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from .demo_scenarios import DemoError, run_scenario

        admin = require_demo_admin(request)
        body = payload or {}
        try:
            customer = _demo_customer(app, body)
            case = run_scenario(app, name, admin=admin, customer=customer, mode=body.get("mode") or "scripted")
        except DemoError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return case.model_dump(mode="json")


def _demo_customer(app, body: dict[str, Any]):
    """The CUSTOMER account a demo case belongs to: by id, by email, or the first one."""

    from .demo_scenarios import DemoError

    store = app.state.accounts
    wanted_id = body.get("customer_account_id")
    wanted_email = (body.get("customer_email") or "").strip().lower()
    candidates = store.list_accounts(role=ROLE_CUSTOMER)
    if wanted_id:
        try:
            account = store.get(wanted_id)
        except AuthError as exc:
            raise DemoError("No account has that id.", 404) from exc
        candidates = [account]
    elif wanted_email:
        candidates = [a for a in candidates if a.email == wanted_email]
    if not candidates:
        raise DemoError(
            "No demo customer account found. Run `python -m tools.seed_demo_accounts` first.", 404
        )
    if candidates[0].role != ROLE_CUSTOMER:
        raise DemoError("Demo cases must belong to a CUSTOMER account.", 400)
    return candidates[0]
