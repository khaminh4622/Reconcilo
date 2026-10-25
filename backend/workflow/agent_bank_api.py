"""Read-only endpoints for the restricted agent bank tools.

The agents call these functions directly inside the orchestrator; the endpoints
exist so a bank admin can *see* exactly what an agent was allowed to read for a
case (and get a 403 when the scope does not match). They never change data.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from .auth_api import current_account, require_admin
from .bank_tools import BANK_TOOL_AGENTS, AgentBankTools, BankToolError, CaseContext


def install_agent_bank_routes(app) -> None:
    @app.get("/bank-tools/catalogue")
    def catalogue(request: Request) -> dict[str, Any]:
        """Which agent may call which bank tool. Admins only."""

        require_admin(request)
        return {
            "simulated": True,
            "tools": AgentBankTools(app.state.bank_store.path).describe(),
            "note": (
                "The tools are read-only. None of them can change a balance, approve a payment "
                "or create a transaction, so a model cannot invent a settlement."
            ),
        }

    @app.get("/bank-tools/{agent_id}/{tool}/{payment_id}")
    def run_tool(agent_id: str, tool: str, payment_id: str, request: Request) -> dict[str, Any]:
        """Run one tool the way the agent would, with this case's scope.

        A bank admin may inspect any case. A customer may only inspect a case
        they own, so the endpoint cannot be used to read another customer's bank
        evidence.
        """

        account = current_account(request)
        if account is None:
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        if tool not in {name for tools in BANK_TOOL_AGENTS.values() for name in tools}:
            raise HTTPException(status_code=404, detail="No such bank tool.")

        store = app.state.bank_store
        payment = store.payment_request(payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="No such payment request.")
        case = app.state.orchestrator.get(payment.get("case_id") or "")
        if case is None:
            raise HTTPException(status_code=404, detail="No such case.")
        if not account.is_admin and case.owner_account_id != account.account_id:
            raise HTTPException(status_code=404, detail="No such case.")

        tools = AgentBankTools(app.state.bank_store.path)
        context = CaseContext(
            agent_id=agent_id,
            case_id=case.case_id,
            customer_id=case.owner_account_id,
            invoice_id=case.invoice_number,
        )
        try:
            result = getattr(tools, tool)(payment_id, context)
        except BankToolError as exc:
            raise HTTPException(
                status_code=403, detail=f"{exc} (scope: {exc.reason})"
            ) from exc
        return {
            "simulated": True,
            "case_id": case.case_id,
            "invoice_number": case.invoice_number,
            "agent_id": agent_id,
            "tool": tool,
            "result": result,
        }

    @app.get("/cases/{case_id}/timeline")
    def case_timeline(case_id: str, request: Request) -> dict[str, Any]:
        """The payment timeline, built from stored records only.

        Every step's ``state`` is derived from the case, the payment request and
        the bank's own rows. The frontend renders this as-is; it never decides a
        status itself.
        """

        from .timeline import build_timeline

        account = current_account(request)
        if account is None:
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        case = app.state.orchestrator.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        if not account.is_admin and case.owner_account_id != account.account_id:
            raise HTTPException(status_code=404, detail="Case not found.")
        return build_timeline(app, case)
