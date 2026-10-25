"""Who may approve a payment, and the bank side's queue of cases awaiting approval.

Customers request payments; only BANK_ADMIN or DEMO_ADMIN accounts approve them,
and never the account that requested the payment. Agent 1 has always reported
by the time a case is in the queue, so the approver sees its result here.
"""

from __future__ import annotations

from typing import Any

from .accounts import ADMIN_ROLES, AccountStore, AuthError
from .schemas import AgentId, CaseStatus, Intent, WorkflowCase

ELIGIBLE_ROLES = sorted(ADMIN_ROLES)


def _requester(case: WorkflowCase, accounts: AccountStore) -> dict[str, Any] | None:
    if not case.owner_account_id:
        return None
    try:
        account = accounts.get(case.owner_account_id)
    except AuthError:
        return {"account_id": case.owner_account_id, "display_name": None, "email": None}
    return {"account_id": account.account_id, "display_name": account.display_name, "email": account.email}


def agent1_result(case: WorkflowCase) -> dict[str, Any] | None:
    """Agent 1's latest finding, trimmed to what an approver needs."""

    report = next((r for r in reversed(case.reports) if r.agent == AgentId.agent_1), None)
    if report is None:
        return None
    investigation = report.data.get("investigation") or {}
    # Agent 1's raw ``blocks_payment`` is deliberately not shown: it is also set
    # for "no comparison records", which the orchestrator does not treat as a
    # block. A case in the queue has already passed the orchestrator's check.
    return {
        "status": report.data.get("status"),
        "summary": report.summary,
        "previous_payment_status": investigation.get("previous_payment_status"),
        "needs_human_judgement": bool(report.requires_human),
        "recommended_next_action": report.next_action,
        "records_compared": report.data.get("records_compared"),
        "completed_at": report.completed_at,
    }


def approval_policy(case: WorkflowCase, accounts: AccountStore) -> dict[str, Any]:
    """Shape: {required, eligible_roles, requester, blocked: [{user, reason}]}."""

    is_payment = case.intent == Intent.new_payment_request or case.run_for(AgentId.human_approval) is not None
    requester = _requester(case, accounts)
    blocked: list[dict[str, str]] = [
        {"user": "CUSTOMER", "reason": "Customers can request payments but cannot approve them."}
    ]
    if requester is not None:
        blocked.insert(
            0,
            {
                "user": requester["display_name"] or requester["account_id"],
                "reason": "The account that requested a payment cannot approve it.",
            },
        )
    return {
        "required": bool(is_payment and case.approval is None),
        "eligible_roles": ELIGIBLE_ROLES,
        "requester": requester,
        "blocked": blocked,
    }


def approval_queue(repository, accounts: AccountStore, limit: int = 200) -> list[dict[str, Any]]:
    """Cases awaiting approval, oldest first, each with Agent 1's result."""

    queue: list[dict[str, Any]] = []
    for summary in repository.list_cases(limit=limit):
        if summary.status != CaseStatus.awaiting_approval:
            continue
        case = repository.get_case(summary.case_id)
        if case is None:
            continue
        queue.append(
            {
                "case_id": case.case_id,
                "created_at": case.created_at,
                "requester": _requester(case, accounts),
                "customer_name": case.customer_name,
                "invoice_number": case.invoice_number,
                "vendor": case.vendor_name,
                "amount": str(case.amount) if case.amount is not None else None,
                "currency": case.currency,
                "agent1": agent1_result(case),
            }
        )
    queue.sort(key=lambda item: item["created_at"])
    return queue
