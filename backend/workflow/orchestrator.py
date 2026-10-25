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
from .agent5_confirmation import VERIFIED_VERDICTS, FinalConfirmationAgent
from .payment_simulator import PaymentSimulatorService
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
    # An existing-payment check goes straight to Agent 2, then Agent 5 confirms.
    Intent.payment_status_check: (AgentId.agent_2, None),
    Intent.payment_failure: (AgentId.agent_4, None),
    Intent.unknown: (AgentId.human_review, None),
}

# Agents that end a case unless they hand off.
TERMINAL_AGENT_OUTCOMES = {AgentId.human_review}


def _intent_from_analysis(
    analysis: AnalysisResult,
    paid_already: bool = False,
    has_attempts: bool = False,
) -> Intent:
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
    # Nothing reported: only a clean invoice can start a new payment. But if the
    # invoice already has payment records, it is not a fresh request - route it
    # through Agent 1 so the existing history is examined instead of paying twice.
    if problem.type == ProblemType.none_identified and not analysis.requires_review:
        if paid_already or has_attempts:
            return Intent.duplicate_invoice
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

    # Only money-relevant gaps stop a payment. A missing issue date or vendor ID
    # does not make an invoice unsafe to approve: the approver sees the document
    # and can judge. Blocking on every missing field made a perfectly payable
    # invoice fall through to generic human review.
    blocking_fields = [
        field
        for field in validation.missing_fields
        if any(
            marker in field
            for marker in (
                "total_amount",
                "currency",
                "customer.customer_name",
                "vendor.vendor_name",
            )
        )
    ]
    if blocking_fields:
        reasons.append("missing required fields: " + ", ".join(blocking_fields))
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
        # Agent 1 may use the LLM to phrase its summary, never to decide.
        summarizer = None
        extractor = None
        try:
            from .agent1_llm import make_summarizer
            from agent3.extractor import build_extractor_from_environment

            extractor = build_extractor_from_environment()
            summarizer = make_summarizer(extractor)
        except Exception:  # noqa: BLE001 - the LLM is optional
            summarizer = None

        # Agent 4 writes its investigation narrative with the same model. The
        # deterministic facts are always gathered; the model only phrases them.
        report_writer = None
        try:
            from .agent4_llm import build_report_writer

            report_writer = build_report_writer(extractor)
        except Exception:  # noqa: BLE001
            report_writer = None
        self.duplicates = DuplicateInvestigator(repository, summarizer=summarizer)
        self.reconciliation = ReconciliationSpecialist(repository)
        self.exceptions = PaymentExceptionSpecialist(repository, report_writer=report_writer)
        # Agent 5 is the FINAL agent: it confirms outcomes and answers the
        # customer. It has no payment-execution path at all.
        self.execution = FinalConfirmationAgent(repository)
        # The Payment Simulator is a service, not an agent. It is the only
        # component allowed to move fictional money.
        self.simulator = PaymentSimulatorService(repository)
        # `execution` is Agent 5 (confirmation only). Expose the simulator's
        # bank and store here for backwards compatibility with existing callers.
        self.execution.bank = self.simulator.bank
        self.execution.store = self.simulator.store

        # Keep the bank and payment store shared so a replaced bank (tests,
        # demos) is used by both the simulator and reconciliation.
        # The simulator owns the bank and the attempt store; tests may swap the bank.

    # ------------------------------------------------------------------
    # Case lifecycle
    # ------------------------------------------------------------------
    def open_case(self, analysis: AnalysisResult, filename: str = "document.pdf") -> WorkflowCase:
        """Create a case from Agent 3's analysis and route it immediately."""

        # An invoice with payment history is never a brand-new request.
        prior_attempts = self.simulator.store.list_for_invoice(analysis.document_id)
        paid_already = any(a.status == "SUCCESS" and a.verified for a in prior_attempts)
        intent = _intent_from_analysis(
            analysis,
            paid_already=paid_already,
            has_attempts=bool(prior_attempts),
        )
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
        """Pre-build the visible workflow so the UI can show what is coming.

        Path A (PAY_INVOICE):
            Agent 3 -> Agent 1 -> Approval -> Simulator -> Agent 2 -> Agent 5

        Path B (CUSTOMER_PROBLEM): Agent 3 -> specialist(s) -> Agent 5

        The plan is replaced as the real branch becomes known: a failed payment
        swaps Agent 2 for Agent 4, and Agent 5 always keeps its final slot.
        """

        intent = case.intent
        order: list[AgentId] = [AgentId.agent_3]

        if intent == Intent.new_payment_request:
            # Path A. Agent 1 checks history, a human approves, the simulator
            # moves the money, Agent 2 verifies it, Agent 5 confirms.
            order += [
                AgentId.agent_1,
                AgentId.human_approval,
                AgentId.payment_simulator,
                AgentId.agent_2,
                AgentId.agent_5,
            ]
        elif intent in {Intent.duplicate_invoice, Intent.duplicate_payment}:
            # Path B: a duplicate investigation, verified against the bank.
            order += [AgentId.agent_1, AgentId.agent_2, AgentId.agent_5]
        elif intent in {
            Intent.bank_reconciliation,
            Intent.payment_status_dispute,
            Intent.payment_status_check,
        }:
            # Path B: an existing-payment check goes straight to Agent 2.
            order += [AgentId.agent_2, AgentId.agent_5]
        elif intent == Intent.payment_failure:
            # Path B: a reported failure needs Agent 4, then Agent 5.
            order += [AgentId.agent_4, AgentId.agent_5]
        else:
            # Path B: not enough information; a human supplies it.
            order += [AgentId.human_review, AgentId.agent_5]

        planned: list[AgentRun] = []
        for index, agent in enumerate(order):
            planned.append(
                AgentRun(
                    agent=agent,
                    agent_name=AGENT_LABELS[agent],
                    status=AgentRunStatus.waiting,
                    order=index,
                    phase="confirmation" if agent == AgentId.agent_5 else None,
                )
            )
        case.agent_runs = planned

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
            # When Agent 2 has already reported, Agent 1 produces its final
            # report using the verified settlement count.
            bank_settlements = None
            verification = next(
                (r for r in case.reports if r.agent == AgentId.agent_2 and r.data.get("matched") is not None),
                None,
            )
            if verification is not None:
                bank_settlements = int(verification.data.get("settlement_count") or 0)
            report = self.duplicates.investigate(
                case,
                analysis,
                verified_by_agent2=verification is not None,
                bank_settlements=bank_settlements,
            )
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

        # Agent 1 reported the original invoice is still UNPAID. A repeat upload
        # is not a duplicate payment, so the case goes to approval and Agent 5
        # pays the *existing* obligation once.
        if report.agent == AgentId.agent_1:
            investigation = report.data.get("investigation") or {}
            previous = investigation.get("previous_payment_status")
            if previous == "UNPAID_ELIGIBLE_FOR_APPROVAL":
                return self._to_approval(case, report, investigation)
            if previous == "PAYMENT_FAILED" and not case.run_for(AgentId.agent_4):
                return self._to_agent_4(case, report)

        # Agent 1 -> Agent 2 -> Agent 1: when Agent 2 has verified (or refuted)
        # the settlements, Agent 1 issues its final report before the case ends.
        if report.agent == AgentId.agent_2:
            agent1_report = next(
                (r for r in reversed(case.reports) if r.agent == AgentId.agent_1), None
            )
            if agent1_report is not None and not report.data.get("final_agent1_done"):
                case.add_audit(
                    AgentId.orchestrator,
                    "handoff",
                    f"{AGENT_LABELS[AgentId.agent_2]} returned verification to "
                    f"{AGENT_LABELS[AgentId.agent_1]} for a final report.",
                )
                analysis = AnalysisResult.model_validate(case.analysis) if case.analysis else None
                if analysis is not None:
                    return self.run_agent(case, AgentId.agent_1, analysis)

        # Agent 5 is the FINAL agent on every path. A specialist never closes a
        # case on its own: Agent 5 issues the customer response and decides the
        # terminal status. This branch runs for Path B (customer problems),
        # because Path A returns earlier through execute_payment.
        if report.agent != AgentId.agent_5 and report.agent != AgentId.agent_4:
            return self._finish_path_b(case)

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

    def _finish_path_b(self, case: WorkflowCase) -> WorkflowCase:
        """Close a customer-problem case with Agent 5's final response."""

        # An unresolved reconciliation or payment exception must be investigated
        # before it is explained. Agent 2 has already run by this point.
        recommendation = case.reconciliation or {}
        verdict = recommendation.get("verdict")
        if verdict and verdict not in VERIFIED_VERDICTS and not case.run_for(AgentId.agent_4):
            self._investigate_exception(case, recommendation)

        # Any planned specialist that did not run is skipped, never left
        # hanging in WAITING.
        for planned in case.agent_runs:
            if planned.status == AgentRunStatus.waiting and planned.agent != AgentId.agent_5:
                planned.status = AgentRunStatus.skipped
                planned.summary = "Not required for this route."
                planned.completed_at = utc_now()

        return self._finish_with_agent5(
            case,
            lambda run: self.execution.explain_case(case),
            stage="explanation",
        )

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
        simulate: str | None = None,
        failure_reason: str | None = None,
        failure_code: str | None = None,
    ) -> WorkflowCase:
        """Record a human approval bound to the case's exact payment terms."""

        case = self.repository.get_case(case_id)
        if case is None:
            raise WorkflowError("Case not found.")
        # Defence in depth: a duplicate investigation that flagged the invoice
        # blocks payment even if a stale approval somehow exists.
        blocked = self._duplicate_block(case)
        if blocked:
            raise WorkflowError(f"Payment blocked: {blocked}")
        if case.status != CaseStatus.awaiting_approval:
            raise WorkflowError(
                f"Case is {case.status.value}; only a case awaiting approval can be approved."
            )

        # Path A requires Agent 1's duplicate/payment-history check before any
        # money moves. It runs here, immediately before the human decision, and
        # only blocks when it actually finds a duplicate or an existing payment.
        if self._run_agent1_precheck(case):
            self.repository.save_case(case)
            return case

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
        return self.execute_payment(
            case_id,
            simulate=simulate,
            failure_reason=failure_reason,
            failure_code=failure_code,
        )

    def _run_agent1_precheck(self, case: WorkflowCase) -> bool:
        """Run Agent 1 before approval on a new-payment case.

        Returns True when the check stopped the payment. A clean invoice with no
        comparable history is *not* a blocker: the absence of history is not
        evidence of a duplicate.
        """

        run = case.run_for(AgentId.agent_1)
        if run is not None and run.status == AgentRunStatus.completed:
            return False  # already investigated

        if run is None:
            run = AgentRun(
                agent=AgentId.agent_1,
                agent_name=AGENT_LABELS[AgentId.agent_1],
                order=len(case.agent_runs),
            )
            case.agent_runs.append(run)

        run.status = AgentRunStatus.running
        run.started_at = utc_now()
        case.add_audit(
            AgentId.orchestrator,
            "handoff",
            "Agent 1 is checking existing invoices and payment history before approval.",
        )
        self.repository.save_case(case)

        analysis = AnalysisResult.model_validate(case.analysis) if case.analysis else None
        self.run_agent(case, AgentId.agent_1, analysis)

        # `run_agent` may already have progressed the case (for example when the
        # invoice turns out to be unpaid and still needs payment). Only
        # intervene when the check found a reason to stop.
        report = next(
            (r for r in reversed(case.reports) if r.agent == AgentId.agent_1), None
        )
        if report is None:
            return False
        investigation = report.data.get("investigation") or {}
        verdict = investigation.get("status")

        # The *payment* verdict decides, not the invoice verdict. A repeated
        # invoice that has never been paid is still payable once; only money
        # that actually moved (or is stuck) blocks a new instruction.
        payment_state = investigation.get("previous_payment_status")
        blocks_payment = investigation.get("blocks_payment") is True

        blocking = blocks_payment and (
            verdict in {
                "CONFIRMED_DUPLICATE_PAYMENT",
                "SUSPECTED_DUPLICATE_PAYMENT",
                "REQUIRES_BANK_VERIFICATION",
            }
            or payment_state in {
                "ALREADY_PAID",
                "DUPLICATE_PAYMENT_DETECTED",
                "PAYMENT_PENDING",
            }
        )
        if blocking:
            case.status = CaseStatus.requires_review
            case.requires_human = True
            case.outcome = report.summary
            self._ensure_human_review(case, "Agent 1 blocked this payment.")
            return True

        # No history to compare against: Agent 1 has nothing to block on.
        if run.status == AgentRunStatus.requires_human:
            run.status = AgentRunStatus.completed
            run.summary = "No comparable history; nothing blocks this payment."
        return False

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
        self,
        case_id: str,
        simulate: str | None = None,
        failure_reason: str | None = None,
        failure_code: str | None = None,
    ) -> WorkflowCase:
        """Run Path A: simulate the payment, verify it, then let Agent 5 close.

        The sequence is fixed:

            Approval -> Payment Simulator -> (Agent 2 | Agent 4) -> Agent 5

        Agent 5 runs in **both** branches, so no required step is left waiting
        when the case reaches its terminal status.
        """

        case = self.repository.get_case(case_id)
        if case is None:
            raise WorkflowError("Case not found.")

        blocked = self._duplicate_block(case)
        if blocked:
            case.add_audit(AgentId.orchestrator, "payment_blocked", blocked)
            self.repository.save_case(case)
            raise WorkflowError(f"Payment blocked: {blocked}")

        # ---------- Stage 1: the Payment Simulator -------------------------
        sim_run = case.run_for(AgentId.payment_simulator) or AgentRun(
            agent=AgentId.payment_simulator,
            agent_name=AGENT_LABELS[AgentId.payment_simulator],
            order=len(case.agent_runs),
        )
        if sim_run not in case.agent_runs:
            case.agent_runs.append(sim_run)

        case.status = CaseStatus.executing
        sim_run.status = AgentRunStatus.running
        sim_run.started_at = utc_now()
        case.add_audit(
            AgentId.payment_simulator,
            "execution_started",
            "The Payment Simulator is submitting the approved instruction.",
        )

        initiation = self.simulator.execute(
            case,
            approval=case.approval,
            invoice_number=case.invoice_number,
            payee=case.vendor_name,
            amount=case.amount,
            currency=case.currency,
            simulate=simulate,
            failure_reason=failure_reason,
            failure_code=failure_code,
        )
        case.reports.append(initiation)
        if initiation.data.get("attempt"):
            case.payment = PaymentAttempt.model_validate(initiation.data["attempt"])

        if initiation.data.get("executed") is False:
            # No money moved: blocked, or a repeated request.
            sim_run.summary = initiation.summary
            sim_run.completed_at = utc_now()
            sim_run.status = AgentRunStatus.blocked if initiation.blocking else AgentRunStatus.completed
            case.last_payment_report = initiation
            case.add_audit(AgentId.payment_simulator, "execution_blocked", initiation.summary)
            case.status = CaseStatus.pending if not initiation.blocking else CaseStatus.requires_review
            case.requires_human = bool(initiation.requires_human)
            case.outcome = initiation.summary
            self.repository.save_case(case)
            return case

        sim_run.summary = initiation.summary
        sim_run.completed_at = utc_now()
        sim_run.status = AgentRunStatus.completed
        case.add_audit(AgentId.payment_simulator, "execution_result", initiation.summary)

        # ---------- Stage 2: branch on the outcome -------------------------
        if case.payment is not None and case.payment.status == "FAILED":
            # A rejected instruction never reaches reconciliation.
            self._skip_planned(case, AgentId.agent_2, "Payment was rejected; nothing to reconcile.")
            return self._failed_branch(case, initiation, failure_reason)

        return self._verify_branch(case)

    # ------------------------------------------------------------------
    def _verify_branch(self, case: WorkflowCase) -> WorkflowCase:
        """Agent 2 verifies the money, then Agent 5 confirms."""

        reconcile_run = case.run_for(AgentId.agent_2)
        if reconcile_run is None:
            reconcile_run = AgentRun(
                agent=AgentId.agent_2,
                agent_name=AGENT_LABELS[AgentId.agent_2],
                order=len(case.agent_runs),
                status=AgentRunStatus.running,
                started_at=utc_now(),
            )
            case.agent_runs.append(reconcile_run)
        else:
            reconcile_run.status = AgentRunStatus.running
            reconcile_run.started_at = utc_now()

        case.status = CaseStatus.investigating
        case.add_audit(
            AgentId.orchestrator,
            "handoff",
            "Agent 2 is reconciling the instruction against the Mock Bank ledger.",
        )
        recommendation = self.reconciliation.reconcile_payment(case)
        case.reconciliation = recommendation
        case.reports.append(self.reconciliation.reconciliation_report(case, recommendation))
        reconcile_run.summary = recommendation.get("summary")
        reconcile_run.completed_at = utc_now()
        reconcile_run.status = (
            AgentRunStatus.completed
            if recommendation.get("verdict") == "MATCHED"
            else AgentRunStatus.requires_human
        )
        case.add_audit(
            AgentId.agent_2,
            "reconciliation_result",
            recommendation.get("summary") or "Reconciliation complete.",
            recommendation.get("verdict"),
        )
        self.repository.save_case(case)

        # A verified payment needs no exception investigation.
        self._skip_planned(case, AgentId.agent_4, "Settlement verified; no exception to investigate.")

        # An unresolved reconciliation needs Agent 4's investigation before the
        # case can be explained. A clean MATCHED needs nobody.
        if recommendation.get("verdict") not in VERIFIED_VERDICTS:
            self._investigate_exception(case, recommendation)

        # Agent 5 always runs last.
        return self._finish_with_agent5(
            case,
            lambda run: self.execution.confirm_payment(
                case, recommendation, approval=case.approval
            ),
            stage="confirmation",
        )

    def _investigate_exception(self, case: WorkflowCase, recommendation: dict) -> None:
        """Run Agent 4 on an unresolved reconciliation before Agent 5 closes."""

        run = case.run_for(AgentId.agent_4)
        if run is None:
            run = AgentRun(
                agent=AgentId.agent_4,
                agent_name=AGENT_LABELS[AgentId.agent_4],
                order=len(case.agent_runs),
            )
            case.agent_runs.append(run)
        run.status = AgentRunStatus.running
        run.started_at = utc_now()

        case.add_audit(
            AgentId.orchestrator,
            "handoff",
            f"Reconciliation reported {recommendation.get('verdict')}; Agent 4 is investigating.",
        )
        self.repository.save_case(case)

        history = self.simulator.store.list_for_invoice(case.invoice_number)
        report = self.exceptions.investigate(
            case,
            case.payment or (history[-1] if history else None),
            None,
            attempt_history=history,
            reconciliation=recommendation,
        )
        case.reports.append(report)
        run.summary = report.summary
        run.completed_at = utc_now()
        run.status = AgentRunStatus.requires_human
        case.add_audit(AgentId.agent_4, "exception_reported", report.summary)
        self.repository.save_case(case)

    def _failed_branch(self, case: WorkflowCase, initiation: AgentReport, failure_reason) -> WorkflowCase:
        """Agent 4 investigates the failure, then Agent 5 closes with the report."""

        exception_run = case.run_for(AgentId.agent_4)
        if exception_run is None:
            exception_run = AgentRun(
                agent=AgentId.agent_4,
                agent_name=AGENT_LABELS[AgentId.agent_4],
                status=AgentRunStatus.running,
                order=len(case.agent_runs),
                started_at=utc_now(),
            )
            case.agent_runs.append(exception_run)
        else:
            exception_run.status = AgentRunStatus.running
            exception_run.started_at = utc_now()

        case.status = CaseStatus.investigating
        case.add_audit(
            AgentId.orchestrator,
            "handoff",
            "The instruction failed; Agent 4 is investigating why.",
        )
        self.repository.save_case(case)

        history = self.simulator.store.list_for_invoice(case.invoice_number)
        exception_report = self.exceptions.investigate(
            case,
            case.payment or (history[-1] if history else None),
            failure_reason,
            provider_response=initiation.data.get("bank_instruction"),
            attempt_history=history,
            reconciliation=case.reconciliation,
        )
        case.reports.append(exception_report)
        exception_run.summary = exception_report.summary
        exception_run.completed_at = utc_now()
        exception_run.status = AgentRunStatus.requires_human
        case.add_audit(
            AgentId.agent_4,
            "exception_reported",
            exception_report.next_action or exception_report.summary,
        )
        self.repository.save_case(case)

        # Agent 5 still runs: it issues the final failure explanation.
        return self._finish_with_agent5(
            case,
            lambda run: self.execution.report_failure(case, exception_report, case.payment),
            stage="failure_report",
            forced_status=CaseStatus.requires_review,
        )

    # ------------------------------------------------------------------
    def _finish_with_agent5(
        self,
        case: WorkflowCase,
        call,
        *,
        stage: str,
        forced_status: CaseStatus | None = None,
    ) -> WorkflowCase:
        """Run Agent 5's final step and settle the case's terminal status."""

        run = case.run_for(AgentId.agent_5) or AgentRun(
            agent=AgentId.agent_5,
            agent_name=AGENT_LABELS[AgentId.agent_5],
            order=len(case.agent_runs),
        )
        if stage == "confirmation":
            # Reuse a planned confirmation run, but never the simulator's slot.
            planned = case.run_for(AgentId.agent_5, "confirmation")
            if planned is not None:
                run = planned
        if run not in case.agent_runs:
            case.agent_runs.append(run)

        # Keep the planned order: Agent 5 always sorts last.
        if run.order < len(case.agent_runs) - 1:
            run.order = max(r.order for r in case.agent_runs) + 1

        run.phase = stage
        run.status = AgentRunStatus.running
        run.started_at = utc_now()

        report = call(run)
        case.reports.append(report)
        case.last_payment_report = report

        if report.data.get("attempt"):
            case.payment = PaymentAttempt.model_validate(report.data["attempt"])
        if report.data.get("receipt"):
            from .schemas import PaymentReceipt

            case.receipt = PaymentReceipt.model_validate(report.data["receipt"])
            case.add_audit(
                AgentId.agent_5,
                "receipt_issued",
                f"Receipt {case.receipt.receipt_id[:8]} issued for {case.receipt.provider_reference}.",
            )

        run.summary = report.summary
        run.completed_at = utc_now()
        case.add_audit(AgentId.agent_5, "final_response", report.summary)

        if report.resolved and forced_status is None:
            run.status = AgentRunStatus.completed
            case.status = CaseStatus.completed
            case.requires_human = False
            case.outcome = report.summary
            # A confirmed case needs no human step (one may have been planned).
            stray = case.run_for(AgentId.human_review)
            if stray is not None and stray.status == AgentRunStatus.waiting:
                stray.status = AgentRunStatus.skipped
                stray.summary = "Case completed; review not required."
            case.add_audit(
                AgentId.orchestrator,
                "case_completed",
                report.summary,
                CaseStatus.completed.value,
            )
        elif forced_status is not None:
            run.status = AgentRunStatus.completed
            case.status = forced_status
            case.requires_human = True
            case.outcome = report.summary
            self._ensure_human_review(case, "Human must act on the final failure report.")
        else:
            run.status = AgentRunStatus.requires_human
            case.requires_human = True
            case.outcome = report.summary
            final_status = report.data.get("final_status")
            if final_status == "PENDING":
                case.status = CaseStatus.pending
                case.pending_reason = (
                    f"Agent 2 reported {(case.reconciliation or {}).get('verdict')}. "
                    "The payment is not confirmed; check it again once the bank settles."
                )
                case.add_audit(
                    AgentId.orchestrator, "payment_pending", case.outcome, CaseStatus.pending.value
                )
            else:
                case.status = CaseStatus.requires_review
                self._ensure_human_review(case, "Human must resolve the reconciliation result.")

        self.repository.save_case(case)
        return case

    @staticmethod
    def _skip_planned(case: WorkflowCase, agent: AgentId, reason: str) -> None:
        """Mark a planned step that this branch does not need."""

        for run in case.agent_runs:
            if run.agent == agent and run.status == AgentRunStatus.waiting:
                run.status = AgentRunStatus.skipped
                run.summary = reason
                run.completed_at = utc_now()

    def _ensure_human_review(self, case: WorkflowCase, summary: str) -> None:
        """Add a Human Review step only when one is genuinely needed."""

        review = case.run_for(AgentId.human_review)
        if review is None:
            case.agent_runs.append(
                AgentRun(
                    agent=AgentId.human_review,
                    agent_name=AGENT_LABELS[AgentId.human_review],
                    status=AgentRunStatus.requires_human,
                    order=len(case.agent_runs),
                    summary=summary,
                )
            )
        else:
            review.status = AgentRunStatus.requires_human
            review.summary = summary
        case.add_audit(
            AgentId.orchestrator, "human_review_required", case.outcome or summary,
            CaseStatus.requires_review.value,
        )

    def _to_approval(self, case: WorkflowCase, report: AgentReport, investigation: dict) -> WorkflowCase:
        """Case B: an unpaid duplicate reuses the original obligation for approval."""

        if case.run_for(AgentId.human_approval) is None:
            case.agent_runs.append(
                AgentRun(
                    agent=AgentId.human_approval,
                    agent_name=AGENT_LABELS[AgentId.human_approval],
                    status=AgentRunStatus.waiting,
                    order=len(case.agent_runs),
                )
            )
        if case.run_for(AgentId.agent_5) is None:
            case.agent_runs.append(
                AgentRun(
                    agent=AgentId.agent_5,
                    agent_name=AGENT_LABELS[AgentId.agent_5],
                    status=AgentRunStatus.waiting,
                    order=len(case.agent_runs),
                )
            )
        case.status = CaseStatus.awaiting_approval
        case.requires_human = True
        case.outcome = (
            "The original invoice is unpaid, so this repeated submission reuses that obligation. "
            "A human must approve before Agent 5 pays it once."
        )
        run = case.run_for(AgentId.human_approval)
        if run is not None:
            run.status = AgentRunStatus.requires_human
            run.summary = "Waiting for human approval of the existing obligation."
            run.started_at = run.started_at or utc_now()
        obligation = investigation.get("obligation_id")
        case.add_audit(
            AgentId.orchestrator,
            "duplicate_reuses_obligation",
            (
                f"Invoice {case.invoice_number or '(unknown)'} was submitted again but is unpaid. "
                f"The existing obligation {obligation or '(none)'} is reused; no second payment obligation "
                "was created."
            ),
        )
        case.add_audit(
            AgentId.orchestrator,
            "awaiting_approval",
            "Duplicate invoice is unpaid and awaits human approval.",
            CaseStatus.awaiting_approval.value,
        )
        self.repository.save_case(case)
        return case

    def _payment_failed(
        self, case: WorkflowCase, report: AgentReport, failure_reason: str | None = None
    ) -> WorkflowCase:
        """The Mock Bank rejected the instruction, so Agent 4 investigates."""

        exception_run = case.run_for(AgentId.agent_4)
        if exception_run is None:
            exception_run = AgentRun(
                agent=AgentId.agent_4,
                agent_name=AGENT_LABELS[AgentId.agent_4],
                status=AgentRunStatus.running,
                order=len(case.agent_runs),
                started_at=utc_now(),
            )
            case.agent_runs.append(exception_run)
        else:
            exception_run.status = AgentRunStatus.running
            exception_run.started_at = utc_now()

        case.status = CaseStatus.investigating
        case.add_audit(
            AgentId.orchestrator,
            "handoff",
            "Agent 5 could not move the money; Agent 4 is investigating.",
        )
        self.repository.save_case(case)

        history = self.simulator.store.list_for_invoice(case.invoice_number)
        exception_report = self.exceptions.investigate(
            case,
            case.payment or (history[-1] if history else None),
            failure_reason,
            provider_response=report.data.get("bank_instruction"),
            attempt_history=history,
        )
        case.reports.append(exception_report)
        exception_run.status = AgentRunStatus.requires_human
        exception_run.summary = exception_report.summary
        exception_run.completed_at = utc_now()

        case.status = CaseStatus.requires_review
        case.requires_human = True
        case.outcome = exception_report.summary
        if case.run_for(AgentId.human_review) is None:
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

    def _to_agent_4(self, case: WorkflowCase, report: AgentReport) -> WorkflowCase:
        """Case D: the previous payment failed, so Agent 4 takes over."""

        if case.run_for(AgentId.agent_4) is None:
            case.agent_runs.append(
                AgentRun(
                    agent=AgentId.agent_4,
                    agent_name=AGENT_LABELS[AgentId.agent_4],
                    status=AgentRunStatus.waiting,
                    order=len(case.agent_runs),
                )
            )
        case.status = CaseStatus.investigating
        case.add_audit(
            AgentId.orchestrator,
            "handoff",
            "A previous payment attempt failed; Agent 4 must confirm nothing settled.",
        )
        self.repository.save_case(case)
        analysis = AnalysisResult.model_validate(case.analysis) if case.analysis else None
        return self.run_agent(case, AgentId.agent_4, analysis)

    @staticmethod
    def _duplicate_block(case: WorkflowCase) -> str | None:
        """Return a reason when Agent 1's findings forbid paying this invoice.

        A suspected or confirmed duplicate must never reach Agent 5, regardless
        of how the case got to an approved state.
        """

        for report in case.reports:
            if report.agent != AgentId.agent_1:
                continue
            investigation = report.data.get("investigation") or {}

            # The previous-invoice payment check is authoritative. A repeated
            # submission of an unpaid invoice must NOT be blocked: it reuses the
            # existing obligation and proceeds to approval.
            previous = investigation.get("previous_payment_status")
            if previous == "UNPAID_ELIGIBLE_FOR_APPROVAL":
                return None
            if previous in {
                "ALREADY_PAID",
                "PAYMENT_PENDING",
                "PAYMENT_FAILED",
                "PAYMENT_STATUS_UNKNOWN",
                "DUPLICATE_PAYMENT_DETECTED",
            }:
                return (
                    f"Agent 1 determined the previous payment status is {previous.replace('_', ' ').lower()} "
                    f"for invoice {case.invoice_number or '(unknown)'}. Resolve it before any payment."
                )

            status = investigation.get("status") or report.data.get("status")
            if status in {
                "CONFIRMED_DUPLICATE_INVOICE",
                "CONFIRMED_DUPLICATE_PAYMENT",
                "SUSPECTED_DUPLICATE_PAYMENT",
            }:
                return (
                    f"Agent 1 reported {status.replace('_', ' ').lower()} for invoice "
                    f"{case.invoice_number or '(unknown)'}. The case must be resolved before any payment."
                )
            if investigation.get("blocks_payment") and status == "POSSIBLE_DUPLICATE":
                return (
                    f"Agent 1 found a possible duplicate for invoice {case.invoice_number or '(unknown)'}. "
                    "A human must confirm which invoice is valid before paying."
                )
        return None

    def get(self, case_id: str) -> WorkflowCase | None:
        return self.repository.get_case(case_id)
