"""Agent 4 - Payment Exception Specialist.

Investigates failed, rejected or unresolved payments and recommends a fix. It
diagnoses only from evidence it can actually see: the Mock Bank's own status and
reason code, the payment attempt, and Agent 2's reconciliation verdict.

Two rules shape everything here:

* **Never invent a reason.** If the bank did not send a code, Agent 4 says so and
  escalates. It does not guess "timeout" from a pending status.
* **Never retry.** A retry needs a fresh human approval, and Agent 4 records that
  requirement rather than acting on it.
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


# The bank's failure code -> what it means, what to do, and who acts next.
DIAGNOSES: dict[str, dict] = {
    "INSUFFICIENT_FUNDS": {
        "explanation": "The sender account did not have sufficient cleared funds when the payment was sent.",
        "customer": (
            "Your payment could not complete because the sending account did not have enough "
            "available funds at the time. No money left the account."
        ),
        "resolution": "Confirm the account balance, then obtain fresh human approval before any retry.",
        "risk": "medium",
        "escalate": "human",
    },
    "INVALID_ACCOUNT": {
        "explanation": "The recipient account details were rejected as invalid by the payment network.",
        "customer": (
            "Your payment could not complete because the recipient's account details were not "
            "accepted. No money left the account."
        ),
        "resolution": "Verify the payee's bank details with the vendor directly, then re-request approval.",
        "risk": "medium",
        "escalate": "human",
    },
    "ACCOUNT_CLOSED": {
        "explanation": "The recipient account is closed, so the transfer could not be completed.",
        "customer": (
            "Your payment could not complete because the recipient's account is closed. "
            "No money left the account."
        ),
        "resolution": "Obtain current account details from the payee before re-requesting approval.",
        "risk": "medium",
        "escalate": "human",
    },
    "LIMIT_EXCEEDED": {
        "explanation": "The transaction exceeds the sender's configured payment limit.",
        "customer": (
            "Your payment could not complete because it exceeds the limit set on the sending "
            "account. No money left the account."
        ),
        "resolution": "Split the payment or raise the limit through an authorised operator, then re-approve.",
        "risk": "high",
        "escalate": "human",
    },
    "REJECTED": {
        "explanation": "The receiving bank rejected the transaction.",
        "customer": (
            "Your payment was rejected by the receiving bank, so it did not go through. "
            "No money left the account."
        ),
        "resolution": "Contact the payee to confirm their details, then re-request approval.",
        "risk": "medium",
        "escalate": "human",
    },
    "RETURNED": {
        "explanation": "The payment was applied and later returned by the receiving bank.",
        "customer": (
            "Your payment was sent and then returned by the receiving bank. The funds should be "
            "back in the sending account."
        ),
        "resolution": "Reconcile with Agent 2 to confirm the funds came back before attempting anything else.",
        "risk": "high",
        "escalate": "agent_2",
    },
}

# Agent 2 verdicts that Agent 4 is responsible for investigating.
A2_HANDOFF_VERDICTS = {
    "PARTIAL_PAYMENT",
    "OVERPAYMENT",
    "AMOUNT_MISMATCH",
    "CURRENCY_MISMATCH",
    "DUPLICATE_BANK_TRANSACTION",
    "DUPLICATE_SETTLEMENT",
    "UNMATCHED_BANK_TRANSACTION",
    "MISSING_PAYMENT",
    "MISSING_SETTLEMENT",
    "NEEDS_HUMAN_REVIEW",
    "NEEDS_REVIEW",
    "RECONCILIATION_REQUIRED",
    "DISCREPANCY_DETECTED",
}


class PaymentExceptionSpecialist:
    """Agent 4: diagnose payment exceptions and recommend resolution."""

    agent = AgentId.agent_4

    def __init__(self, repository: WorkflowRepository, report_writer=None):
        self.repository = repository
        # Optional LLM writer. Without it Agent 4 still reports the evidence,
        # just with the deterministic prose.
        self.report_writer = report_writer

    # ------------------------------------------------------------------
    def investigate(
        self,
        case: WorkflowCase,
        attempt: PaymentAttempt | None = None,
        reported_reason: str | None = None,
        provider_response: dict | None = None,
        attempt_history: list[PaymentAttempt] | None = None,
        reconciliation: dict | None = None,
    ) -> AgentReport:
        started = utc_now()
        findings: list[Finding] = []

        if attempt is None:
            history = attempt_history or self._history(case)
            attempt = history[-1] if history else None

        bank_row = self._bank_row(attempt)
        reconciliation = reconciliation or case.reconciliation or {}

        status = (attempt.status if attempt else "UNKNOWN") or "UNKNOWN"
        bank_status = (bank_row or {}).get("status") or None

        # The bank's own code is the only reliable source of a failure reason.
        bank_code = (bank_row or {}).get("failure_code") or None
        attempt_code = (attempt.failure_code if attempt else None) or None
        failure_code = bank_code or attempt_code

        findings.append(
            Finding(
                code="exception_status",
                label="Transaction status",
                status="info",
                detail=(
                    f"GhostLedger records the attempt as {status}"
                    + (f"; the Mock Bank reports {bank_status}" if bank_status else
                       "; the Mock Bank holds no record for this instruction")
                    + "."
                ),
            )
        )

        # --- Money movement -------------------------------------------------
        if status == "FAILED" or (bank_status or "").upper() == "FAILED":
            findings.append(
                Finding(
                    code="not_settled",
                    label="No funds were transferred",
                    status="passed",
                    detail=(
                        "The Mock Bank did not credit the recipient. A failed instruction is not a "
                        "payment, so no invoice balance changes."
                    ),
                )
            )
        elif status == "PENDING":
            findings.append(
                Finding(
                    code="outcome_uncertain",
                    label="Settlement uncertain",
                    status="unverified",
                    detail=(
                        "The instruction has not settled. It must be checked with the bank before "
                        "any new attempt; a blind retry could pay twice."
                    ),
                )
            )
        elif status == "SUCCESS":
            findings.append(
                Finding(
                    code="settled",
                    label="Payment settled",
                    status="passed",
                    detail="The attempt settled; no exception handling is required.",
                )
            )

        # --- Diagnosis: bank code first, never guessed ----------------------
        diagnosis = DIAGNOSES.get(failure_code) if failure_code else None
        if diagnosis is None and failure_code:
            findings.append(
                Finding(
                    code="unknown_bank_code",
                    label="Unrecognised bank reason code",
                    status="unverified",
                    detail=(
                        f"The bank reported code {failure_code}, which Agent 4 does not recognise. "
                        "It will not guess a cause."
                    ),
                )
            )
        elif diagnosis is None and status != "SUCCESS":
            findings.append(
                Finding(
                    code="reason_unavailable",
                    label="Failure reason not available",
                    status="unverified",
                    detail=(
                        "Neither the Mock Bank nor the payment attempt recorded a reason code, so "
                        "the cause cannot be determined. Agent 4 will not invent one."
                    ),
                )
            )

        # --- Agent 2 handoff ------------------------------------------------
        verdict = (reconciliation or {}).get("verdict")
        if verdict in A2_HANDOFF_VERDICTS:
            findings.append(
                Finding(
                    code="reconciliation_discrepancy",
                    label="Reconciliation discrepancy handed to Agent 4",
                    status="failed",
                    detail=(
                        f"Agent 2 reported {verdict}. The expected amount and the amount actually "
                        "received do not reconcile, so the payment cannot be confirmed."
                    ),
                )
            )

        # --- Missing or incorrect details -----------------------------------
        missing = self._missing_details(case, attempt, bank_row)
        for item in missing:
            findings.append(
                Finding(
                    code="missing_detail",
                    label="Missing or incorrect payment detail",
                    status="unverified",
                    detail=item,
                )
            )

        risk, escalate, explanation, resolution, customer = self._conclude(
            status, failure_code, diagnosis, verdict, missing
        )

        findings.append(
            Finding(
                code="diagnosis",
                label="Diagnosis",
                status="passed" if status == "SUCCESS" else "unverified",
                detail=explanation,
            )
        )
        findings.append(
            Finding(
                code="customer_explanation",
                label="What to tell the customer",
                status="info",
                detail=customer,
            )
        )
        findings.append(
            Finding(
                code="recommended_resolution",
                label="Recommended next step",
                status="info",
                detail=resolution,
            )
        )

        retry_permitted = False
        findings.append(
            Finding(
                code="retry_policy",
                label="Retry not permitted automatically",
                status="info",
                detail=(
                    "Agent 4 never retries. A new attempt needs fresh human approval and proof "
                    "that the earlier attempt did not settle."
                ),
            )
        )

        # --- LLM investigation report --------------------------------------
        facts = self._build_facts(case, attempt, bank_row, reconciliation, status,
                                 failure_code, verdict, missing, risk, explanation,
                                 resolution, customer)
        llm_report = None
        if self.report_writer is not None:
            try:
                llm_report = self.report_writer.write(facts)
            except Exception:  # noqa: BLE001 - the report must never break the workflow
                llm_report = None
        if llm_report is not None:
            findings.append(
                Finding(
                    code="llm_investigation",
                    label="Investigation report",
                    status="info",
                    detail=llm_report.get("investigation_summary") or "",
                )
            )
            # The model's prose replaces the terse deterministic text.
            explanation = llm_report.get("root_cause") or explanation
            resolution = llm_report.get("recommended_next_steps") or resolution
            customer = llm_report.get("customer_explanation") or customer

        resolved = status == "SUCCESS" and not verdict
        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=(
                "No exception: the payment did not fail."
                if resolved
                else f"Payment exception investigated: {explanation}"
            ),
            findings=findings,
            recommended_next=AgentId.agent_2 if escalate == "agent_2" else None,
            next_action=resolution,
            requires_human=escalate == "human" or not resolved,
            blocking=True,
            resolved=resolved,
            data={
                "transaction_status": status,
                "bank_status": bank_status,
                "failure_code": failure_code,
                "failure_code_source": "mock_bank" if bank_code else ("attempt" if attempt_code else None),
                "failure_reason": (bank_row or {}).get("failure_reason")
                or (attempt.failure_reason if attempt else reported_reason),
                "transaction_id": attempt.transaction_id if attempt else None,
                "provider_reference": attempt.provider_reference if attempt else None,
                "provider_response": provider_response,
                "bank_evidence": bank_row,
                "reconciliation": reconciliation,
                "reconciliation_verdict": verdict,
                "diagnosis_code": failure_code or (verdict or "unknown"),
                "diagnosis": explanation,
                "customer_explanation": customer,
                "recommended_resolution": resolution,
                "risk_level": risk,
                "missing_details": missing,
                "retry_permitted": retry_permitted,
                "requires_fresh_approval": True,
                "retry_performed": False,
                "attempt_history": [a.model_dump(mode="json") for a in self._history(case)],
                "invoice_number": case.invoice_number,
                "invoice_amount": str(case.amount) if case.amount is not None else None,
                "currency": case.currency,
                "attempt": attempt.model_dump(mode="json") if attempt else None,
                "llm_report": llm_report,
                "customer_claims": facts.customer_claims,
                "unknown": facts.unknown,
            },
            started_at=started,
            completed_at=utc_now(),
        )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _customer_claims(self, case: WorkflowCase) -> dict:
        """What the uploaded PDF says. These are claims, never facts."""

        analysis = case.analysis or {}
        problem = analysis.get("detected_problem") or {}
        financial = analysis.get("financial_information") or {}
        claim = financial.get("payment_claim") or {}

        claims: dict = {}
        if problem.get("description"):
            claims["reported_problem"] = problem.get("description")
        if problem.get("source_evidence"):
            claims["quoted_evidence"] = problem.get("source_evidence")
        if case.invoice_number:
            claims["invoice_number"] = case.invoice_number
        amount = financial.get("amount_paid")
        if amount:
            claims["claimed_amount"] = f"{amount} {financial.get('currency') or ''}".strip()
        reference = claim.get("reference")
        if reference:
            claims["claimed_reference"] = reference
        if case.amount is not None:
            claims["invoice_total"] = f"{case.amount} {case.currency or ''}".strip()
        if case.vendor_name:
            claims["vendor"] = case.vendor_name
        if case.customer_name:
            claims["customer"] = case.customer_name
        if (problem.get("primary_problem") or "") == "payment_failure":
            claims["wants"] = "The customer reports that a payment failed."
        elif problem.get("primary_problem"):
            claims["wants"] = (
                f"The customer wants GhostLedger to resolve: "
                f"{str(problem.get('primary_problem')).replace('_', ' ')}."
            )
        return claims

    def _audit_trail(self, case: WorkflowCase) -> list[str]:
        """Relevant audit lines, oldest first."""

        keep = {"execution_started", "execution_result", "execution_blocked",
                "reconciliation_result", "exception_reported", "handoff",
                "payment_blocked", "receipt_issued"}
        return [
            f"{entry.actor.value}: {entry.action} - {entry.detail}"
            for entry in case.audit
            if entry.action in keep
        ][-8:]

    def _build_facts(
        self, case, attempt, bank_row, reconciliation, status, failure_code,
        verdict, missing, risk, explanation, resolution, customer,
    ):
        """Assemble the three evidence tiers for the LLM report."""

        from .agent4_llm import InvestigationFacts

        claims = self._customer_claims(case)

        bank_facts: dict = {
            "transaction_status": status,
            "invoice_number": case.invoice_number,
            "invoice_total": str(case.amount) if case.amount is not None else None,
            "currency": case.currency,
        }
        if attempt is not None:
            bank_facts.update(
                {
                    "payment_attempt_id": attempt.attempt_id,
                    "payment_reference": attempt.provider_reference,
                    "attempt_amount": str(attempt.amount) if attempt.amount is not None else None,
                    "attempt_status": attempt.status,
                    "attempt_payee": attempt.payee,
                    "attempted_at": attempt.attempted_at,
                }
            )
        has_failed_bank = False
        if bank_row is not None:
            has_failed_bank = (bank_row.get("status") or "").upper() in {"FAILED", "PENDING", "PARTIAL"}
            bank_facts.update(
                {
                    "bank_transaction_id": bank_row.get("entry_id"),
                    "bank_status": bank_row.get("status"),
                    "bank_failure_code": bank_row.get("failure_code"),
                    "bank_failure_reason": bank_row.get("failure_reason"),
                    "bank_amount": bank_row.get("amount"),
                    "bank_received_amount": bank_row.get("received_amount"),
                    "bank_transaction_date": bank_row.get("transaction_date"),
                    "bank_settlement_date": bank_row.get("settlement_date"),
                    "bank_scenario": bank_row.get("scenario"),
                    "bank_sender_account": bank_row.get("sender_account"),
                    "bank_receiver_account": bank_row.get("receiver_account"),
                }
            )
        bank_facts["audit_trail"] = self._audit_trail(case)
        if reconciliation:
            bank_facts["reconciliation_verdict"] = reconciliation.get("verdict")
            bank_facts["reconciliation_summary"] = reconciliation.get("summary")

        unknown: list[str] = []
        if not failure_code:
            unknown.append("The Mock Bank reported no failure reason code, so the root cause is unknown.")
        if bank_row is None:
            unknown.append("The Mock Bank holds no transaction for this payment, so the claim cannot be verified.")
        if attempt is None:
            unknown.append("No payment attempt is recorded for this invoice.")
        elif not attempt.provider_reference:
            unknown.append("The payment has no provider reference, so it cannot be traced at the bank.")
        unknown.extend(missing)

        settlement = (bank_row or {}).get("status")
        if settlement:
            settlement = str(settlement).upper()

        return InvestigationFacts(
            customer_claims=claims,
            bank_facts=bank_facts,
            unknown=unknown,
            failure_reason_code=failure_code,
            settlement_status=settlement,
            has_failed_bank_transaction=has_failed_bank,
            retry_permitted=False,
            requires_human=(status != "SUCCESS") or bool(verdict),
        )

    def _history(self, case: WorkflowCase) -> list[PaymentAttempt]:
        try:
            from .payment_store import PaymentRecordStore

            return PaymentRecordStore(self.repository.path).list_for_invoice(case.invoice_number)
        except Exception:  # noqa: BLE001
            return self.repository.list_attempts(case.case_id)

    def _bank_row(self, attempt: PaymentAttempt | None) -> dict | None:
        """The Mock Bank's own record for this attempt."""

        if attempt is None:
            return None
        try:
            from .mock_bank import MockBank

            bank = MockBank(self.repository.path)
            rows = bank.ledger(payment_reference=attempt.provider_reference)
            if rows:
                return rows[-1]
            if attempt.invoice_number:
                rows = bank.ledger(invoice_number=attempt.invoice_number)
                if rows:
                    return rows[-1]
        except Exception:  # noqa: BLE001
            return None
        return None

    def _missing_details(self, case, attempt, bank_row) -> list[str]:
        """Missing or incorrect fields that would block a clean retry."""

        problems: list[str] = []
        if attempt is None:
            problems.append("There is no payment attempt on record for this invoice.")
        if not case.invoice_number:
            problems.append("The case has no invoice number.")
        if bank_row is None:
            problems.append("The Mock Bank holds no instruction for this payment.")
        elif not bank_row.get("received_account") and not bank_row.get("receiver_account"):
            problems.append("The recipient account is not recorded on the bank instruction.")
        if attempt is not None:
            if not attempt.provider_reference:
                problems.append("The payment has no provider reference, so it cannot be traced at the bank.")
            if attempt.amount is None:
                problems.append("The payment amount is missing.")
            if not attempt.payee:
                problems.append("The payee is missing.")
        return problems

    def _conclude(self, status, failure_code, diagnosis, verdict, missing):
        """Return (risk, escalate, explanation, resolution, customer_text)."""

        if status == "SUCCESS" and not verdict:
            return (
                "low",
                None,
                "The payment settled and no exception was found.",
                "No action required.",
                "Your payment was received successfully.",
            )

        if diagnosis:
            return (
                diagnosis["risk"],
                diagnosis["escalate"],
                diagnosis["explanation"],
                diagnosis["resolution"],
                diagnosis["customer"],
            )

        if verdict:
            return (
                "high",
                "human",
                f"Agent 2 could not reconcile the payment ({verdict}).",
                "A human must review Agent 2's findings and decide whether the payment should be "
                "recognised or re-requested.",
                (
                    "We could not confirm your payment against our bank records. Someone will "
                    "review it before any money is moved again."
                ),
            )

        if status == "PENDING":
            return (
                "high",
                "human",
                "The payment is unresolved and the bank has not reported a failure reason.",
                "Check the existing instruction with the bank before considering anything else.",
                (
                    "Your payment is still being processed. We will check it before taking any "
                    "further action."
                ),
            )

        return (
            "high",
            "human",
            "The available records do not explain the failure.",
            "A human must inspect the bank's records before any further action.",
            (
                "We could not determine why this payment did not complete. Someone will review it "
                "before anything else happens."
            ),
        )
