"""Agent 1 - Duplicate Invoice Investigator.

Deterministic comparison of invoice numbers, parties, dates and amounts against
everything GhostLedger has already seen, plus repeated payment references inside
the document. Agent 1 investigates and reports; it never refunds anything.
"""

from __future__ import annotations

from decimal import Decimal

from agent3.schemas import AnalysisResult

from .repository import WorkflowRepository
from .schemas import AgentId, AgentReport, AGENT_LABELS, Finding, WorkflowCase, utc_now


def _norm(value: str | None) -> str:
    return " ".join((value or "").split()).strip(" .,;:").casefold()


def _money(value: Decimal | None, currency: str | None = None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f} {currency or ''}".strip()


class DuplicateInvestigator:
    """Agent 1: look for the same invoice being billed or paid more than once."""

    agent = AgentId.agent_1

    def __init__(self, repository: WorkflowRepository):
        self.repository = repository

    def investigate(
        self,
        case: WorkflowCase,
        analysis: AnalysisResult,
        document_text: str = "",
    ) -> AgentReport:
        findings: list[Finding] = []
        started = utc_now()

        invoice_number = analysis.document_id or case.invoice_number
        vendor = analysis.vendor.vendor_name or case.vendor_name
        total = analysis.financial_information.total_amount
        paid = analysis.financial_information.amount_paid
        currency = analysis.currency or analysis.financial_information.currency

        # 1. Repeated payment references inside this document.
        reference_count = analysis.detected_problem.reported_payment_count
        duplicate_amount = analysis.validation.calculated.potential_duplicate_amount
        if duplicate_amount is not None:
            findings.append(
                Finding(
                    code="repeated_payment",
                    label="Repeated payment for the same invoice",
                    status="unverified",
                    detail=(
                        f"{reference_count or 2} separate payments appear to have been made for invoice "
                        f"{invoice_number or '(no number)'}. Potential duplicate: {_money(duplicate_amount, currency)}. "
                        "This is the customer's report and has not been confirmed against bank records."
                    ),
                )
            )

        # 2. Repeated references counted directly from the text.
        if reference_count and reference_count >= 2:
            findings.append(
                Finding(
                    code="payment_reference_count",
                    label="Payment references found",
                    status="info",
                    detail=f"The document references {reference_count} separate payments for this invoice.",
                )
            )

        # 3. Same invoice number already recorded by GhostLedger.
        prior = self.repository.find_invoice_payments(invoice_number, vendor)
        if prior:
            confirmed = [p for p in prior if p.get("status") == "SUCCESS"]
            pending = [p for p in prior if p.get("status") == "PENDING"]
            if confirmed:
                findings.append(
                    Finding(
                        code="invoice_already_paid",
                        label="Invoice already paid in GhostLedger",
                        status="failed",
                        detail=(
                            f"Invoice {invoice_number} was already paid {len(confirmed)} time(s) by GhostLedger. "
                            "A further payment would be a duplicate."
                        ),
                    )
                )
            if pending:
                findings.append(
                    Finding(
                        code="invoice_payment_pending",
                        label="Earlier payment still pending",
                        status="unverified",
                        detail=(
                            f"Invoice {invoice_number} has {len(pending)} payment attempt(s) in PENDING state. "
                            "The existing attempt must be investigated before any new payment."
                        ),
                    )
                )

        # 4. Same document already processed (identical file).
        duplicates = analysis.validation.duplicate_matches
        if duplicates:
            findings.append(
                Finding(
                    code="document_duplicate",
                    label="Document seen before",
                    status="unverified",
                    detail=(
                        f"This document matches {len(duplicates)} previously processed record(s). "
                        "It may be a resubmission of the same invoice."
                    ),
                )
            )

        # 5. Amount consistency: is the invoice itself duplicated in value?
        if total is not None and paid is not None and total > 0:
            if paid >= total * 2 and paid == total * (paid // total):
                findings.append(
                    Finding(
                        code="amount_multiple",
                        label="Payment amount is a multiple of the invoice total",
                        status="unverified",
                        detail=(
                            f"Reported payments {_money(paid, currency)} are a whole multiple of the "
                            f"invoice total {_money(total, currency)}."
                        ),
                    )
                )
            else:
                findings.append(
                    Finding(
                        code="amount_single",
                        label="Payment amount matches a single invoice",
                        status="passed",
                        detail=f"Reported payments {_money(paid, currency)} do not indicate repeated payment.",
                    )
                )

        resolved = not any(f.status in {"failed", "unverified"} for f in findings)
        needs_bank = any(f.code in {"repeated_payment", "invoice_already_paid", "invoice_payment_pending"} for f in findings)

        summary = (
            "No evidence of duplicate billing or duplicate payment was found."
            if resolved
            else "A possible duplicate was identified; bank verification is required before any refund or further payment."
        )

        return AgentReport(
            agent=self.agent,
            agent_name=AGENT_LABELS[self.agent],
            summary=summary,
            findings=findings,
            recommended_next=AgentId.agent_2 if needs_bank else None,
            next_action=(
                "Verify the identified payments against bank records."
                if needs_bank
                else "No duplicate follow-up required."
            ),
            requires_human=not resolved,
            blocking=not resolved,
            resolved=resolved,
            data={
                "invoice_number": invoice_number,
                "vendor": vendor,
                "currency": currency,
                "invoice_total": str(total) if total is not None else None,
                "reported_payments": str(paid) if paid is not None else None,
                "potential_duplicate_amount": str(duplicate_amount) if duplicate_amount is not None else None,
                "payment_reference_count": reference_count,
                "prior_payments": prior,
                "refund_issued": False,
                "note": "Agent 1 never issues refunds. A human decides any corrective payment.",
            },
            started_at=started,
            completed_at=utc_now(),
        )
