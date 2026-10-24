"""Central workflow orchestrator.

Receives Agent 3's structured analysis, decides which workflow applies, dispatches
to the specialist agents, enforces the human-approval gate, tracks each agent's
status, and records a complete audit trail.

The agents are independent modules; none of them calls another directly. All
coordination happens here so the path a case takes is always explainable.
"""

from __future__ import annotations

from decimal import Decimal

from agent3.schemas import (
    AnalysisResult,
    FinancialStatus,
    PaymentStatus,
    PrimaryProblem,
    ProblemType,
)

from .agent1_duplicates import DuplicateInvestigator
from .agent2_reconciliation import ReconciliationSpecialist
from .agent4_exceptions import PaymentExceptionSpecialist
from .agent5_execution import PaymentExecutionAgent
from .repository import WorkflowRepository
from .schemas import (
    AGENT_LABELS,
    AgentId,
    AgentReport,
    AgentRun,
    AgentRunStatus,
    ApprovalRecord,
    CaseStatus,
    PaymentAttempt,
    Intent,
    RoutingDecisionView,
    WorkflowCase,
    WorkflowType,
    utc_now,
)


class WorkflowError(RuntimeError):
    """A workflow action was requested that the current state does not allow."""


# Intent -> (primary agent, supporting agent). Mirrors the approved routing table.
INTENT_ROUTES: dict[Intent, tuple[AgentId, AgentId | None]] = {
    Intent.new_payment_request: (AgentId.human_approval, None),
    Intent.duplicate_invoice: (AgentId.agent_1, None),
    Intent.duplicate_payment: (AgentId.agent_1, AgentId.agent_2),
    Intent.bank_reconciliation: (AgentId.agent_2, None),
    Intent.payment_status_dispute: (AgentId.agent_2, None),
    Intent.payment_failure: (AgentId.agent_4, None),
    Intent.unknown: (AgentId.human_review, None),
}

# Agents that end a case unless they hand off.
TERMINAL_AGENT_OUTCOMES = {AgentId.human_review}


def _intent_from_analysis(analysis: AnalysisResult) -> Intent:
    """Map Agent 3's classification onto the orchestrator's intent vocabulary."""

    problem = analysis.detected_problem
    primary = problem.primary_problem
    if primary == PrimaryProblem.duplicate_payment:
        # A duplicate is only a *new payment request* if nothing has been paid.
        return Intent.duplicate_payment
    if primary == PrimaryProblem.duplicate_invoice:
        return Intent.duplicate_invoice
    if primary == PrimaryProblem.payment_failure:
        return Intent.payment_failure
    if primary == PrimaryProblem.payment_request:
        return Intent.new_payment_request
    if primary == PrimaryProblem.payment_status_dispute:
        return Intent.payment_status_dispute
    if primary == PrimaryProblem.bank_reconciliation:
        paid = analysis.financial_information.amount_paid
        if paid is not None and paid > 0:
            return Intent.payment_status_dispute
        return Intent.bank_reconciliation
    # Nothing reported: only a clean invoice can start a new payment.
    if problem.type == ProblemType.none_identified and not analysis.requires_review:
        return Intent.new_payment_request
    # A document that reports a payment but still shows a balance due is a
    # payment-status dispute even without an explicit customer complaint.
    paid = analysis.financial_information.amount_paid
    if problem.type == ProblemType.payment_status_discrepancy and paid is not None and paid > 0:
        return Intent.payment_status_dispute
    return Intent.unknown


def _blocking_reasons(analysis: AnalysisResult) -> list[str]:
    """Issues that stop a new invoice from being payable.

    Arithmetic failures, missing required fields, an unclassified document and
    explicit contradictions all block. A merely unverified *payment* does not,
    because an unpaid invoice is expected to have none.
    """

    reasons: list[str] = []
    validation = analysis.validation
    if validation.financial_consistency == FinancialStatus.failed:
        reasons.append("financial consistency failed")
    if validation.missing_fields:
        reasons.append("missing required fields: " + ", ".join(validation.missing_fields))
    if validation.customer_verification == "mismatch":
        reasons.append("customer details contradict existing records")
    if analysis.contradictions:
        reasons.append("the document contains contradictions")
    if validation.status == "extraction_failed":
        reasons.append("document extraction failed")
    failed_checks = [
        check.code for check in validation.validation_findings
        if check.status == "failed" and check.code != "payment_verification"
    ]
    if failed_checks:
        reasons.append("failed checks: " + ", ".join(sorted(set(failed_checks))))
    return reasons


