"""Agent 5 - Final Confirmation and Customer Response.

Agent 5 is the last agent in every workflow. It does not move money and it does
not investigate: it reads the structured reports produced by the other agents and
turns them into the customer-facing answer plus the case's terminal status.

The rules it enforces:

* A payment is only confirmed when Agent 2 verified a matching SETTLED bank
  transaction. A simulator success alone proves nothing.
* Nothing is ever retried here; Agent 5 has no execution path at all.
* When the evidence does not establish something, the response says so.
"""

from __future__ import annotations

from decimal import Decimal

from .repository import WorkflowRepository
from .schemas import (
    AGENT_LABELS,
    AgentId,
    AgentReport,
    Finding,
    PaymentReceipt,
    WorkflowCase,
    utc_now,
)
from .payment_store import ReceiptStore


# Agent 2 verdicts that mean "the bank confirms this settlement".
VERIFIED_VERDICTS = {"MATCHED", "SETTLEMENT_VERIFIED"}


class FinalConfirmationAgent:
    """Agent 5: confirm the outcome and answer the customer."""

    agent = AgentId.agent_5

    def __init__(self, repository: WorkflowRepository, response_writer=None, receipts=None):
        self.repository = repository
        self.receipts = receipts or ReceiptStore(repository.path)
        # Optional LLM writer for the customer-facing prose.
        self.response_writer = response_writer

    # ==================================================================
    # Path A - after reconciliation (or an exception)
    # ==================================================================
    def confirm_payment(
        self,
        case: WorkflowCase,
        recommendation: dict | None,
        approval=None,
    ) -> AgentReport:
        """Finalise a payment case using Agent 2's verdict."""

        started = utc_now()
        findings: list[Finding] = []
        verdict = (recommendation or {}).get("verdict") or "NEEDS_REVIEW"
        independent = bool((recommendation or {}).get("independent_evidence"))
        attempt = self._attempt_for(case)

        # --- Verified settlement -------------------------------------------
        if verdict in VERIFIED_VERDICTS and independent and attempt is not None:
            return self._confirm_settled(case, attempt, recommendation, approval, findings, started)

        if verdict in VERIFIED_VERDICTS and not independent:
            findings.append(
                Finding(
                    code="unverified_evidence",
                    label="Not independently verified",
                    status="unverified",
                    detail=(
                        "Agent 2 matched the payment but could not corroborate it with independent "
                        "settlement evidence, so Agent 5 will not confirm it."
                    ),
                )
            )
            return self._not_confirmed(case, attempt, "PENDING", verdict, findings, started)

        findings.append(
            Finding(
                code="settlement_not_verified",
                label="Settlement not verified",
                status="failed",
                detail=(
                    f"Agent 2 reported {verdict}. The bank does not confirm this settlement, so "
                    "Agent 5 cannot report the payment as complete."
                ),
            )
        )
        # An instruction the bank has not settled is PENDING, not a review
        # failure: nothing is wrong, the money simply has not arrived yet.
        unsettled = (
            verdict in {"PENDING", "PROCESSING", "NEEDS_HUMAN_REVIEW", "NEEDS_REVIEW"}
            and attempt is not None
            and attempt.status == "PENDING"
        )
        status = "PENDING" if unsettled else "REQUIRES_REVIEW"
        if unsettled:
            findings.append(
                Finding(
                    code="awaiting_settlement",
                    label="Awaiting settlement",
                    status="unverified",
                    detail=(
                        "The Mock Bank has not settled this instruction. It must be checked again "
                        "rather than retried."
                    ),
                )
            )
        return self._not_confirmed(case, attempt, status, verdict, findings, started)

    # ==================================================================
    # Path A - after a rejected instruction
    # ==================================================================
    def report_failure(
        self,
        case: WorkflowCase,
        exception_report: AgentReport | None,
        attempt=None,
    ) -> AgentReport:
        """Close a failed payment with a final failure explanation."""

        started = utc_now()
        findings: list[Finding] = []
        data = (exception_report.data if exception_report else {}) or {}
        attempt = attempt or self._attempt_for(case)

        findings.append(
            Finding(
                code="final_failure",
                label="Payment did not complete",
                status="passed",
                detail=(
                    "The bank did not credit the recipient, so no invoice balance changed and "
                    "nothing was paid."
                ),
            )
        )
        code = data.get("failure_code")
        if code:
            findings.append(
                Finding(
                    code="verified_failure_reason",
                    label="Verified failure reason",
                    status="passed",
                    detail=f"The Mock Bank reported {code}: {data.get('failure_reason') or data.get('diagnosis') or ''}".strip(),
                )
            )
        else:
            findings.append(
                Finding(
                    code="failure_reason_unknown",
                    label="Failure reason not established",
                    status="unverified",
                    detail=(
                        "The bank did not report a reason code, so the cause is unknown and must "
                        "not be guessed."
                    ),
                )
            )

        response = self._customer_response(
            case,
            status="FAILED",
            verdict=data.get("reconciliation_verdict") or code or "PAYMENT_FAILED",
            exception=data,
        )
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=(
                f"Final outcome: the payment for {case.invoice_number} did not complete."
            ),
            findings=findings,
            next_action=data.get("recommended_resolution") or "A human must review this case.",
            requires_human=True,
            blocking=True,
            resolved=False,
            data={
                "final_status": "FAILED",
                "confirmed": False,
                "attempt": attempt.model_dump(mode="json") if attempt else None,
                "exception": data,
                "customer_response": response,
            },
            started_at=started,
            completed_at=utc_now(),
        )

    # ==================================================================
    # Path B - answer an investigation with no payment
    # ==================================================================
    def explain_case(self, case: WorkflowCase) -> AgentReport:
        """Close a customer-problem case with a final explanation."""

        started = utc_now()
        findings: list[Finding] = []
        contributed = [r for r in case.reports if r.agent != self.agent]

        for report in contributed:
            findings.append(
                Finding(
                    code=f"{report.agent.value}_summary",
                    label=report.agent_name,
                    status="passed" if report.resolved else "unverified",
                    detail=report.summary,
                )
            )

        # Is there a verified settlement to report?
        a2 = next((r for r in reversed(contributed) if r.agent == AgentId.agent_2), None)
        verified = bool(
            a2 and (a2.data.get("matched") is True or a2.data.get("reconciliation", {}).get("outcome") == "SETTLEMENT_VERIFIED")
        )
        exception = next((r for r in reversed(contributed) if r.agent == AgentId.agent_4), None)
        final_status = "SETTLED" if verified else ("REQUIRES_REVIEW" if exception else "INVESTIGATED")

        response = self._customer_response(
            case,
            status=final_status,
            verdict=(a2.data.get("verdict") if a2 else None) or final_status,
            exception=exception.data if exception else None,
        )

        needs_human = not verified and not case.receipt
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=(
                f"Final answer issued for {case.invoice_number or 'this case'}: "
                f"{'settlement verified' if verified else 'not confirmed as settled'}."
            ),
            findings=findings,
            next_action=(
                "Settlement verified; no further action required."
                if verified
                else "A human must review the outstanding findings."
            ),
            requires_human=needs_human,
            blocking=False,
            resolved=verified,
            data={
                "final_status": final_status,
                "confirmed": verified,
                "agents_completed": [r.agent.value for r in contributed],
                "customer_response": response,
            },
            started_at=started,
            completed_at=utc_now(),
        )

    # ------------------------------------------------------------------
    def _confirm_settled(self, case, attempt, recommendation, approval, findings, started) -> AgentReport:
        attempt.status = "SUCCESS"
        attempt.provider_status = "SETTLED"
        attempt.verified = True
        attempt.settled_at = utc_now()
        attempt.settlement_reference = (
            recommendation.get("matched_reference") or attempt.provider_reference
        )

        from .payment_store import PaymentRecordStore

        PaymentRecordStore(self.repository.path).update(attempt)

        # Link the attempt to the bank entry Agent 2 verified.
        try:
            from .mock_bank import MockBank

            entries = [
                e for e in MockBank(self.repository.path).credits_for(attempt.provider_reference)
                if (e.get("status") or "").upper() in {"SETTLED", "PARTIAL"}
            ]
            if entries:
                attempt.transaction_id = entries[-1]["entry_id"]
                PaymentRecordStore(self.repository.path).update(attempt)
        except Exception:  # noqa: BLE001
            pass

        self._mark_invoice_paid(case, attempt)
        receipt = self._issue_receipt(case, attempt, recommendation, approval)
        self.receipts.save(receipt)

        findings.append(
            Finding(
                code="payment_confirmed",
                label="Settlement verified by Agent 2",
                status="passed",
                detail=(
                    f"{recommendation.get('settled_amount')} "
                    f"{recommendation.get('currency') or attempt.currency} confirmed under "
                    f"{recommendation.get('matched_reference') or attempt.provider_reference}."
                ),
            )
        )
        response = self._customer_response(
            case,
            status="SETTLED",
            verdict=recommendation.get("verdict"),
            receipt=receipt,
        )
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=f"Payment confirmed for {attempt.invoice_number}; receipt issued.",
            findings=findings,
            next_action="No further action required.",
            requires_human=False,
            blocking=False,
            resolved=True,
            data={
                "final_status": "PAID",
                "confirmed": True,
                "attempt": attempt.model_dump(mode="json"),
                "receipt": receipt.model_dump(mode="json"),
                "reconciliation": recommendation,
                "customer_response": response,
            },
            started_at=started,
            completed_at=utc_now(),
        )

    def _not_confirmed(self, case, attempt, status, verdict, findings, started) -> AgentReport:
        if attempt is not None and status == "REQUIRES_REVIEW":
            attempt.failure_reason = attempt.failure_reason or f"Agent 2 reported {verdict}."
            from .payment_store import PaymentRecordStore

            PaymentRecordStore(self.repository.path).update(attempt)
        response = self._customer_response(case, status=status, verdict=verdict)
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=f"Payment not confirmed: Agent 2 reported {verdict}.",
            findings=findings,
            next_action="A human must resolve the reconciliation result.",
            requires_human=True,
            blocking=True,
            resolved=False,
            data={
                "final_status": status,
                "confirmed": False,
                "verdict": verdict,
                "attempt": attempt.model_dump(mode="json") if attempt else None,
                "customer_response": response,
            },
            started_at=started,
            completed_at=utc_now(),
        )

    # ------------------------------------------------------------------
    def _customer_response(self, case, *, status, verdict, receipt=None, exception=None) -> dict:
        """The customer-facing answer, or a deterministic one if no model."""

        deterministic = {
            "final_status": status,
            "customer_request": self._customer_request(case),
            "investigation": self._investigation_summary(case),
            "verified_findings": self._verified_findings(case, verdict),
            "explanation": self._explanation(status, verdict, exception),
            "next_steps": self._next_steps(status, exception),
            "agents": [r.agent.value for r in case.reports if r.agent != self.agent],
            "generated_by": "deterministic",
        }
        if receipt is not None:
            deterministic["receipt_id"] = receipt.receipt_id

        if self.response_writer is not None:
            try:
                llm = self.response_writer.write(case, deterministic)
            except Exception:  # noqa: BLE001
                llm = None
            if llm:
                deterministic.update(llm)
                deterministic["generated_by"] = "llm"
        return deterministic

    def _customer_request(self, case: WorkflowCase) -> str:
        analysis = case.analysis or {}
        problem = analysis.get("detected_problem") or {}
        description = problem.get("description")
        if description:
            return str(description)
        return f"A request concerning invoice {case.invoice_number or 'unknown'}."

    def _investigation_summary(self, case: WorkflowCase) -> str:
        labels = [r.agent_name for r in case.reports if r.agent != self.agent]
        if not labels:
            return "No agent produced a report for this case."
        return "GhostLedger checked this case with: " + ", ".join(labels) + "."

    def _verified_findings(self, case: WorkflowCase, verdict) -> list[str]:
        facts: list[str] = []
        for report in case.reports:
            if report.agent == self.agent:
                continue
            for finding in report.findings:
                if finding.status == "passed":
                    facts.append(f"{report.agent_name}: {finding.detail}")
        return facts[:8]

    def _explanation(self, status, verdict, exception) -> str:
        if status == "SETTLED":
            return (
                "The Mock Bank shows a settled transaction matching this invoice, and Agent 2 "
                "verified it. The payment is confirmed."
            )
        if status == "FAILED":
            code = (exception or {}).get("failure_code")
            reason = (exception or {}).get("failure_reason")
            if code:
                return f"The payment did not complete. The bank reported {code}: {reason or 'see the record'}."
            return (
                "The payment did not complete, but the bank did not report a reason code, so the "
                "cause is unknown."
            )
        if verdict:
            return (
                f"The payment could not be confirmed because reconciliation reported {verdict}. "
                "No money is recorded as settled on this basis."
            )
        return "The available records do not confirm the outcome of this case."

    def _next_steps(self, status, exception) -> str:
        if status == "SETTLED":
            return "None."
        if exception:
            return exception.get("recommended_resolution") or "A human must review this case."
        return "A human must review the outstanding findings before any further action."

    def _attempt_for(self, case: WorkflowCase):
        if case.payment is not None:
            return case.payment
        from .payment_store import PaymentRecordStore

        history = PaymentRecordStore(self.repository.path).list_for_invoice(case.invoice_number)
        return history[-1] if history else None

    def _mark_invoice_paid(self, case, attempt) -> None:
        from .obligations import ObligationRepository, obligation_key

        obligations = ObligationRepository(self.repository.path)
        key = obligation_key(attempt.invoice_number, case.vendor_name, attempt.currency or case.currency)
        obligation = obligations.get_obligation(key)
        if obligation is not None:
            obligations.set_obligation_state(obligation["obligation_id"], "PAID", attempt.amount)
        else:
            found, _created = obligations.ensure_obligation(
                invoice_number=attempt.invoice_number,
                vendor=case.vendor_name,
                customer=case.customer_name,
                currency=attempt.currency or case.currency,
                amount=attempt.amount,
                case_id=case.case_id,
            )
            obligations.set_obligation_state(found["obligation_id"], "PAID", attempt.amount)

    def _issue_receipt(self, case, attempt, recommendation, approval) -> PaymentReceipt:
        return PaymentReceipt(
            case_id=case.case_id,
            invoice_number=attempt.invoice_number,
            vendor_name=case.vendor_name,
            customer_name=case.customer_name,
            amount=attempt.amount,
            currency=attempt.currency,
            provider_reference=attempt.provider_reference,
            settlement_reference=attempt.settlement_reference,
            transaction_id=attempt.transaction_id,
            approved_by=approval.approved_by if approval else None,
            approved_at=approval.approved_at if approval else None,
            settled_at=attempt.settled_at,
            outcome="SETTLED",
            verification=[
                "Human approval verified against invoice, payee, amount and currency",
                "Payment Simulator submitted the instruction to the Mock Bank",
                "Agent 2 reconciled the instruction against the Mock Bank ledger",
                f"Agent 2 reported {recommendation.get('verdict')} with independent evidence",
                "Agent 5 issued this final confirmation",
            ],
        )
