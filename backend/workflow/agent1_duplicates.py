"""Agent 1 - Duplicate Invoice Investigator.

Investigates whether the invoice under review duplicates an invoice GhostLedger
has already seen, and whether it appears to have been paid more than once.

All matching and arithmetic is deterministic Python (see ``agent1_engine``).
Agent 1 investigates and reports; it never pays, refunds, or edits verified bank
records. When settlement needs confirming it asks Agent 2 and waits for the
result before issuing its final report.
"""

from __future__ import annotations

from decimal import Decimal

from agent3.schemas import AnalysisResult, PrimaryProblem

from .agent1_engine import (
    STATUS_GUIDANCE,
    build_payment_analysis,
    decide_status,
    is_recurring_pattern,
    score_candidate,
)
from .agent1_history import (
    InvoiceHistoryRepository,
    normalize_invoice_number,
    normalize_name,
)
from .agent1_payment_state import DECISION_TABLE, resolve_payment_state
from .agent1_schemas import (
    DuplicateInvestigation,
    InvestigationStatus,
    ObligationState,
    PreviousPaymentStatus,
)
from .obligations import ObligationRepository, obligation_key
from .repository import WorkflowRepository
from .schemas import AGENT_LABELS, AgentId, AgentReport, Finding, WorkflowCase, utc_now


def _money(value: Decimal | None, currency: str | None = None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f} {currency or ''}".strip()


def _same_amount_and_party(value, counterparty, amount, parties) -> bool:
    """An unlabelled bank row matches only on the same amount with a party to the invoice.

    ``parties`` is (vendor, customer): stored rows record either the payee or
    the payer as the counterparty of a credit.
    """

    wanted = {normalize_name(p) for p in parties if p} - {""}
    if value in (None, "") or amount is None or not wanted or not counterparty:
        return False
    try:
        same_amount = abs(Decimal(str(value)) - amount) <= Decimal("0.005")
    except Exception:  # noqa: BLE001 - untrusted stored value
        return False
    return same_amount and normalize_name(counterparty) in wanted


