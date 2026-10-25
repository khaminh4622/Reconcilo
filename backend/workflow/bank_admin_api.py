"""FastAPI routes for the Bank Admin dashboard.

Every route here is restricted to the ``BANK_ADMIN`` role. Customers and demo
administrators get 403, enforced by ``require_bank_admin`` on the server, not
by hiding a navigation item.

The dashboard's ``/approve`` route records the human authorisation first and
then invokes the bank processing path.  The two stages remain separately
audited, while the administrator gets one safe action.  ``/process`` remains
available only for an already-authorised request (for example, recovery of a
previously interrupted dashboard action).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from .auth_api import require_bank_admin
from .bank_admin import (
    audit_log,
    bank_overview,
    bank_transactions,
    process_request,
    request_queue,
)
from .mock_bank_accounts import BankError
from .auth_api import require_account
from .orchestrator import WorkflowError


def _process_and_continue(app, payment_id: str, *, account) -> dict[str, Any]:
    """Process one authorised bank request and advance its workflow case.

    The bank transfer itself is idempotent.  This wrapper also avoids running
    Agent 2/Agent 5 a second time when a browser retries an already-settled
    request after a slow response.
    """

    result = process_request(app, payment_id, account=account)
    case = _case_for(app, payment_id)
    if case is not None:
        if not result.get("already_settled"):
            try:
                case = app.state.orchestrator.apply_bank_outcome(
                    case.case_id,
                    result["status"],
                    failure_code=result.get("failure_code"),
                    failure_reason=result.get("failure_reason"),
                    actor_account_id=account.account_id,
                )
            except Exception as exc:  # noqa: BLE001 - a committed bank result stands
                result["workflow_error"] = str(exc)
        result["case_id"] = case.case_id
        result["case_status"] = case.status.value
    result["simulated"] = True
    result["disclaimer"] = "SIMULATED - NO REAL MONEY MOVED."
    return result


def install_bank_admin_routes(app) -> None:
    @app.get("/bank-admin/overview")
    def overview(request: Request) -> dict[str, Any]:
        """Simulated accounts, balances and the request counts."""

        require_bank_admin(request)
        return bank_overview(app)

    @app.get("/bank-admin/requests")
    def requests(request: Request, status: str | None = None) -> dict[str, Any]:
        """Payment requests, newest first, actionable work at the top."""

        require_bank_admin(request)
        return request_queue(app, status=status)

    @app.get("/bank-admin/transactions")
    def transactions(request: Request, limit: int = 200, account_id: str | None = None) -> dict[str, Any]:
        require_bank_admin(request)
        return bank_transactions(app, limit=limit, account_id=account_id)

    @app.get("/bank-admin/audit")
    def audit(request: Request, limit: int = 200) -> dict[str, Any]:
        require_bank_admin(request)
        return audit_log(app, limit=limit)

    @app.post("/bank-admin/requests/{payment_id}/process")
    def process(payment_id: str, request: Request) -> dict[str, Any]:
        """Apply the bank's deterministic rules to an approved request.

        This is transaction processing, not authorisation: a request that has
        not been approved is refused and recorded as FAILED with the reason.
        """

        account = require_bank_admin(request)
        try:
            return _process_and_continue(app, payment_id, account=account)
        except BankError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/bank-admin/requests/{payment_id}/approve")
    def approve(payment_id: str, request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Authorise a pending request, then settle it through the Mock Bank.

        This is the dashboard's one-click bank-admin operation.  It deliberately
        delegates authorisation to the orchestrator so Agent 1's duplicate
        guard, case audit trail, and the recorded human decision remain the
        source of truth.  Only after that record exists does the bank transfer
        start.
        """

        account = require_bank_admin(request)
        bank_request = app.state.bank_store.payment_request(payment_id)
        if bank_request is None:
            raise HTTPException(status_code=404, detail="That payment request does not exist.")
        case = _case_for(app, payment_id)
        if case is None:
            raise HTTPException(
                status_code=409,
                detail="This bank request is not linked to a workflow case and cannot be authorised here.",
            )

        # A repeated click after settlement reports the original result.  It
        # cannot create another approval, debit, credit, or agent run.
        if bank_request["status"] == "SETTLED":
            return _process_and_continue(app, payment_id, account=account)
        if bank_request["status"] != "PENDING_APPROVAL":
            raise HTTPException(
                status_code=409,
                detail=f"Payment {payment_id} is {bank_request['status']} and cannot be approved again.",
            )

        note = str((payload or {}).get("note") or "").strip() or None
        try:
            approved_case = app.state.orchestrator.approve(
                case.case_id,
                approved_by=f"{account.display_name} ({account.account_id})",
                approver_account_id=account.account_id,
                note=note,
                # Processing must use the bank-admin path below, never the
                # legacy direct execution route.
                process=False,
            )
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if approved_case.status.value != "APPROVED":
            raise HTTPException(
                status_code=409,
                detail="The workflow did not approve this payment, so it was not sent to the Mock Bank.",
            )
        try:
            result = _process_and_continue(app, payment_id, account=account)
        except BankError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        result["approval_recorded"] = True
        return result

    @app.post("/bank-admin/requests/{payment_id}/reject")
    def reject(payment_id: str, request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record a reasoned human rejection without sending funds."""

        account = require_bank_admin(request)
        reason = str((payload or {}).get("reason") or "").strip()
        if not reason:
            raise HTTPException(status_code=422, detail="A rejection reason is required.")
        bank_request = app.state.bank_store.payment_request(payment_id)
        if bank_request is None:
            raise HTTPException(status_code=404, detail="That payment request does not exist.")
        if bank_request["status"] != "PENDING_APPROVAL":
            raise HTTPException(
                status_code=409,
                detail=f"Payment {payment_id} is {bank_request['status']} and cannot be rejected again.",
            )
        case = _case_for(app, payment_id)
        if case is None:
            raise HTTPException(
                status_code=409,
                detail="This bank request is not linked to a workflow case and cannot be rejected here.",
            )
        try:
            rejected_case = app.state.orchestrator.reject(
                case.case_id,
                f"{account.display_name} ({account.account_id})",
                reason,
                rejector_account_id=account.account_id,
            )
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        rejected_request = app.state.bank_store.payment_request(payment_id)
        return {
            "simulated": True,
            "disclaimer": "SIMULATED - NO REAL MONEY MOVED.",
            "status": (rejected_request or bank_request)["status"],
            "request": rejected_request or bank_request,
            "case_id": rejected_case.case_id,
            "case_status": rejected_case.status.value,
            "rejection_recorded": True,
        }

    @app.post("/bank-admin/accounts/{account_id}/status")
    def account_status(account_id: str, request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Freeze, close or reactivate a simulated account (bank admins only)."""

        account = require_bank_admin(request)
        status = str((payload or {}).get("status") or "").upper()
        try:
            updated = app.state.bank_store.set_account_status(
                account_id, status, actor_id=account.account_id, actor_role=account.role
            )
        except BankError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"simulated": True, "account": updated}


def _case_for(app, payment_id: str):
    store = app.state.bank_store
    request = store.payment_request(payment_id)
    if not request or not request.get("case_id"):
        return None
    return app.state.orchestrator.get(request["case_id"])
