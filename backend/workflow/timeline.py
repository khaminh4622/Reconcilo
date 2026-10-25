"""The payment timeline, derived from stored records.

The frontend renders this list directly, so a status can never be a hardcoded
string in the UI: each step's ``state`` and ``detail`` come from the case, the
Mock Bank's payment request, the ledgers and the agents' own reports.

    Document Uploaded -> Agent 3 Verified -> Agent 1 Duplicate Check ->
    Awaiting Approval -> Approved -> Mock Bank Processing -> Bank Settled ->
    Agent 2 Reconciled -> Agent 5 Confirmed

Agent 4 appears after the bank step only when the bank refused the payment (or
reconciliation could not confirm it).
"""

from __future__ import annotations

from typing import Any

from .mock_bank_accounts import (
    REQUEST_APPROVED,
    REQUEST_FAILED,
    REQUEST_PENDING,
    REQUEST_PROCESSING,
    REQUEST_REJECTED,
    REQUEST_SETTLED,
    REQUEST_UNCERTAIN,
)
from .schemas import AgentId, CaseStatus

DONE = "done"
CURRENT = "current"
WAITING = "waiting"
FAILED = "failed"
SKIPPED = "skipped"

STEP_LABELS = [
    ("document_uploaded", "Document Uploaded"),
    ("agent3_verified", "Agent 3 Verified"),
    ("agent1_duplicate_check", "Agent 1 Duplicate Check"),
    ("awaiting_approval", "Awaiting Approval"),
    ("approved", "Approved"),
    ("bank_processing", "Mock Bank Processing"),
    ("bank_settled", "Bank Settled"),
    ("agent2_reconciled", "Agent 2 Reconciled"),
    ("agent5_confirmed", "Agent 5 Confirmed"),
    ("agent4_exception", "Agent 4 Investigation"),
]


def _step(state: str, detail: str, at: str | None = None, **extra) -> dict:
    return {"state": state, "detail": detail, "at": at, **extra}