class DuplicateInvestigator:
    """Agent 1: investigate duplicate invoices and duplicate payments."""

    agent = AgentId.agent_1

    def __init__(
        self,
        repository: WorkflowRepository,
        history: InvoiceHistoryRepository | None = None,
        summarizer=None,
    ):
        self.repository = repository
        self.history = history or InvoiceHistoryRepository(repository.path)
        self.obligations = ObligationRepository(repository.path)
        # Optional LLM summariser. It may explain the result; it never decides it.
        self.summarizer = summarizer

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def investigate(
        self,
        case: WorkflowCase,
        analysis: AnalysisResult,
        document_text: str = "",
        *,
        verified_by_agent2: bool = False,
        bank_settlements: int | None = None,
    ) -> AgentReport:
        investigation = self.build_investigation(
            case,
            analysis,
            verified_by_agent2=verified_by_agent2,
            bank_settlements=bank_settlements,
        )
        return self.to_agent_report(investigation, case)

    def _bank_evidence(self, amount, invoice_number, parties=(), references=()):
        """Synthetic bank transactions, from every ledger, that belong to this invoice.

        The Mock Bank keeps its own ledger and the importer keeps imported
        statements; both are evidence of money received. A row belongs to the
        invoice when it names the invoice, carries a payment reference known for
        it, or (for a row with no invoice label) has the same amount and a
        counterparty that is the invoice's vendor or customer. Counting every credit in the bank made
        unrelated payments look like this invoice being paid.
        """

        from .schemas import SyntheticTransaction

        found: list[SyntheticTransaction] = []
        seen: set[str] = set()
        wanted_number = normalize_invoice_number(invoice_number)
        known_refs = {r.casefold() for r in references if r}

        def belongs(label, reference, value, counterparty) -> bool:
            if reference and reference.casefold() in known_refs:
                return True
            if label:
                return bool(wanted_number) and wanted_number in normalize_invoice_number(label)
            return _same_amount_and_party(value, counterparty, amount, parties)

        try:
            from .mock_bank import MockBank

            # SyntheticTransaction accepts lowercase statuses only.
            STATUS_MAP = {
                "SETTLED": "settled",
                "PARTIAL": "settled",
                "PENDING": "pending",
                "FAILED": "failed",
            }

            for entry in MockBank(self.repository.path).ledger():
                if entry["direction"] != "credit":
                    continue
                if not belongs(
                    entry.get("invoice_number"),
                    entry["payment_reference"],
                    entry.get("received_amount") or entry["amount"],
                    entry["counterparty"] or entry.get("vendor_name"),
                ):
                    continue
                txn = SyntheticTransaction(
                    transaction_id=entry["entry_id"],
                    reference=entry["payment_reference"],
                    amount=__import__("decimal").Decimal(
                        entry.get("received_amount") or entry["amount"]
                    ),
                    currency=entry["currency"],
                    direction="credit",
                    counterparty=entry["counterparty"],
                    status=STATUS_MAP.get((entry["status"] or "").upper(), "pending"),
                    value_date=entry["value_date"],
                    source="synthetic_bank",
                )
                if txn.transaction_id not in seen:
                    seen.add(txn.transaction_id)
                    found.append(txn)
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning("Mock Bank evidence unavailable: %s", exc)

        try:
            from .bank_importer import BankTransactionStore

            for row in BankTransactionStore(self.repository.path).list_transactions():
                # Statement rows have no invoice column; the description may name it.
                described = f"{row.get('description') or ''} {row['payment_reference'] or ''}"
                if not (
                    (wanted_number and wanted_number in normalize_invoice_number(described))
                    or belongs(None, row["payment_reference"], row["amount"], row["counterparty"])
                ):
                    continue
                txn = SyntheticTransaction(
                    transaction_id=row["row_id"],
                    reference=row["payment_reference"],
                    amount=__import__("decimal").Decimal(row["amount"]),
                    currency=row["currency"],
                    direction="credit",
                    counterparty=row["counterparty"],
                    status="settled",
                    value_date=row["transaction_date"],
                    source="synthetic_bank",
                )
                if txn.transaction_id not in seen:
                    seen.add(txn.transaction_id)
                    found.append(txn)
        except Exception:  # noqa: BLE001
            pass

        return found

    def build_investigation(
        self,
        case: WorkflowCase,
        analysis: AnalysisResult,
        *,
        verified_by_agent2: bool = False,
        bank_settlements: int | None = None,
    ) -> DuplicateInvestigation:
        invoice_number = analysis.document_id or case.invoice_number
        vendor = analysis.vendor.vendor_name or case.vendor_name
        customer = analysis.customer.customer_name or case.customer_name
        amount = analysis.financial_information.total_amount or case.amount
        currency = analysis.currency or analysis.financial_information.currency or case.currency
        issue_date = analysis.issue_date
        description = " ".join(
            item.description for item in analysis.line_items if item.description
        ) or None

        # --- Checks A and B: compare against recorded invoices --------------
        history_available = self.history.available()
        # Agent 3 saves the analysis before routing, so exclude the document itself.
        records = (
            self.history.list_records(customer=customer, exclude_record_id=analysis.record_id)
            if history_available
            else []
        )
        by_record = {record.record_id: record for record in records}

        candidates = [
            score_candidate(
                vendor=vendor,
                customer=customer,
                amount=amount,
                currency=currency,
                issue_date=issue_date,
                description=description,
                prior=record,
            )
            for record in records
        ]
        candidates.sort(key=lambda c: c.match_score, reverse=True)

        wanted_number = normalize_invoice_number(invoice_number)
        wanted_vendor = normalize_name(vendor)
        exact: list = []
        for candidate in candidates:
            prior = by_record.get(candidate.record_id)
            if prior is None:
                continue
            number_matches = bool(wanted_number) and wanted_number == prior.normalized_number
            vendor_matches = bool(wanted_vendor) and wanted_vendor == prior.normalized_vendor
            amount_matches = (
                amount is not None
                and prior.amount is not None
                and abs(amount - prior.amount) <= Decimal("0.005")
            )
            if number_matches and vendor_matches and amount_matches:
                candidate.is_exact_duplicate = True
                candidate.match_band = "exact"
                candidate.evidence.append(
                    f"Same invoice number ({prior.invoice_number}) for the same vendor and amount"
                )
                exact.append(candidate)
            elif number_matches:
                # The same invoice number reused for a different amount or vendor.
                candidate.evidence.append(
                    f"Invoice number {prior.invoice_number} is already on record "
                    f"for {prior.vendor or 'another vendor'} at "
                    f"{_money(prior.amount, prior.currency)}"
                )

        # --- Check C: payment records --------------------------------------
        payment_attempts = [
            attempt.model_dump(mode="json") for attempt in self.repository.list_attempts(case.case_id)
        ]
        seen_attempts = {attempt.get("attempt_id") for attempt in payment_attempts}
        if invoice_number:
            wanted = " ".join(invoice_number.split()).casefold()
            for other in self.repository.list_attempts():
                if other.invoice_number and " ".join(other.invoice_number.split()).casefold() == wanted:
                    payload = other.model_dump(mode="json")
                    if payload["attempt_id"] not in seen_attempts:
                        payment_attempts.append(payload)
                        seen_attempts.add(payload["attempt_id"])

        # Agent 5 records the authoritative payment attempt through its own
        # store; include those so Agent 1 sees payments Agent 5 confirmed.
        try:
            from .payment_store import PaymentRecordStore

            store = PaymentRecordStore(self.repository.path)
            for recorded in store.list_for_invoice(invoice_number):
                payload = recorded.model_dump(mode="json")
                if payload["attempt_id"] not in seen_attempts:
                    payment_attempts.append(payload)
                    seen_attempts.add(payload["attempt_id"])
        except Exception:  # noqa: BLE001 - never break investigation on this
            pass

        claim = analysis.financial_information.payment_claim
        references = [a.get("provider_reference") for a in payment_attempts]
        if claim is not None and claim.reference:
            references.append(claim.reference)
        parties = (vendor, customer)
        transactions = self._bank_evidence(amount, invoice_number, parties, references)
        if amount is not None:
            # Legacy rows carry no invoice number: same amount AND a party to the invoice.
            transactions += [
                txn for txn in self.repository.find_bank_transactions(amount=amount)
                if _same_amount_and_party(txn.amount, txn.counterparty, amount, parties)
            ]
        if claim is not None and claim.reference:
            transactions += self.repository.find_bank_transactions(reference=claim.reference)
        unique_txns, seen_txn = [], set()
        for txn in transactions:
            if txn.transaction_id in seen_txn:
                continue
            seen_txn.add(txn.transaction_id)
            unique_txns.append(txn)

        payment_analysis = build_payment_analysis(
            invoice_number=invoice_number,
            expected_amount=amount,
            currency=currency,
            payment_attempts=payment_attempts,
            bank_transactions=unique_txns,
            reported_payment_count=analysis.detected_problem.reported_payment_count,
            reported_payment_amount=analysis.financial_information.amount_paid,
        )

        # Agent 2 may already have confirmed how many settlements exist.
        if bank_settlements is not None:
            payment_analysis.verified_settlements = max(
                payment_analysis.verified_settlements, bank_settlements
            )
            if payment_analysis.verified_settlements >= 2:
                payment_analysis.confirmed_duplicate_payment = True
                payment_analysis.duplicate_payment_suspected = True
                payment_analysis.needs_bank_verification = False
                payment_analysis.conclusion = (
                    f"Agent 2 confirmed {bank_settlements} settled transaction(s) for invoice "
                    f"{invoice_number or '(unknown)'}; this is a confirmed duplicate payment."
                )
            else:
                payment_analysis.needs_bank_verification = False

        # --- Decide ---------------------------------------------------------
        status = decide_status(
            candidates=candidates,
            exact_duplicates=exact,
            payment=payment_analysis,
            history_available=history_available,
            invoice_number=invoice_number,
            vendor=vendor,
        )
        if (
            verified_by_agent2
            and status == InvestigationStatus.suspected_duplicate_payment
            and not payment_analysis.confirmed_duplicate_payment
        ):
            # Bank verification came back without a second settlement.
            status = (
                InvestigationStatus.possible_duplicate if candidates else InvestigationStatus.no_duplicate_found
            )

        next_action, needs_agent2, needs_human, blocks = STATUS_GUIDANCE[status]
        if status == InvestigationStatus.insufficient_information and not history_available:
            next_action = (
                "GhostLedger holds no earlier invoice records to compare against, so this invoice cannot "
                "be confirmed unique. A human should review it."
            )

        # --- Previous payment check (Cases A-F) -----------------------------
        # A repeated submission is not automatically a duplicate *payment*.
        # Look at what actually happened to the original invoice first.
        # The payment check matters both for a repeated invoice AND for a
        # customer reporting duplicate or disputed payments, even when no prior
        # invoice record exists to compare against.
        intent_says_payment = analysis.detected_problem.primary_problem in {
            PrimaryProblem.duplicate_payment,
            PrimaryProblem.duplicate_invoice,
            PrimaryProblem.bank_reconciliation,
        }
        is_duplicate_submission = (
            status
            in {
                InvestigationStatus.confirmed_duplicate_invoice,
                InvestigationStatus.possible_duplicate,
                InvestigationStatus.suspected_duplicate_payment,
                InvestigationStatus.confirmed_duplicate_payment,
                InvestigationStatus.requires_bank_verification,
            }
            or bool(exact)
            or intent_says_payment
        )
        previous_payment: "OriginalInvoicePaymentState | None" = None
        original_invoice = None
        obligation = None
        obligation_created = False
        decision_reason = ""
        submission_is_duplicate = False
        prior_submissions = 0

        if is_duplicate_submission:
            # Which inventory record is the original?
            original_record = None
            if exact:
                original_record = by_record.get(exact[0].record_id)
            elif candidates:
                original_record = by_record.get(candidates[0].record_id)
            if original_record is not None:
                original_invoice = {
                    "record_id": original_record.record_id,
                    "invoice_number": original_record.invoice_number,
                    "vendor": original_record.vendor,
                    "customer": original_record.customer,
                    "amount": str(original_record.amount) if original_record.amount is not None else None,
                    "currency": original_record.currency,
                    "issue_date": original_record.issue_date,
                }

            # The payable obligation is keyed by invoice identity, so a repeat
            # upload reuses it instead of creating a second one.
            obligation, obligation_created = self.obligations.ensure_obligation(
                invoice_number=invoice_number,
                vendor=vendor,
                customer=customer,
                currency=currency,
                amount=amount,
                case_id=case.case_id,
                original_record_id=original_invoice["record_id"] if original_invoice else None,
            )

            # Payment records for the ORIGINAL invoice, not just this case.
            original_attempts: list[dict] = []
            for attempt in self.repository.list_attempts():
                if attempt.invoice_number and normalize_invoice_number(attempt.invoice_number) == normalize_invoice_number(
                    invoice_number
                ):
                    original_attempts.append(attempt.model_dump(mode="json"))
            if not original_attempts:
                original_attempts = payment_attempts

            previous_payment = resolve_payment_state(
                invoice_amount=amount,
                currency=currency,
                payment_attempts=original_attempts,
                bank_transactions=unique_txns,
            )

            blocks_decision, needs_a2, needs_a4, needs_human_decision, decision_reason = DECISION_TABLE[
                previous_payment.status
            ]
            previous_payment.blocks_payment = blocks_decision

            # Persist the obligation state so later uploads see it.
            state_map = {
                PreviousPaymentStatus.already_paid: ObligationState.paid,
                PreviousPaymentStatus.duplicate_payment_detected: ObligationState.paid,
                PreviousPaymentStatus.payment_pending: ObligationState.pending,
                PreviousPaymentStatus.payment_failed: ObligationState.failed,
                PreviousPaymentStatus.payment_status_unknown: ObligationState.unknown,
                PreviousPaymentStatus.unpaid_eligible_for_approval: ObligationState.open,
            }
            if obligation is not None:
                self.obligations.set_obligation_state(
                    obligation["obligation_id"],
                    state_map[previous_payment.status].value,
                    previous_payment.paid_amount,
                )

            # A submission is a duplicate when EITHER an obligation already
            # existed for this invoice, OR the invoice is already on record and
            # this is not the very first submission for it. Checking only the
            # obligation missed the case where the invoice record predates the
            # obligation, which then read as "newly created" even though the
            # customer had submitted the invoice before.
            prior_submissions = self.obligations.count_submissions(invoice_number)
            submission_is_duplicate = (
                not obligation_created
                or bool(original_invoice)
                or prior_submissions > 0
            )
            self.obligations.record_submission(
                submission_id=case.case_id,
                obligation_id=obligation["obligation_id"] if obligation else None,
                case_id=case.case_id,
                filename=(case.documents[0].filename if case.documents else None),
                invoice_number=invoice_number,
                vendor=vendor,
                customer=customer,
                amount=amount,
                currency=currency,
                is_duplicate=submission_is_duplicate,
                original_invoice=original_invoice,
                payment_status=previous_payment.status.value,
                payment_reference=previous_payment.payment_reference,
            )

            # A reported duplicate payment must not be softened into "unpaid,
            # just approve" merely because the bank shows no settlement. The
            # customer's claim itself is the reason to keep investigating.
            reported_duplicate = analysis.detected_problem.primary_problem in {
                PrimaryProblem.duplicate_payment,
                PrimaryProblem.duplicate_invoice,
            } and (analysis.detected_problem.reported_payment_count or 0) >= 2
            if (
                reported_duplicate
                and previous_payment.status
                in {
                    PreviousPaymentStatus.unpaid_eligible_for_approval,
                    PreviousPaymentStatus.payment_status_unknown,
                }
            ):
                previous_payment.status = PreviousPaymentStatus.payment_status_unknown
                blocks_decision, needs_a2, needs_a4, needs_human_decision, decision_reason = DECISION_TABLE[
                    previous_payment.status
                ]
                previous_payment.blocks_payment = blocks_decision

            # The payment check overrides the generic duplicate verdict.
            needs_agent2 = needs_a2
            needs_human = needs_human_decision
            blocks = blocks_decision
            next_action = decision_reason

            if previous_payment.status == PreviousPaymentStatus.duplicate_payment_detected:
                status = InvestigationStatus.confirmed_duplicate_payment
            elif previous_payment.status == PreviousPaymentStatus.already_paid:
                status = InvestigationStatus.confirmed_duplicate_invoice
            elif previous_payment.status == PreviousPaymentStatus.payment_pending:
                status = InvestigationStatus.suspected_duplicate_payment
            elif previous_payment.status == PreviousPaymentStatus.payment_failed:
                status = InvestigationStatus.requires_human_review
            elif previous_payment.status == PreviousPaymentStatus.payment_status_unknown:
                status = InvestigationStatus.requires_bank_verification
            elif previous_payment.status == PreviousPaymentStatus.unpaid_eligible_for_approval and reported_duplicate:
                status = InvestigationStatus.suspected_duplicate_payment
                needs_agent2 = True
                needs_human = True
                blocks = True
                next_action = (
                    "The customer reports paying this invoice more than once, but no settlement is "
                    "recorded. Agent 2 must verify the payments before this invoice is treated as unpaid."
                )
            elif previous_payment.status == PreviousPaymentStatus.unpaid_eligible_for_approval:
                # The customer claims a payment that the records do not support
                # (for example "I paid twice" with nothing settled). Keep the
                # suspicion rather than downgrading it to "unpaid, just approve".
                if status in {
                    InvestigationStatus.suspected_duplicate_payment,
                    InvestigationStatus.confirmed_duplicate_payment,
                    InvestigationStatus.requires_bank_verification,
                }:
                    needs_human = True
                    blocks = True
                    next_action = (
                        "The customer reports a payment problem, but no settlement is recorded. "
                        "Bank verification is required before treating this invoice as unpaid."
                    )
                    status = InvestigationStatus.suspected_duplicate_payment
                    needs_agent2 = True
                else:
                    status = (
                        InvestigationStatus.possible_duplicate
                        if not exact
                        else InvestigationStatus.confirmed_duplicate_invoice
                    )
                    needs_human = False
                    blocks = False

        investigation = DuplicateInvestigation(
            status=status,
            invoice_number=invoice_number,
            vendor=vendor,
            customer=customer,
            amount=amount,
            currency=currency,
            candidates=candidates[:10],
            best_match=candidates[0] if candidates else None,
            highest_score=candidates[0].match_score if candidates else 0,
            records_compared=len(records),
            history_available=history_available,
            payment_analysis=payment_analysis,
            needs_agent_2=needs_agent2 and not verified_by_agent2,
            needs_human_review=needs_human,
            blocks_payment=blocks,
            recommended_next_action=next_action,
            evidence_count=sum(len(c.evidence) for c in candidates),
            previous_payment_status=previous_payment.status if previous_payment else None,
            original_invoice=original_invoice,
            original_payment_reference=previous_payment.payment_reference if previous_payment else None,
            obligation_id=obligation["obligation_id"] if obligation else None,
            # Reuse is about the payable obligation; duplicate is about the
            # submission. They are related but not the same thing.
            obligation_reused=bool(obligation) and not obligation_created,
            submission_is_duplicate=submission_is_duplicate,
            prior_submission_count=prior_submissions,
            decision_reason=decision_reason,
            needs_agent_4=bool(previous_payment and previous_payment.needs_agent_4),
            remaining_balance=previous_payment.remaining_balance if previous_payment else None,
        )
        investigation.summary = self._summarise(investigation)
        return investigation

    # ------------------------------------------------------------------
    def _summarise(self, inv: DuplicateInvestigation) -> str:
        """Deterministic summary; an LLM may rewrite the prose but not the facts."""

        if inv.status == InvestigationStatus.insufficient_information and not inv.history_available:
            base = "No comparison records are available, so Agent 1 cannot say whether this invoice is unique."
        elif inv.status == InvestigationStatus.confirmed_duplicate_payment:
            base = (
                f"Invoice {inv.invoice_number or '(unknown)'} was paid more than once: bank records show "
                f"{inv.payment_analysis.verified_settlements} settled transactions."
            )
        elif inv.status == InvestigationStatus.confirmed_duplicate_invoice:
            exact = next((c for c in inv.candidates if c.is_exact_duplicate), None)
            base = (
                f"Invoice {inv.invoice_number or '(unknown)'} duplicates an invoice already on record"
                + (f" ({exact.invoice_number}, {_money(exact.amount, exact.currency)})" if exact else "")
                + " with the same vendor and amount."
            )
        elif inv.status == InvestigationStatus.suspected_duplicate_payment:
            base = inv.payment_analysis.conclusion
        elif inv.status == InvestigationStatus.possible_duplicate:
            best = inv.best_match
            base = (
                f"A similar invoice from {best.vendor} for {_money(best.amount, best.currency)} was found "
                f"(match score {best.match_score}/100). It is not an exact duplicate."
                if best
                else "A similar invoice was found."
            )
        elif inv.status == InvestigationStatus.no_duplicate_found:
            recurring = [c for c in inv.candidates if is_recurring_pattern(c)]
            if recurring:
                base = (
                    f"The closest match is a recurring invoice from {recurring[0].vendor} billed about a "
                    "month apart, which is normal billing rather than a duplicate."
                )
            else:
                base = (
                    f"No duplicate was found among {inv.records_compared} earlier invoice record(s) "
                    "for this customer."
                )
        else:
            base = "The investigation was inconclusive."

        if self.summarizer is not None:
            try:
                described = self.summarizer(inv)
                if described:
                    return described
            except Exception:  # noqa: BLE001 - the LLM must never break an investigation
                pass
        return base

    # ------------------------------------------------------------------
    def to_agent_report(self, inv: DuplicateInvestigation, case: WorkflowCase) -> AgentReport:
        started = utc_now()
        findings: list[Finding] = []

        severity = {
            InvestigationStatus.confirmed_duplicate_invoice: "failed",
            InvestigationStatus.confirmed_duplicate_payment: "failed",
            InvestigationStatus.suspected_duplicate_payment: "failed",
            InvestigationStatus.possible_duplicate: "unverified",
            InvestigationStatus.requires_human_review: "unverified",
            InvestigationStatus.insufficient_information: "unverified",
            InvestigationStatus.requires_bank_verification: "unverified",
            InvestigationStatus.no_duplicate_found: "passed",
        }[inv.status]

        findings.append(
            Finding(
                code="investigation_status",
                label="Duplicate investigation",
                status=severity,  # type: ignore[arg-type]
                detail=f"{inv.status.value}: {inv.summary}",
            )
        )

        if not inv.history_available:
            findings.append(
                Finding(
                    code="no_history",
                    label="No comparison records",
                    status="unverified",
                    detail=(
                        "GhostLedger has no earlier invoice records, so this invoice cannot be confirmed "
                        "unique. That is different from being a duplicate."
                    ),
                )
            )

        best = inv.best_match
        if best is not None:
            findings.append(
                Finding(
                    code="closest_match",
                    label="Closest matching invoice",
                    status="info",
                    detail=(
                        f"{best.invoice_number or '(no number)'} from {best.vendor or 'unknown vendor'}, "
                        f"{_money(best.amount, best.currency)}, scored {best.match_score}/100 "
                        f"({best.match_band} similarity)."
                    ),
                )
            )
            for signal in best.signals:
                findings.append(
                    Finding(
                        code=f"signal_{signal.signal}",
                        label=f"Matching signal: {signal.signal.replace('_', ' ')}",
                        status="passed" if signal.matched else "info",
                        detail=f"{signal.detail} ({signal.score}/{signal.weight})",
                    )
                )

        payment = inv.payment_analysis
        findings.append(
            Finding(
                code="payment_records",
                label="Payment records",
                status=(
                    "failed"
                    if payment.confirmed_duplicate_payment
                    else "unverified"
                    if payment.duplicate_payment_suspected
                    else "passed"
                ),
                detail=payment.conclusion,
            )
        )
        for record in payment.records:
            findings.append(
                Finding(
                    code=f"payment_{record.source}",
                    label=f"Payment record ({record.source.replace('_', ' ')})",
                    status="info",
                    detail=record.detail,
                )
            )

        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=inv.summary,
            findings=findings,
            recommended_next=AgentId.agent_2 if inv.needs_agent_2 else None,
            next_action=inv.recommended_next_action,
            requires_human=inv.needs_human_review,
            blocking=inv.blocks_payment,
            resolved=inv.status == InvestigationStatus.no_duplicate_found,
            data={
                "investigation": inv.model_dump(mode="json"),
                "status": inv.status.value,
                "match_score": inv.highest_score,
                "records_compared": inv.records_compared,
                "history_available": inv.history_available,
                "needs_agent_2": inv.needs_agent_2,
                "blocks_payment": inv.blocks_payment,
            },
            started_at=started,
            completed_at=utc_now(),
        )
