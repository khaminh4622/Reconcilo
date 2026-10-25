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

import re
from decimal import Decimal

from .repository import WorkflowRepository
from .schemas import (
    AGENT_LABELS,
    AgentId,
    AgentReport,
    CustomerResponse,
    Finding,
    PaymentReceipt,
    WorkflowCase,
    utc_now,
)
from .payment_store import ReceiptStore


# Agent 2 verdicts that mean "the bank confirms this settlement".
VERIFIED_VERDICTS = {"MATCHED", "SETTLEMENT_VERIFIED"}

# Wording that tells a customer money arrived. A model may phrase the answer,
# but never with one of these unless the settlement was verified.
_SETTLEMENT_CLAIM = re.compile(
    r"\b(settled|has been paid|was paid|is paid|payment (is|was|has been) (confirmed|completed|received)"
    r"|successfully paid|money (has )?arrived)\b",
    re.IGNORECASE,
)


def phrasing_is_safe(phrased: dict, *, settled: bool) -> bool:
    """False when a model's wording claims a settlement that was not verified."""

    if settled:
        return True
    text = " ".join([str(phrased.get("answer") or ""), *map(str, phrased.get("next_steps") or [])])
    return not _SETTLEMENT_CLAIM.search(text)


class FinalConfirmationAgent:
    """Agent 5: confirm the outcome and answer the customer."""

    agent = AgentId.agent_5

    def __init__(self, repository: WorkflowRepository, response_writer=None, receipts=None, phraser=None):
        self.repository = repository
        self.receipts = receipts or ReceiptStore(repository.path)
        # Optional LLM writer for the customer-facing prose.
        self.response_writer = response_writer
        # Optional LLM phraser for ``customer_answer``: (case, facts) -> dict.
        self.phraser = phraser

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

    # ==================================================================
    # Rejected by the bank side, or stopped for a person
    # ==================================================================
    def report_rejection(self, case: WorkflowCase, reason: str | None = None) -> AgentReport:
        """Close a payment request the approver rejected. Nothing was paid."""

        started = utc_now()
        response = self._customer_response(case, status="REJECTED", verdict="REJECTED")
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=f"Final outcome: {case.outcome or 'the payment was not approved.'} No payment was sent.",
            findings=[
                Finding(
                    code="not_approved",
                    label="Payment not approved",
                    status="passed",
                    detail="The approver rejected the request, so no payment instruction was sent.",
                )
            ],
            next_action="No payment will be made unless a new request is approved.",
            requires_human=False,
            blocking=True,
            resolved=False,
            data={
                "final_status": "REJECTED",
                "confirmed": False,
                # The approver's own words only; never their identity.
                "reason": reason,
                "customer_response": response,
            },
            started_at=started,
            completed_at=utc_now(),
        )

    def explain_review(self, case: WorkflowCase, reason: str) -> AgentReport:
        """Close a case a person must look at, telling the customer what to do."""

        started = utc_now()
        response = self._customer_response(case, status="REQUIRES_REVIEW", verdict=None)
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=f"A person must review {case.invoice_number or 'this case'}: {reason}",
            findings=[
                Finding(code="review_reason", label="Why a person is needed", status="info", detail=reason)
            ],
            # Left empty on purpose: the reviewer's next step comes from the
            # specialists and the case status, not from this explanation.
            next_action=None,
            requires_human=True,
            blocking=True,
            resolved=False,
            data={"final_status": "REQUIRES_REVIEW", "confirmed": False, "customer_response": response},
            started_at=started,
            completed_at=utc_now(),
        )

    # ==================================================================
    # The plain-language answer stored on the case
    # ==================================================================
    def customer_answer(self, case: WorkflowCase) -> CustomerResponse:
        """Build the customer's answer from the case's verified facts.

        Deterministic data decides the outcome and whether money settled. An
        optional phraser may reword ``answer`` and ``next_steps``; a rewording
        that claims settlement when nothing settled is discarded.
        """

        outcome, headline, answer, steps, missing = self._answer_facts(case)
        settled = outcome in {"PAID", "PAYMENT_FOUND"}
        response = CustomerResponse(
            case_status=case.status.value,
            outcome=outcome,
            headline=headline,
            answer=answer,
            next_steps=steps,
            payment_settled=settled,
            missing_information=missing,
        )
        if self.phraser is not None:
            try:
                phrased = self.phraser(case, response.model_dump(mode="json"))
            except Exception:  # noqa: BLE001 - phrasing must never break a case
                phrased = None
            if phrased and phrasing_is_safe(phrased, settled=settled):
                response.answer = str(phrased.get("answer") or response.answer)
                if phrased.get("next_steps"):
                    response.next_steps = [str(step) for step in phrased["next_steps"]]
                response.generated_by = "llm"
        return response

    def _answer_facts(self, case: WorkflowCase) -> tuple[str, str, str, list[str], list[str]]:
        status = case.status.value
        invoice = f"invoice {case.invoice_number}" if case.invoice_number else "your request"
        amount = (
            f"{case.amount:,.2f} {case.currency or ''}".strip() if case.amount is not None else None
        )
        actions = {entry.action for entry in case.audit}
        a1 = self._latest(case, AgentId.agent_1)
        a4 = self._latest(case, AgentId.agent_4)
        do_not_pay_again = "Please do not send the payment again while this is being checked."

        if status == "COMPLETED":
            if case.receipt is not None and case.payment is not None and case.payment.verified:
                return (
                    "PAID",
                    "Payment confirmed",
                    f"Your payment{' of ' + amount if amount else ''} for {invoice}"
                    f"{' to ' + case.vendor_name if case.vendor_name else ''} is confirmed: the bank "
                    f"settled it and we matched it to your invoice. Receipt {case.receipt.receipt_id[:8]}.",
                    ["No action is needed.", "Keep the receipt for your records."],
                    [],
                )
            if self._path_b_verified(case):
                return (
                    "PAYMENT_FOUND",
                    "We found your payment",
                    f"The bank records show a settled payment for {invoice}, and we matched it to the invoice.",
                    ["No further action is needed on your side."],
                    [],
                )
            return (
                "RESOLVED",
                "Your request has been handled",
                f"We have finished checking {invoice}. No payment was made or confirmed as part of this request.",
                ["No action is needed unless you disagree with this answer."],
                [],
            )

        if status == "PENDING":
            return (
                "PAYMENT_PENDING",
                "Payment sent, not yet confirmed",
                f"Your payment for {invoice} was sent to the bank but has not settled yet, so we cannot "
                "confirm it.",
                ["No action is needed now; we will check again once the bank settles it.", do_not_pay_again],
                [],
            )

        if status == "FAILED" and "rejected" in actions:
            a5 = self._latest(case, AgentId.agent_5)
            reason = str((a5.data.get("reason") if a5 else None) or "").strip()
            return (
                "REJECTED",
                "Payment not approved",
                f"Your payment request for {invoice} was not approved by the bank team, so no money was sent."
                + (f" Reason given: {reason.rstrip('.')}." if reason else ""),
                [
                    "If you think this is a mistake, contact support or submit a corrected invoice.",
                ],
                [],
            )

        if case.payment is not None and case.payment.status == "FAILED":
            data = (a4.data if a4 else {}) or {}
            code = data.get("failure_code") or case.payment.failure_code
            reason = data.get("failure_reason") or case.payment.failure_reason
            cause = (
                f" The bank's reason: {reason} ({code})." if code and reason
                else f" The bank reported {code}." if code
                else " The bank did not give a reason, so the cause is not known yet."
            )
            steps = [data["recommended_resolution"]] if data.get("recommended_resolution") else []
            steps += [
                "Nothing will be retried automatically; a new payment needs a fresh approval.",
                do_not_pay_again,
            ]
            return (
                "PAYMENT_FAILED",
                "Payment did not go through",
                f"Your payment for {invoice} did not go through, so the invoice has not been paid." + cause,
                steps,
                [],
            )

        if "blocked_before_approval" in actions and a1 is not None:
            investigation = a1.data.get("investigation") or {}
            previous = investigation.get("previous_payment_status")
            if previous is None and (investigation.get("payment_analysis") or {}).get("verified_settlements"):
                previous = "SETTLEMENT_ON_RECORD"
            why = {
                "SETTLEMENT_ON_RECORD": "the bank already shows a settled payment that matches it",
                "ALREADY_PAID": "our records show it has already been paid",
                "DUPLICATE_PAYMENT_DETECTED": "it appears to have been paid more than once already",
                "PAYMENT_PENDING": "an earlier payment for it is still being processed",
                "PAYMENT_FAILED": "an earlier payment attempt failed and must be checked first",
                "PAYMENT_STATUS_UNKNOWN": "we cannot yet tell whether an earlier payment went through",
            }.get(previous or "", "it looks like a duplicate of an invoice already on record")
            return (
                "BLOCKED_DUPLICATE",
                "Payment held for review",
                f"We did not pay {invoice} because {why}. A member of the bank team will review it.",
                [
                    do_not_pay_again,
                    "If this really is a different invoice, send the vendor's invoice so we can compare them.",
                ],
                [],
            )

        if case.intent.value in {"duplicate_invoice", "duplicate_payment"} and a1 is not None:
            finding = {
                "CONFIRMED_DUPLICATE_PAYMENT": "Bank records show it was paid more than once.",
                "SUSPECTED_DUPLICATE_PAYMENT": (
                    "There are signs it may have been paid more than once, but the bank records do not "
                    "confirm a second payment."
                ),
                "CONFIRMED_DUPLICATE_INVOICE": "It duplicates an invoice already on record.",
                "POSSIBLE_DUPLICATE": "We found a similar invoice, but cannot confirm it is a duplicate.",
                "NO_DUPLICATE_FOUND": "We found no evidence that it was billed or paid twice.",
                "REQUIRES_BANK_VERIFICATION": "The bank records do not yet show how many payments settled.",
            }.get(str(a1.data.get("status") or ""), "We could not reach a firm conclusion from the records.")
            if case.intent.value == "duplicate_payment":
                # Counted from settled bank-ledger rows only, never from claims.
                a2 = self._latest(case, AgentId.agent_2)
                analysis = (a1.data.get("investigation") or {}).get("payment_analysis") or {}
                settled = int(
                    (a2.data.get("settlement_count") if a2 else None)
                    or analysis.get("verified_settlements")
                    or 0
                )
                finding += (
                    f" The bank records show {settled} settled payment{'s' if settled != 1 else ''} for it."
                    if settled
                    else " The bank records show no settled payment for it."
                )
            return (
                "DUPLICATE_CHECKED",
                "Duplicate check finished",
                f"We checked {invoice} for duplicate billing and payments. {finding}",
                [
                    "A member of our team will review the records; refunds are never issued automatically.",
                    "If you have bank statements showing the payments, send their references and dates.",
                ],
                [],
            )

        if case.intent.value == "payment_failure" or (a4 is not None and case.payment is None):
            data = (a4.data if a4 else {}) or {}
            diagnosis = data.get("customer_explanation") or data.get("diagnosis")
            steps = [data["recommended_resolution"]] if data.get("recommended_resolution") else []
            steps += ["Nothing will be retried automatically.", do_not_pay_again]
            return (
                "FAILURE_CHECKED",
                "We looked into the failed payment",
                f"We investigated the failed payment for {invoice}."
                + (f" {diagnosis}" if diagnosis else " The records do not show why it failed."),
                steps,
                [],
            )

        if case.intent.value in {"payment_status_dispute", "bank_reconciliation", "PAYMENT_STATUS_CHECK"}:
            a2 = self._latest(case, AgentId.agent_2)
            if a2 is not None and a2.resolved and a2.data.get("matched") is False:
                # Agent 2 had no claimed payment to look for, so nothing was searched.
                return (
                    "PAYMENT_DETAILS_NEEDED",
                    "We need the payment details",
                    f"We could not check {invoice} against the bank records because the request does not "
                    "say how much was paid.",
                    [
                        "Send your bank's transfer confirmation (reference, date and amount) by upload or message.",
                        do_not_pay_again,
                    ],
                    ["amount paid", "payment reference"],
                )
            return (
                "PAYMENT_NOT_FOUND",
                "Payment not found yet",
                f"We could not find a settled payment for {invoice} in the bank records, so we cannot "
                "confirm it was paid.",
                [
                    "Send your bank's transfer confirmation (reference, date and amount) by upload or message.",
                    do_not_pay_again,
                ],
                [],
            )

        missing = self._missing_for_customer(case)
        reason = case.pending_reason or case.outcome or ""
        steps = [f"Tell us the {item}." for item in missing] or [
            "Upload the invoice as a PDF, or send a message with the invoice number, vendor and amount."
        ]
        steps.append("A member of our team will review your request.")
        return (
            "NEEDS_REVIEW",
            "A person will review your request",
            "We could not process your request automatically."
            + (f" {reason.rstrip('.')}." if reason else ""),
            steps,
            missing,
        )

    @staticmethod
    def _latest(case: WorkflowCase, agent: AgentId) -> AgentReport | None:
        return next((r for r in reversed(case.reports) if r.agent == agent), None)

    def _path_b_verified(self, case: WorkflowCase) -> bool:
        a2 = self._latest(case, AgentId.agent_2)
        return bool(
            a2 and (
                a2.data.get("matched") is True
                or (a2.data.get("reconciliation") or {}).get("outcome") == "SETTLEMENT_VERIFIED"
            )
        )

    @staticmethod
    def _missing_for_customer(case: WorkflowCase) -> list[str]:
        labels = {
            "document_id": "invoice number",
            "invoice_number": "invoice number",
            "total_amount": "amount",
            "currency": "currency",
            "customer.customer_name": "customer name",
            "vendor.vendor_name": "vendor (who the invoice is from)",
        }
        found: list[str] = []
        for raw in (case.analysis or {}).get("missing_information") or []:
            text = str(raw)
            label = next((v for k, v in labels.items() if k in text), None)
            if label and label not in found:
                found.append(label)
        if not case.invoice_number and "invoice number" not in found:
            found.insert(0, "invoice number")
        return found

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
