"""Agent 4 - Payment Exception Specialist.

Investigates failed, rejected, returned or uncertain payments. It diagnoses and
recommends; it never retries a payment itself.
"""

from __future__ import annotations

from .repository import WorkflowRepository
from .schemas import (
    AGENT_LABELS,
    AgentId,
    AgentReport,
    Finding,
    PaymentAttempt,
    WorkflowCase,
    utc_now,
)


# Known failure signals mapped to a diagnosis and a safe recommended resolution.
DIAGNOSES: dict[str, tuple[str, str, str]] = {
    "insufficient_funds": (
        "The account did not have sufficient cleared funds when the payment was submitted.",
        "Confirm the account balance, then obtain fresh human approval before any retry.",
        "human",
    ),
    "invalid_account": (
        "The payee account details were rejected as invalid by the payment network.",
        "Verify the payee's bank details with the vendor directly, then re-request approval.",
        "human",
    ),
    "rejected": (
        "The receiving bank rejected the transaction.",
        "Contact the payee to confirm their details, then re-request approval.",
        "human",
    ),
    "returned": (
        "The payment was applied and later returned by the receiving bank.",
        "Reconcile with Agent 2 to confirm the funds came back before attempting anything else.",
        "agent_2",
    ),
    "timeout": (
        "The payment gateway did not confirm the outcome within the expected time.",
        "Treat as uncertain: investigate the existing attempt before considering a new one.",
        "human",
    ),
    "duplicate": (
        "The payment was blocked because an identical payment already exists.",
        "No further payment should be made; verify the original transaction statement.",
        "human",
    ),
}


class PaymentExceptionSpecialist:
    """Agent 4: diagnose payment exceptions and recommend resolution."""

    agent = AgentId.agent_4

    def __init__(self, repository: WorkflowRepository):
        self.repository = repository

    def investigate(
        self,
        case: WorkflowCase,
        attempt: PaymentAttempt | None = None,
        reported_reason: str | None = None,
    ) -> AgentReport:
        started = utc_now()
        findings: list[Finding] = []

        # Prefer the attempt Agent 5 recorded; fall back to the case's report.
        if attempt is None:
            attempts = self.repository.list_attempts(case.case_id)
            attempt = attempts[-1] if attempts else None

        status = attempt.status if attempt else "UNKNOWN"
        reason_text = (reported_reason or (attempt.failure_reason if attempt else None) or "").casefold()

        findings.append(
            Finding(
                code="exception_status",
                label="Transaction status",
                status="info",
                detail=(
                    f"The payment attempt is in state {status}"
                    + (f" (reason: {attempt.failure_reason})." if attempt and attempt.failure_reason else ".")
                ),
            )
        )

        # A failed attempt is never a completed transfer.
        if status == "FAILED":
            findings.append(
                Finding(
                    code="not_settled",
                    label="Funds were not transferred",
                    status="info",
                    detail="A failed attempt does not mean money moved. Do not record it as a payment.",
                )
            )
        elif status == "PENDING":
            findings.append(
                Finding(
                    code="outcome_uncertain",
                    label="Outcome uncertain",
                    status="unverified",
                    detail=(
                        "The attempt is pending. It must be investigated with the payment provider "
                        "before any new attempt is made. Blind retries can double-pay."
                    ),
                )
            )

        diagnosis_code = "unknown"
        for code in DIAGNOSES:
            if code.replace("_", " ") in reason_text or code in reason_text:
                diagnosis_code = code
                break
        if diagnosis_code == "unknown" and status == "PENDING":
            diagnosis_code = "timeout"

        if diagnosis_code in DIAGNOSES:
            diagnosis, resolution, escalate = DIAGNOSES[diagnosis_code]
        else:
            diagnosis = (
                "The failure reason was not reported clearly enough to diagnose automatically."
                if status != "SUCCESS"
                else "The transaction did not fail; no exception handling is required."
            )
            resolution = "A human must inspect the payment provider's records before any further action."
            escalate = "human"

        findings.append(
            Finding(
                code="diagnosis",
                label="Diagnosis",
                status="unverified" if status != "SUCCESS" else "passed",
                detail=diagnosis,
            )
        )
        findings.append(
            Finding(
                code="recommended_resolution",
                label="Recommended resolution",
                status="info",
                detail=resolution,
            )
        )

        retry_allowed = False
        findings.append(
            Finding(
                code="retry_policy",
                label="Retry policy",
                status="info",
                detail=(
                    "Agent 4 does not retry payments. Any new attempt requires fresh human approval "
                    "and confirmation that the earlier attempt did not settle."
                ),
            )
        )

        resolved = status == "SUCCESS"
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=(
                "No exception: the payment did not fail."
                if resolved
                else f"Payment exception diagnosed: {diagnosis}"
            ),
            findings=findings,
            recommended_next=AgentId.agent_2 if escalate == "agent_2" else None,
            next_action=resolution,
            requires_human=escalate == "human" or not resolved,
            blocking=True,
            resolved=resolved,
            data={
                "transaction_status": status,
                "failure_reason": attempt.failure_reason if attempt else reported_reason,
                "diagnosis_code": diagnosis_code,
                "recommended_resolution": resolution,
                "retry_performed": retry_allowed,
                "attempt": attempt.model_dump(mode="json") if attempt else None,
            },
            started_at=started,
            completed_at=utc_now(),
        )