class WorkflowOrchestrator:
    def __init__(self, repository: WorkflowRepository):
        self.repository = repository
        self.duplicates = DuplicateInvestigator(repository)
        self.reconciliation = ReconciliationSpecialist(repository)
        self.exceptions = PaymentExceptionSpecialist(repository)
        self.execution = PaymentExecutionAgent(repository)

    # ------------------------------------------------------------------
    # Case lifecycle
    # ------------------------------------------------------------------
    def open_case(self, analysis: AnalysisResult, filename: str = "document.pdf") -> WorkflowCase:
        """Create a case from Agent 3's analysis and route it immediately."""

        intent = _intent_from_analysis(analysis)
        case = WorkflowCase(
            status=CaseStatus.received,
            intent=intent,
            customer_name=analysis.customer.customer_name or analysis.customer.company_name,
            vendor_name=analysis.vendor.vendor_name,
            invoice_number=analysis.document_id,
            amount=analysis.financial_information.total_amount,
            currency=analysis.currency or analysis.financial_information.currency,
            analysis=analysis.model_dump(mode="json"),
        )
        case.add_audit(AgentId.orchestrator, "case_opened", f"Case opened for {filename}.")
        self._plan_runs(case)
        self.repository.save_case(case)
        return case

    def _plan_runs(self, case: WorkflowCase) -> None:
        """Pre-build the visual workflow so the UI can show what is coming."""

        intent = case.intent
        primary, supporting = INTENT_ROUTES[intent]
        order: list[AgentId] = [AgentId.agent_3]
        if intent == Intent.new_payment_request:
            order += [AgentId.human_approval, AgentId.agent_5]
        elif primary == AgentId.human_review:
            order += [AgentId.human_review]
        else:
            order.append(primary)
            if supporting:
                order.append(supporting)
        case.agent_runs = [
            AgentRun(agent=agent, agent_name=AGENT_LABELS[agent], status=AgentRunStatus.waiting, order=index)
            for index, agent in enumerate(order)
        ]
        # Agent 3 has already run by the time a case exists.
        agent3 = case.run_for(AgentId.agent_3)
        if agent3:
            agent3.status = AgentRunStatus.completed
            agent3.summary = "Document analysed and validated."
            agent3.completed_at = utc_now()

    def route(self, case: WorkflowCase) -> WorkflowCase:
        """Validate Agent 3's recommendation and route the case."""

        analysis = AnalysisResult.model_validate(case.analysis) if case.analysis else None
        if analysis is None:
            raise WorkflowError("Cannot route a case without an Agent 3 analysis.")

        primary, supporting = INTENT_ROUTES[case.intent]
        case.status = CaseStatus.analyzing
        case.add_audit(AgentId.agent_3, "analysis_complete", "Agent 3 finished document analysis.")

        # Deterministic guard: a new payment may only proceed when the financial
        # facts themselves are sound. Payment provenance is deliberately NOT part
        # of this test - an unpaid invoice always has no verified payment, and
        # requiring one would make every new payment request impossible. The
        # human approval step is what covers the payment risk.
        if case.intent == Intent.new_payment_request:
            blocking_reasons = _blocking_reasons(analysis)
            if blocking_reasons:
                case.intent = Intent.unknown
                primary, supporting = AgentId.human_review, None
                case.add_audit(
                    AgentId.orchestrator,
                    "routing_overridden",
                    "Agent 3 found unresolved issues ("
                    + "; ".join(blocking_reasons)
                    + "), so the payment request was routed to human review.",
                    CaseStatus.requires_review.value,
                )

        case.workflow = (
            WorkflowType.new_payment
            if primary in {AgentId.human_approval, AgentId.agent_5}
            else WorkflowType.human_review
            if primary == AgentId.human_review
            else WorkflowType.existing_problem
        )
        case.routing = RoutingDecisionView(
            primary_agent=primary,
            supporting_agent=supporting,
            intent=case.intent,
            reason=self._routing_reason(case, analysis),
            priority="high" if analysis.routing.priority == "high" else "normal",
            validation_status=analysis.validation.financial_consistency.value,
            blocking=bool(analysis.routing.blocking),
            required_next_action=self._next_action(primary, analysis),
            requires_human_approval=primary == AgentId.human_approval,
        )
        # Rebuild the planned path when routing changed the destination, so the
        # visualisation never shows an agent that will not actually run.
        planned = {run.agent for run in case.agent_runs}
        if primary not in planned:
            case.agent_runs = [
                run for run in case.agent_runs
                if run.agent in {AgentId.agent_3, AgentId.human_review} or run.status != AgentRunStatus.waiting
            ]
            order: list[AgentId] = [AgentId.agent_3]
            if primary == AgentId.human_approval:
                order += [AgentId.human_approval, AgentId.agent_5]
            elif primary == AgentId.human_review:
                order += [AgentId.human_review]
            else:
                order.append(primary)
                if supporting:
                    order.append(supporting)
            existing = {run.agent: run for run in case.agent_runs}
            case.agent_runs = []
            for index, agent in enumerate(order):
                run = existing.get(agent) or AgentRun(agent=agent, agent_name=AGENT_LABELS[agent])
                run.order = index
                if agent == AgentId.agent_3:
                    run.status = AgentRunStatus.completed
                case.agent_runs.append(run)

        case.status = CaseStatus.routed
        case.add_audit(
            AgentId.orchestrator,
            "routed",
            f"Routed to {AGENT_LABELS[primary]}.",
        )
        self.repository.save_case(case)

        if primary == AgentId.human_approval:
            return self._await_approval(case)
        if primary == AgentId.human_review:
            return self._human_review(case, "The problem could not be classified, or validation failed.")
        return self._dispatch(case, primary, analysis)

    def _routing_reason(self, case: WorkflowCase, analysis: AnalysisResult) -> str:
        base = analysis.routing.reason or ""
        if case.intent == Intent.payment_status_dispute:
            return base or "The customer reports a payment that the document still shows as unpaid."
        if case.intent == Intent.new_payment_request:
            return (
                "The document is a valid, internally consistent invoice with no problem reported, "
                "so it is treated as a request to pay it."
            )
        return base or "Routed from the customer's reported problem."

    @staticmethod
    def _next_action(agent: AgentId, analysis: AnalysisResult) -> str:
        if agent == AgentId.human_approval:
            return "A human must approve the invoice, payee and amount before any payment is attempted."
        if agent == AgentId.agent_1:
            return "Investigate whether the invoice or payment is a duplicate."
        if agent == AgentId.agent_2:
            return "Reconcile the reported payment against bank records."
        if agent == AgentId.agent_4:
            return "Investigate the payment failure and recommend a resolution."
        return "A human must clarify the request."

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def _dispatch(self, case: WorkflowCase, agent: AgentId, analysis: AnalysisResult) -> WorkflowCase:
        case.status = CaseStatus.investigating
        return self.run_agent(case, agent, analysis)

    def run_agent(
        self, case: WorkflowCase, agent: AgentId, analysis: AnalysisResult | None = None
    ) -> WorkflowCase:
        """Execute one specialist agent and record its report."""

        analysis = analysis or (AnalysisResult.model_validate(case.analysis) if case.analysis else None)
        if analysis is None:
            raise WorkflowError("Case has no analysis to investigate.")

        run = case.run_for(agent) or AgentRun(agent=agent, agent_name=AGENT_LABELS[agent])
        run.status = AgentRunStatus.running
        run.started_at = utc_now()
        case.add_audit(agent, "agent_started", f"{AGENT_LABELS[agent]} started.")

        if agent == AgentId.agent_1:
            report = self.duplicates.investigate(case, analysis)
        elif agent == AgentId.agent_2:
            report = self.reconciliation.reconcile(case, analysis)
        elif agent == AgentId.agent_4:
            report = self.exceptions.investigate(case)
        else:
            raise WorkflowError(f"{agent.value} cannot be dispatched this way.")

        case.reports.append(report)
        run.status = (
            AgentRunStatus.completed
            if report.resolved
            else AgentRunStatus.requires_human
            if report.requires_human
            else AgentRunStatus.blocked
        )
        run.summary = report.summary
        run.completed_at = utc_now()
        case.add_audit(agent, "agent_completed", report.summary)

        return self._after_agent(case, report)

    def _after_agent(self, case: WorkflowCase, report: AgentReport) -> WorkflowCase:
        """Decide what happens next based on the agent's structured report."""

        # Hand off to a supporting agent exactly once, when it adds value.
        if report.recommended_next and report.recommended_next != report.agent:
            next_agent = report.recommended_next
            next_run = case.run_for(next_agent)
            if next_run and next_run.status == AgentRunStatus.waiting:
                case.status = CaseStatus.investigating
                case.add_audit(
                    AgentId.orchestrator,
                    "handoff",
                    f"{AGENT_LABELS[report.agent]} handed the case to {AGENT_LABELS[next_agent]}.",
                )
                self.repository.save_case(case)
                analysis = AnalysisResult.model_validate(case.analysis) if case.analysis else None
                return self.run_agent(case, next_agent, analysis)

        if report.resolved and not report.requires_human:
            case.status = CaseStatus.completed
            case.outcome = report.summary
            case.requires_human = False
            case.add_audit(AgentId.orchestrator, "case_completed", report.summary, CaseStatus.completed.value)
        else:
            case.status = CaseStatus.requires_review
            case.requires_human = True
            case.outcome = report.summary
            review_run = case.run_for(AgentId.human_review)
            if review_run is None:
                case.agent_runs.append(
                    AgentRun(
                        agent=AgentId.human_review,
                        agent_name=AGENT_LABELS[AgentId.human_review],
                        status=AgentRunStatus.requires_human,
                        order=len(case.agent_runs),
                        summary="Human review required.",
                    )
                )
            else:
                review_run.status = AgentRunStatus.requires_human
                review_run.summary = "Human review required."
            case.add_audit(
                AgentId.orchestrator,
                "human_review_required",
                report.next_action or report.summary,
                CaseStatus.requires_review.value,
            )
        self.repository.save_case(case)
        return case

    # ------------------------------------------------------------------
    # Human steps
    # ------------------------------------------------------------------
    def _human_review(self, case: WorkflowCase, reason: str) -> WorkflowCase:
        case.status = CaseStatus.requires_review
        case.requires_human = True
        case.outcome = reason
        run = case.run_for(AgentId.human_review)
        if run:
            run.status = AgentRunStatus.requires_human
            run.summary = reason
            run.started_at = run.started_at or utc_now()
        case.add_audit(AgentId.orchestrator, "human_review_required", reason, CaseStatus.requires_review.value)
        self.repository.save_case(case)
        return case

    def _await_approval(self, case: WorkflowCase) -> WorkflowCase:
        case.status = CaseStatus.awaiting_approval
        case.requires_human = True
        case.outcome = "Waiting for a human to approve the payment."
        run = case.run_for(AgentId.human_approval)
        if run:
            run.status = AgentRunStatus.requires_human
            run.summary = "Waiting for human approval."
            run.started_at = utc_now()
        case.add_audit(
            AgentId.orchestrator,
            "awaiting_approval",
            "Agent 3 validated the invoice. A human must approve before Agent 5 executes.",
            CaseStatus.awaiting_approval.value,
        )
        self.repository.save_case(case)
        return case

    def approve(
        self,
        case_id: str,
        approved_by: str,
        note: str | None = None,
        simulate: str = "SUCCESS",
        failure_reason: str | None = None,
    ) -> WorkflowCase:
        """Record a human approval bound to the case's exact payment terms."""

        case = self.repository.get_case(case_id)
        if case is None:
            raise WorkflowError("Case not found.")
        if case.status != CaseStatus.awaiting_approval:
            raise WorkflowError(
                f"Case is {case.status.value}; only a case awaiting approval can be approved."
            )

        approval = ApprovalRecord(
            approved_by=approved_by,
            invoice_number=case.invoice_number,
            payee=case.vendor_name,
            amount=case.amount,
            currency=case.currency,
            note=note,
        )
        case.approval = approval
        case.status = CaseStatus.approved
        case.requires_human = False
        case.outcome = f"Approved by {approved_by}."
        run = case.run_for(AgentId.human_approval)
        if run:
            run.status = AgentRunStatus.completed
            run.summary = f"Approved by {approved_by}."
            run.completed_at = utc_now()
        case.add_audit(
            AgentId.human_approval,
            "approved",
            f"{approved_by} approved {case.amount} {case.currency} to {case.vendor_name}.",
            CaseStatus.approved.value,
        )
        self.repository.save_approval(case.case_id, approval)
        self.repository.save_case(case)
        return self.execute_payment(case_id, simulate=simulate, failure_reason=failure_reason)

    def reject(self, case_id: str, rejected_by: str, reason: str | None = None) -> WorkflowCase:
        case = self.repository.get_case(case_id)
        if case is None:
            raise WorkflowError("Case not found.")
        if case.status != CaseStatus.awaiting_approval:
            raise WorkflowError(f"Case is {case.status.value}; it is not awaiting approval.")
        case.status = CaseStatus.failed
        case.requires_human = False
        case.outcome = f"Rejected by {rejected_by}." + (f" {reason}" if reason else "")
        run = case.run_for(AgentId.human_approval)
        if run:
            run.status = AgentRunStatus.blocked
            run.summary = case.outcome
            run.completed_at = utc_now()
        case.add_audit(AgentId.human_approval, "rejected", case.outcome, CaseStatus.failed.value)
        self.repository.save_case(case)
        return case

    # ------------------------------------------------------------------
    # Payment execution
    # ------------------------------------------------------------------
    def execute_payment(
        self, case_id: str, simulate: str = "SUCCESS", failure_reason: str | None = None
    ) -> WorkflowCase:
        """Hand an approved case to Agent 5. Never executes without approval."""

        case = self.repository.get_case(case_id)
        if case is None:
            raise WorkflowError("Case not found.")

        run = case.run_for(AgentId.agent_5) or AgentRun(
            agent=AgentId.agent_5, agent_name=AGENT_LABELS[AgentId.agent_5], order=len(case.agent_runs)
        )
        if run not in case.agent_runs:
            case.agent_runs.append(run)

        case.status = CaseStatus.executing
        run.status = AgentRunStatus.running
        run.started_at = utc_now()
        case.add_audit(AgentId.agent_5, "execution_started", "Agent 5 started the synthetic payment.")

        report = self.execution.execute(
            case,
            approval=case.approval,
            invoice_number=case.invoice_number,
            payee=case.vendor_name,
            amount=case.amount,
            currency=case.currency,
            simulate=simulate,
            failure_reason=failure_reason,
        )
        case.reports.append(report)
        case.last_payment_report = report
        case.payment = (
            PaymentAttempt.model_validate(report.data["attempt"])
            if report.data.get("attempt")
            else case.payment
        )
        run.summary = report.summary
        run.completed_at = utc_now()
        case.add_audit(AgentId.agent_5, "execution_result", report.summary)

        status = (report.data.get("attempt") or {}).get("status")
        if status == "SUCCESS":
            run.status = AgentRunStatus.completed
            case.status = CaseStatus.completed
            case.outcome = "Synthetic payment executed and recorded."
            case.add_audit(
                AgentId.orchestrator,
                "case_completed",
                case.outcome,
                CaseStatus.completed.value,
            )
            self.repository.save_case(case)
            return case

        if status == "PENDING":
            run.status = AgentRunStatus.waiting
            case.status = CaseStatus.pending
            case.requires_human = True
            case.pending_reason = "The payment outcome is uncertain; no retry is permitted."
            case.outcome = "Payment pending: investigate the existing attempt before doing anything else."
            case.add_audit(AgentId.orchestrator, "payment_pending", case.outcome, CaseStatus.pending.value)
            self.repository.save_case(case)
            return case

        # FAILED (or blocked) -> Agent 4 investigates.
        run.status = AgentRunStatus.blocked
        case.status = CaseStatus.failed
        exception_run = case.run_for(AgentId.agent_4)
        if exception_run is None:
            case.agent_runs.append(
                AgentRun(
                    agent=AgentId.agent_4,
                    agent_name=AGENT_LABELS[AgentId.agent_4],
                    status=AgentRunStatus.running,
                    order=len(case.agent_runs),
                    started_at=utc_now(),
                )
            )
        else:
            exception_run.status = AgentRunStatus.running
            exception_run.started_at = utc_now()
        case.add_audit(
            AgentId.orchestrator,
            "handoff",
            "Agent 5 could not complete the payment; Agent 4 is investigating.",
        )
        self.repository.save_case(case)

        attempts = self.repository.list_attempts(case.case_id)
        exception_report = self.exceptions.investigate(
            case, attempts[-1] if attempts else None, failure_reason
        )
        case.reports.append(exception_report)
        exception_run = case.run_for(AgentId.agent_4)
        if exception_run:
            exception_run.status = AgentRunStatus.requires_human
            exception_run.summary = exception_report.summary
            exception_run.completed_at = utc_now()
        case.status = CaseStatus.requires_review
        case.requires_human = True
        case.outcome = exception_report.summary
        review = case.run_for(AgentId.human_review)
        if review is None:
            case.agent_runs.append(
                AgentRun(
                    agent=AgentId.human_review,
                    agent_name=AGENT_LABELS[AgentId.human_review],
                    status=AgentRunStatus.requires_human,
                    order=len(case.agent_runs),
                    summary="Human must resolve the payment exception.",
                )
            )
        case.add_audit(
            AgentId.agent_4,
            "exception_reported",
            exception_report.next_action or exception_report.summary,
            CaseStatus.requires_review.value,
        )
        self.repository.save_case(case)
        return case

    def get(self, case_id: str) -> WorkflowCase | None:
        return self.repository.get_case(case_id)