def build_timeline(app, case) -> dict[str, Any]:
    store = app.state.bank_store
    payment = store.payment_request(getattr(case, "payment_request_id", None))
    reports = case.reports or []
    audit = case.audit or []

    def first_audit(action: str) -> str | None:
        for entry in audit:
            if entry.action == action:
                return entry.at
        return None

    def report_of(agent: AgentId):
        return next((r for r in reversed(reports) if r.agent == agent), None)

    steps: list[dict] = []
    index = {key: position for position, (key, _label) in enumerate(STEP_LABELS)}

    # -- 1. Uploaded -----------------------------------------------------
    steps.append(
        _step(
            DONE if case.analysis else WAITING,
            (
                "The customer submitted the document."
                if case.analysis
                else "No document has been attached to this case."
            ),
            case.created_at,
        )
    )

    # -- 2. Agent 3 verified ---------------------------------------------
    agent3 = next(
        (run for run in case.agent_runs if run.agent == AgentId.agent_3), None
    )
    verified = bool(case.analysis) and case.status not in {CaseStatus.received, CaseStatus.analyzing}
    steps.append(
        _step(
            DONE if verified else (CURRENT if case.analysis else WAITING),
            (
                f"Invoice {case.invoice_number or '(none)'} read and validated "
                f"for {case.amount or '—'} {case.currency or ''}.".strip()
                if verified
                else "Agent 3 has not finished reading the document yet."
            ),
            agent3.completed_at if agent3 else None,
        )
    )

    # -- 3. Agent 1 duplicate check --------------------------------------
    agent1 = report_of(AgentId.agent_1)
    if agent1 is None:
        steps.append(_step(WAITING, "Agent 1 has not run for this case yet."))
    else:
        raw_blocks = (agent1.data.get("investigation") or {}).get("blocks_payment")
        # Agent 1 reports "blocks_payment" even for a first invoice with no
        # comparison history, which is not an actual block. It only counts as a
        # block when the case stopped before approval.
        stopped_before_approval = (
            case.status in {CaseStatus.requires_review, CaseStatus.failed}
            and case.approval is None
            and payment is None
        )
        blocked = stopped_before_approval and bool(raw_blocks)
        steps.append(
            _step(
                FAILED if blocked else DONE,
                agent1.summary,
                agent1.completed_at,
                blocking=bool(blocked),
            )
        )

    # -- 4-5. Approval, driven by the bank's own request row -------------
    if case.approval is not None:
        approval_state, approval_detail, approval_at = (
            DONE,
            f"Approved by {case.approval.approved_by} for the exact invoice, payee, amount and currency.",
            case.approval.approved_at,
        )
    elif case.status == CaseStatus.awaiting_approval:
        approval_state, approval_detail, approval_at = (
            CURRENT,
            "Waiting for a bank administrator to authorise this payment.",
            None,
        )
    elif case.status in {CaseStatus.requires_review, CaseStatus.failed} and not (
        payment and payment["status"] in {REQUEST_APPROVED, REQUEST_PROCESSING, REQUEST_SETTLED}
    ):
        approval_state, approval_detail, approval_at = (
            SKIPPED,
            "The case did not reach a payment decision.",
            None,
        )
    else:
        approval_state, approval_detail, approval_at = (WAITING, "Not reached yet.", None)
    steps.append(_step(approval_state, approval_detail, approval_at))

    if case.approval is not None:
        approved_step = _step(
            DONE,
            "The authorisation is recorded on the bank's payment request.",
            case.approval.approved_at,
        )
    elif payment and payment["status"] == REQUEST_PENDING:
        approved_step = _step(CURRENT, "Awaiting a human authorisation before the bank can act.")
    else:
        approved_step = _step(WAITING, "No authorisation recorded yet.")
    steps.append(approved_step)

    # -- 6-7. Processing and settlement ----------------------------------
    bank_status = (payment or {}).get("status")
    if payment is None:
        steps.append(_step(WAITING, "No simulated payment request exists for this case."))
        steps.append(_step(WAITING, "Nothing has been submitted to the bank."))
    else:
        if bank_status == REQUEST_PENDING:
            processing = (WAITING, "The bank has not received an authorised request yet.")
            settled = (WAITING, "The bank has not processed the payment.")
        elif bank_status == REQUEST_APPROVED:
            processing = (
                CURRENT,
                f"Authorised request {payment['payment_id']} is queued for the bank to process.",
            )
            settled = (WAITING, "The bank has not processed the payment yet.")
        elif bank_status == REQUEST_PROCESSING:
            processing = (CURRENT, "The bank is applying its settlement rules.")
            settled = (WAITING, "Settlement is in progress.")
        elif bank_status == REQUEST_SETTLED:
            processing = (DONE, "The bank accepted and processed the instruction.")
            settled = (
                DONE,
                f"Both account entries are committed under {payment.get('settlement_reference')}.",
            )
        elif bank_status in {REQUEST_FAILED, REQUEST_REJECTED}:
            processing = (
                DONE,
                f"The bank processed the instruction and refused it ({payment.get('failure_code')}).",
            )
            settled = (
                FAILED,
                f"{payment.get('failure_code')}: {payment.get('failure_reason') or 'no reason given'}",
            )
        elif bank_status == REQUEST_UNCERTAIN:
            processing = (DONE, "The bank accepted the instruction but did not confirm the outcome.")
            settled = (
                CURRENT,
                "Outcome unknown. It must be investigated; it must never be retried automatically.",
            )
        else:
            processing = (WAITING, f"The bank reports {bank_status}.")
            settled = (WAITING, "Not settled.")
        steps.append(_step(processing[0], processing[1], payment.get("processed_at")))
        steps.append(_step(settled[0], settled[1], payment.get("processed_at")))

    # -- 8. Agent 2 reconciliation ---------------------------------------
    agent2 = report_of(AgentId.agent_2)
    verdict = (case.reconciliation or {}).get("verdict")
    if agent2 is None or verdict is None:
        state = WAITING if not (bank_status == REQUEST_SETTLED) else CURRENT
        steps.append(
            _step(
                state,
                "Agent 2 verifies the settlement against the bank's own entries."
                if state == CURRENT
                else "Not reached: there is no settlement to reconcile yet.",
            )
        )
    else:
        steps.append(
            _step(
                DONE if verdict in {"MATCHED", "SETTLEMENT_VERIFIED"} else FAILED,
                (case.reconciliation or {}).get("summary") or agent2.summary,
                agent2.completed_at,
            )
        )

    # -- 9. Agent 5 confirmed -------------------------------------------
    agent5 = report_of(AgentId.agent_5)
    if agent5 is None:
        steps.append(_step(WAITING, "Agent 5 reports the outcome last."))
    else:
        response = case.customer_response
        steps.append(
            _step(
                DONE if response and response.payment_settled else CURRENT,
                agent5.summary,
                agent5.completed_at,
            )
        )

    # -- 10. Agent 4, only when there is an exception --------------------
    agent4 = report_of(AgentId.agent_4)
    if agent4 is not None:
        steps.append(
            _step(FAILED if agent4.requires_human else DONE, agent4.summary, agent4.completed_at)
        )

    # A step that cannot still move is not "current".
    terminal = case.status in {
        CaseStatus.completed,
        CaseStatus.failed,
        CaseStatus.requires_review,
    }
    if terminal:
        for position, step in enumerate(steps):
            if step["state"] == CURRENT and position < len(steps) - 1:
                step["state"] = DONE if position < index["agent5_confirmed"] else CURRENT

    labels = dict(STEP_LABELS)
    return {
        "simulated": True,
        "case_id": case.case_id,
        "status": case.status.value,
        "bank_status": bank_status,
        "payment_id": (payment or {}).get("payment_id"),
        "settlement_reference": (payment or {}).get("settlement_reference"),
        "steps": [
            {
                "key": STEP_LABELS[position][0],
                "label": labels[STEP_LABELS[position][0]],
                **step,
            }
            for position, step in enumerate(steps)
        ],
    }
