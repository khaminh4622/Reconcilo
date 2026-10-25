"""Persistent, case-scoped support chat for Agent 4.

The assistant is a read-only layer over the existing GhostLedger workflow. It
gets bank facts only through :class:`AgentBankTools` with Agent 4's case context;
neither the LLM nor this module has a method for approving a payment, moving
money, or creating a bank transaction.

Two rules are enforced here, not merely requested of the model:

* **No invented money.** A settlement is reported only when
  ``verify_payment_settlement`` confirms both sides of the transfer.
* **No bypassed approval.** Asking Agent 4 to pay can only *prepare* a new
  request through the ordinary Agent 3 -> Agent 1 -> bank-approval workflow.
  Agent 4 cannot approve, process, retry, or settle anything.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from agent3.extractor import LLMExtractor

from .bank_tools import AgentBankTools, BankToolError, CaseContext
from .repository import WorkflowRepository
from .schemas import (
    AGENT_LABELS,
    AgentId,
    AgentRun,
    AgentRunStatus,
    CaseStatus,
    WorkflowCase,
    utc_now,
)


MAX_SUPPORT_MESSAGE_CHARS = 4_000

SUGGESTED_ACTIONS = (
    {
        "id": "check_status",
        "label": "Check payment status",
        "message": "Please check this payment's status.",
    },
    {
        "id": "help_pay",
        "label": "Help me pay this invoice",
        "message": "I haven't paid this invoice. Help me pay it.",
    },
    {
        "id": "already_paid",
        "label": "I already paid",
        "message": "I already paid, but the vendor says they did not receive it.",
    },
    {
        "id": "why_declined",
        "label": "Why was my payment declined?",
        "message": "Why was my payment declined?",
    },
    {
        "id": "human_assistance",
        "label": "Request human assistance",
        "message": "Please request human assistance for this payment.",
    },
)

# Bank request statuses that mean "do not create a second instruction".
LIVE_BANK_STATUSES = {"PENDING_APPROVAL", "APPROVED", "PROCESSING", "UNCERTAIN"}
TERMINAL_FAILURE_STATUSES = {"FAILED", "REJECTED", "CANCELLED"}

BANK_STATUS_LABELS = {
    "PENDING_APPROVAL": "Awaiting bank approval",
    "APPROVED": "Approved, not processed",
    "PROCESSING": "Processing at the bank",
    "SETTLED": "Settled by the bank",
    "FAILED": "Failed at the bank",
    "REJECTED": "Rejected by the bank",
    "UNCERTAIN": "Bank outcome uncertain",
    "CANCELLED": "Cancelled",
    "MISSING": "No payment record found",
}


SYSTEM_PROMPT = """You are Agent 4, GhostLedger's payment support assistant.

Reply to the customer in no more than 180 words. Use only the CASE FACTS
provided in the user message. They came from case-scoped, read-only GhostLedger
and Mock Bank tools. Never invent an amount, payment reference, bank reason,
approval, settlement, or vendor result.

Rules:
- A payment is confirmed only when `settlement_verified` is true.
- If a bank status is pending, uncertain, missing, failed, or rejected, say it is
  not confirmed. Never tell the customer the money arrived.
- Explain a failure reason only when the bank supplied `failure_code` or
  `failure_reason`. If no reason exists, say the bank recorded none.
- You cannot approve a payment, move money, issue a refund, retry a payment, or
  create a bank settlement. Human bank approval is always required.
- If the customer asks to pay, explain the verified Agent 3 / Agent 1 / bank
  approval steps. Do not claim a payment request already exists unless the facts
  show one.
- If records are insufficient, ask for the missing payment reference, date, or
  amount, and offer human review.
- Do not expose unrelated accounts, customers, or full account numbers.
- If the facts say the case was escalated to human review, say so and explain
  why. Do not promise a resolution time.

Return plain customer-facing text only, with no headings or markdown.
"""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _label(value: str | None) -> str:
    return BANK_STATUS_LABELS.get((value or "").upper(), _clean(value) or "No payment record found")


class Agent4ConversationStore:
    """Small persistent message store in the existing GhostLedger SQLite DB."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialise(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent4_conversation_messages (
                    message_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    account_id TEXT,
                    actor TEXT NOT NULL,
                    body TEXT NOT NULL,
                    action_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            # rowid may be used for ordering, but it cannot be indexed. The
            # created_at index is enough to find a case's conversation quickly.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_agent4_conversation_case "
                "ON agent4_conversation_messages(case_id, created_at)"
            )
            conn.commit()
        finally:
            conn.close()

    def list(self, case_id: str, limit: int = 200) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT message_id, case_id, account_id, actor, body, action_id, created_at
                FROM agent4_conversation_messages WHERE case_id = ?
                ORDER BY created_at ASC, rowid ASC LIMIT ?
                """,
                (case_id, max(1, min(int(limit), 500))),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def append(
        self,
        *,
        case_id: str,
        account_id: str | None,
        actor: str,
        body: str,
        action_id: str | None = None,
    ) -> dict:
        message = {
            "message_id": str(uuid4()),
            "case_id": case_id,
            "account_id": account_id,
            "actor": actor,
            "body": _clean(body),
            "action_id": action_id,
            "created_at": utc_now(),
        }
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO agent4_conversation_messages
                (message_id, case_id, account_id, actor, body, action_id, created_at)
                VALUES (:message_id, :case_id, :account_id, :actor, :body, :action_id, :created_at)
                """,
                message,
            )
            conn.commit()
        finally:
            conn.close()
        return message


class Agent4SupportAssistant:
    """Grounded conversational facade for the existing Agent 4 specialist."""

    def __init__(self, repository: WorkflowRepository, extractor=None):
        self.repository = repository
        self.extractor = extractor
        self.messages = Agent4ConversationStore(repository.path)

    # ------------------------------------------------------------------
    # Public reads
    # ------------------------------------------------------------------
    def conversation(self, case: WorkflowCase, *, account=None) -> dict:
        evidence = self.evidence(case)
        return {
            "case_id": case.case_id,
            "invoice_number": case.invoice_number,
            "case_status": case.status.value,
            "requires_human": bool(case.requires_human),
            "payment_status": evidence,
            "messages": self.messages.list(case.case_id),
            "suggested_actions": self._suggested_actions(evidence),
            "next_steps": evidence["next_steps"],
            "escalation": self._escalation_state(case),
            "viewer": (
                {
                    "account_id": getattr(account, "account_id", None),
                    "role": getattr(account, "role", None),
                    "is_staff": bool(getattr(account, "is_admin", False)),
                }
                if account is not None
                else None
            ),
        }

    def evidence(self, case: WorkflowCase) -> dict:
        """Read only this case's authorised bank evidence through Agent 4 tools."""

        agent4_report = next(
            (report for report in reversed(case.reports) if report.agent == AgentId.agent_4),
            None,
        )
        agent1_report = next(
            (report for report in reversed(case.reports) if report.agent == AgentId.agent_1),
            None,
        )
        facts: dict = {
            "case_id": case.case_id,
            "case_status": case.status.value,
            "invoice_number": case.invoice_number,
            "vendor": case.vendor_name,
            "customer_name": case.customer_name,
            "amount": str(case.amount) if case.amount is not None else None,
            "currency": case.currency,
            "requires_human": bool(case.requires_human),
            "pending_reason": case.pending_reason,
            "agent_3": {
                "validation_status": ((case.analysis or {}).get("validation") or {}).get("status"),
                "summary": next(
                    (run.summary for run in case.agent_runs if run.agent == AgentId.agent_3),
                    None,
                ),
                "document_type": (case.analysis or {}).get("document_type"),
                "analysis_source": (case.analysis or {}).get("analysis_source"),
            },
            "agent_1": {
                "status": (agent1_report.data.get("status") if agent1_report else None),
                "summary": (agent1_report.summary if agent1_report else None),
            },
            "agent_4": {
                "summary": agent4_report.summary if agent4_report else None,
                "diagnosis": (agent4_report.data.get("diagnosis") if agent4_report else None),
                "customer_explanation": (
                    agent4_report.data.get("customer_explanation") if agent4_report else None
                ),
                "retry_permitted": (
                    agent4_report.data.get("retry_permitted") if agent4_report else None
                ),
            },
            "reconciliation": case.reconciliation or {},
            "payment_request_id": case.payment_request_id,
            "payment_history": [],
            "bank": None,
            "bank_status_label": "No payment record found",
            "settlement_verified": False,
            "has_bank_record": False,
            "retry_blocked": True,
            "can_request_payment": False,
            "reason_not_requestable": None,
            "next_steps": [],
            "simulated": True,
        }

        context = CaseContext(
            agent_id="agent_4",
            case_id=case.case_id,
            customer_id=case.owner_account_id,
            invoice_id=case.invoice_number,
            customer_name=case.customer_name,
            payee=case.vendor_name,
            currency=case.currency,
        )
        tools = AgentBankTools(self.repository.path)
        try:
            history = tools.get_case_payment_history(context)
        except BankToolError:
            history = {"requests": [], "count": 0}
            facts["bank_error"] = "Case-scoped bank evidence is unavailable."
            facts["bank_status_label"] = "Bank evidence unavailable"
        requests = history.get("requests") or []
        facts["payment_history"] = requests

        primary = self._primary_request(case, requests)
        if primary:
            facts["has_bank_record"] = True
            facts["payment_request_id"] = primary["payment_id"]
            facts["bank_status_label"] = _label(primary.get("status"))
            facts["bank"] = {
                "payment_id": primary.get("payment_id"),
                "status": primary.get("status"),
                "status_label": _label(primary.get("status")),
                "amount": primary.get("amount"),
                "currency": primary.get("currency"),
                "payee": primary.get("payee"),
                "settlement_reference": primary.get("settlement_reference"),
                "failure_code": primary.get("failure_code"),
                "failure_reason": primary.get("failure_reason"),
                "created_at": primary.get("created_at"),
                "processed_at": primary.get("processed_at"),
                "source": "Mock Bank",
                "simulated": True,
            }
            payment_context = CaseContext(
                agent_id="agent_4",
                case_id=primary.get("case_id") or case.case_id,
                customer_id=case.owner_account_id,
                invoice_id=case.invoice_number,
                customer_name=case.customer_name,
                payee=case.vendor_name,
                currency=case.currency,
            )
            try:
                status = tools.get_payment_status(primary["payment_id"], payment_context)
                exception = tools.get_payment_exception_details(primary["payment_id"], payment_context)
                verification = tools.verify_payment_settlement(primary["payment_id"], payment_context)
            except BankToolError:
                # Do not turn an authorisation failure into a fake bank result.
                facts["bank"]["status"] = "UNAVAILABLE"
                facts["bank"]["status_label"] = "Bank evidence unavailable"
                facts["bank"]["reason"] = "Case-scoped bank evidence is unavailable."
                facts["bank_error"] = facts["bank"]["reason"]
            else:
                facts["settlement_verified"] = bool(verification.get("verified"))
                facts["settlement_check"] = {
                    "verified": bool(verification.get("verified")),
                    "reason": verification.get("reason"),
                    "debit_entries": verification.get("debit_entries"),
                    "credit_entries": verification.get("credit_entries"),
                    "amounts_agree": verification.get("amounts_agree"),
                }
                facts["bank"].update(
                    {
                        "status": status.get("status") or facts["bank"].get("status"),
                        "status_label": _label(status.get("status")),
                        "settlement_reference": status.get("settlement_reference")
                        or facts["bank"].get("settlement_reference"),
                        "processed_at": status.get("processed_at")
                        or facts["bank"].get("processed_at"),
                        "failure_code": exception.get("failure_code"),
                        "failure_reason": exception.get("failure_reason"),
                        "ledger_entries": exception.get("ledger_entries"),
                        "decisions": (exception.get("decisions") or [])[-1:] or None,
                        "retry_blocked": bool(exception.get("retry_blocked")),
                        "attempts_in_case": status.get("attempts_in_case") or [],
                    }
                )
                facts["retry_blocked"] = bool(exception.get("retry_blocked"))
                facts["bank_status_label"] = facts["bank"]["status_label"]

        facts["payment_attempts"] = [
            {
                "attempt_id": attempt.attempt_id,
                "status": attempt.status,
                "provider_status": attempt.provider_status,
                "failure_code": attempt.failure_code,
                "failure_reason": attempt.failure_reason,
                "settlement_reference": attempt.settlement_reference,
                "verified": attempt.verified,
            }
            for attempt in (case.payment_history or ([case.payment] if case.payment else []))
        ]
        facts["can_request_payment"], facts["reason_not_requestable"] = self._can_request_new_payment(
            case, facts
        )
        facts["next_steps"] = self._next_steps(facts)
        return facts

    @staticmethod
    def _primary_request(case: WorkflowCase, requests: list[dict]) -> dict | None:
        if not requests:
            return None
        linked = case.payment_request_id
        if linked:
            for row in requests:
                if row.get("payment_id") == linked:
                    return row
        # Prefer the newest non-cancelled request; the tool returns newest first.
        for row in requests:
            if (row.get("status") or "").upper() != "CANCELLED":
                return row
        return requests[0]

    @staticmethod
    def _can_request_new_payment(case: WorkflowCase, facts: dict) -> tuple[bool, str | None]:
        missing = [
            label
            for label, value in (
                ("invoice number", case.invoice_number),
                ("vendor", case.vendor_name),
                ("amount", case.amount),
                ("currency", case.currency),
            )
            if value in (None, "")
        ]
        if missing:
            return False, "This case is missing the " + ", ".join(missing) + " needed for a new request."
        if facts.get("bank_error"):
            return False, "Bank evidence is unavailable, so a new payment request cannot be prepared safely."
        if case.receipt is not None or facts.get("settlement_verified"):
            return False, "This invoice already has a verified settled payment."
        bank = facts.get("bank") or {}
        status = (bank.get("status") or "").upper()
        if status in LIVE_BANK_STATUSES:
            return False, (
                "An existing payment request is still active or uncertain, so GhostLedger will not "
                "create another one."
            )
        if status == "SETTLED":
            return False, "The bank records this invoice as settled."
        if case.status in {CaseStatus.awaiting_approval, CaseStatus.approved, CaseStatus.pending}:
            return False, "This case is already waiting for a bank decision or a bank result."
        if case.status == CaseStatus.completed:
            return False, "This case is already complete."
        if status == "UNAVAILABLE":
            return False, "The bank evidence is unavailable, so a new request cannot be prepared safely."
        return True, None

    @staticmethod
    def _next_steps(facts: dict) -> list[str]:
        bank = facts.get("bank") or {}
        status = (bank.get("status") or "").upper()
        invoice = facts.get("invoice_number") or "this invoice"
        if facts.get("bank_error") or status == "UNAVAILABLE":
            return [
                "The case-scoped Mock Bank evidence is unavailable, so payment status cannot be confirmed.",
                "Do not create another payment request while the bank status is unknown.",
                "Try again later or ask Agent 4 to escalate this case for human review.",
            ]
        if facts.get("settlement_verified"):
            reference = bank.get("settlement_reference")
            return [
                f"No further payment is needed for {invoice}.",
                "Keep the receipt"
                + (f" and settlement reference {reference}" if reference else "")
                + " for your records.",
                "If the vendor still cannot trace the money, ask Agent 4 for human assistance and share the reference.",
            ]
        if status == "PENDING_APPROVAL":
            return [
                "A bank administrator must approve or decline the request.",
                "No money has moved yet. Do not send a second payment.",
                "After approval, Agent 5 submits it, Agent 2 reconciles the bank entries, and Agent 5 confirms only when both sides settle.",
            ]
        if status == "APPROVED":
            return [
                "The bank has authorised the request but has not processed it yet.",
                "Wait for the simulated bank result; do not create another request.",
                "Agent 2 will reconcile the bank entries before Agent 5 confirms anything.",
            ]
        if status == "PROCESSING":
            return [
                "The bank is processing the transfer.",
                "Do not retry while the outcome is in progress.",
                "Wait for the bank result or ask Agent 4 for human assistance if it stays stuck.",
            ]
        if status == "UNCERTAIN":
            return [
                "The bank outcome is uncertain, so the payment is not confirmed.",
                "Do not retry. A person must check the bank before another request is prepared.",
                "Ask Agent 4 for human assistance to escalate the case.",
            ]
        if status in TERMINAL_FAILURE_STATUSES:
            reason = bank.get("failure_reason") or bank.get("failure_code")
            return [
                f"The bank records the payment as {status.lower()}"
                + (f" ({reason})" if reason else " with no reason code") + ".",
                "No money moved. The failure is not retried automatically.",
                "Fix the recorded cause, then ask Agent 4 to prepare a new verified payment request; it still needs bank approval.",
            ]
        if facts.get("can_request_payment"):
            return [
                "GhostLedger has no confirmed payment for this invoice.",
                "Ask Agent 4 to prepare a new payment request. Agent 3 verifies the invoice and Agent 1 checks for duplicates.",
                "The request becomes a PENDING_APPROVAL instruction in the Mock Bank and waits for a human bank decision. No money moves until that approval and processing finish.",
            ]
        return [
            "Share any payment reference, date, amount, or payer account you have.",
            "A person can review the case if the records do not resolve it.",
            "Do not send another payment while the status is unclear.",
        ]

    def _suggested_actions(self, evidence: dict) -> list[dict]:
        actions = []
        for action in SUGGESTED_ACTIONS:
            item = dict(action)
            item["available"] = True
            item["reason"] = None
            if action["id"] == "help_pay" and not evidence.get("can_request_payment"):
                item["available"] = False
                item["reason"] = evidence.get("reason_not_requestable")
            elif action["id"] == "already_paid" and evidence.get("settlement_verified"):
                item["available"] = False
                item["reason"] = "This invoice is already confirmed as settled."
            actions.append(item)
        return actions

    @staticmethod
    def _escalation_state(case: WorkflowCase) -> dict | None:
        entries = [
            entry
            for entry in case.audit
            if entry.action in {"support_escalation", "support_escalation_reconfirmed"}
        ]
        if entries:
            latest = entries[-1]
            return {
                "escalated": True,
                "reason": latest.detail,
                "at": latest.at,
                "already_escalated": latest.action.endswith("reconfirmed"),
            }
        if case.status == CaseStatus.requires_review or case.requires_human:
            return {
                "escalated": False,
                "reason": case.pending_reason or "This case is waiting for a person to review it.",
                "at": None,
                "already_escalated": True,
            }
        return None

    # ------------------------------------------------------------------
    # Conversation
    # ------------------------------------------------------------------
    def reply(
        self,
        case: WorkflowCase,
        *,
        account_id: str,
        actor: str,
        message: str,
        action_id: str | None = None,
    ) -> dict:
        text = _clean(message)
        if not text:
            raise ValueError("Write a message for Agent 4.")
        if len(text) > MAX_SUPPORT_MESSAGE_CHARS:
            raise ValueError(f"Messages are limited to {MAX_SUPPORT_MESSAGE_CHARS:,} characters.")
        customer_message = self.messages.append(
            case_id=case.case_id,
            account_id=account_id,
            actor=actor,
            body=text,
            action_id=action_id,
        )
        evidence = self.evidence(case)
        escalation = None
        if action_id == "human_assistance" or self._requests_human(text):
            escalation = self.escalate(
                case,
                account_id=account_id,
                reason=(
                    "The customer asked Agent 4 for human assistance with "
                    f"invoice {case.invoice_number or '(unknown invoice)'}. "
                    "No bank approval was granted and no payment was retried."
                ),
            )
            evidence = self.evidence(case)
        history = self.messages.list(case.case_id, limit=40)
        answer = self._llm_reply(text, history, evidence, escalation) or self._fallback_reply(
            text, evidence, action_id, escalation
        )
        agent_message = self.messages.append(
            case_id=case.case_id,
            account_id=None,
            actor="agent_4",
            body=answer,
            action_id=action_id,
        )
        return {
            "case_id": case.case_id,
            "case_status": case.status.value,
            "requires_human": bool(case.requires_human),
            "customer_message": customer_message,
            "agent_message": agent_message,
            "payment_status": evidence,
            "suggested_actions": self._suggested_actions(evidence),
            "next_steps": evidence["next_steps"],
            "escalation": escalation,
            "tools_used": [
                "get_case_payment_history",
                "get_payment_status",
                "get_payment_exception_details",
                "verify_payment_settlement",
            ],
        }

    def escalate(
        self, case: WorkflowCase, *, account_id: str, reason: str | None = None
    ) -> dict:
        """Move an unresolved case to human review. Never touches the bank."""

        invoice = case.invoice_number or "(unknown invoice)"
        text = _clean(reason) or (
            f"Agent 4 escalated invoice {invoice} to human review at the customer's request. "
            "No bank approval was granted and no payment was retried."
        )
        already = case.status == CaseStatus.requires_review and case.requires_human
        if not already:
            case.status = CaseStatus.requires_review
            case.requires_human = True
            case.pending_reason = text
            case.outcome = text
        run = case.run_for(AgentId.human_review)
        if run is None:
            run = AgentRun(
                agent=AgentId.human_review,
                agent_name=AGENT_LABELS[AgentId.human_review],
                order=len(case.agent_runs),
            )
            case.agent_runs.append(run)
        run.status = AgentRunStatus.requires_human
        run.summary = text
        run.started_at = run.started_at or utc_now()
        case.add_audit(
            AgentId.agent_4,
            "support_escalation_reconfirmed" if already else "support_escalation",
            text,
            CaseStatus.requires_review.value,
            actor_account_id=account_id,
        )
        self.repository.save_case(case)
        return {
            "escalated": True,
            "already_escalated": already,
            "reason": text,
            "case_status": case.status.value,
            "requires_human": True,
        }

    def record_payment_intake(
        self,
        case: WorkflowCase,
        *,
        account_id: str,
        result: str,
        customer_message: str | None = None,
    ) -> None:
        """Record that Agent 4 prepared a new request, for the chat history."""

        if customer_message:
            self.messages.append(
                case_id=case.case_id,
                account_id=account_id,
                actor="customer",
                body=customer_message,
                action_id="help_pay",
            )
        self.messages.append(
            case_id=case.case_id,
            account_id=None,
            actor="agent_4",
            body=result,
            action_id="help_pay",
        )

    # ------------------------------------------------------------------
    # Reply generation
    # ------------------------------------------------------------------
    def _llm_reply(
        self,
        message: str,
        history: list[dict],
        evidence: dict,
        escalation: dict | None,
    ) -> str | None:
        if not isinstance(self.extractor, LLMExtractor):
            return None
        client = getattr(self.extractor, "client", None)
        if client is None:
            return None
        prompt = json.dumps(
            {
                "case_facts": self._prompt_facts(evidence),
                "conversation": [
                    {"actor": row["actor"], "message": row["body"]} for row in history[-14:]
                ],
                "latest_customer_message": message,
                "escalation": escalation,
                "available_suggested_actions": [
                    {"id": action["id"], "label": action["label"], "available": action["available"]}
                    for action in self._suggested_actions(evidence)
                ],
            },
            default=str,
        )
        try:
            if hasattr(client, "complete_text"):
                raw = client.complete_text(SYSTEM_PROMPT, prompt)
            else:
                # Test doubles and older clients only implement complete().
                raw = client.complete(SYSTEM_PROMPT, prompt)
        except Exception:  # noqa: BLE001 - support must retain its deterministic fallback
            return None
        answer = self._coerce_text(raw)
        if not self._safe_answer(answer, evidence):
            return None
        return answer[:2_000]

    @staticmethod
    def _prompt_facts(evidence: dict) -> dict:
        """The exact, already-authorised facts the model may use."""

        bank = evidence.get("bank") or {}
        return {
            "case_id": evidence.get("case_id"),
            "case_status": evidence.get("case_status"),
            "requires_human": evidence.get("requires_human"),
            "pending_reason": evidence.get("pending_reason"),
            "invoice_number": evidence.get("invoice_number"),
            "vendor": evidence.get("vendor"),
            "amount": evidence.get("amount"),
            "currency": evidence.get("currency"),
            "payment_confirmed": bool(evidence.get("settlement_verified")),
            "bank_status": bank.get("status"),
            "bank_status_label": bank.get("status_label"),
            "settlement_reference": bank.get("settlement_reference"),
            "failure_code": bank.get("failure_code"),
            "failure_reason": bank.get("failure_reason"),
            "retry_blocked": evidence.get("retry_blocked"),
            "payment_request_id": evidence.get("payment_request_id"),
            "payment_history": evidence.get("payment_history"),
            "agent_1": evidence.get("agent_1"),
            "agent_3": evidence.get("agent_3"),
            "agent_4": evidence.get("agent_4"),
            "reconciliation_verdict": (evidence.get("reconciliation") or {}).get("verdict"),
            "bank_evidence_error": evidence.get("bank_error"),
            "can_request_payment": evidence.get("can_request_payment"),
            "reason_not_requestable": evidence.get("reason_not_requestable"),
            "next_steps": evidence.get("next_steps"),
        }

    @staticmethod
    def _coerce_text(raw: object) -> str:
        """Accept plain prose or a JSON object returned by a JSON-only provider."""

        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json|text)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        if text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
            except ValueError:
                return ""
            if isinstance(data, dict):
                for key in ("reply", "answer", "message", "text", "response", "content"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        return _clean(value)
                strings = [value for value in data.values() if isinstance(value, str) and value.strip()]
                return _clean(strings[0]) if len(strings) == 1 else ""
            if isinstance(data, list) and all(isinstance(value, str) for value in data):
                return _clean(" ".join(data))
        return _clean(text)

    @staticmethod
    def _safe_answer(answer: str, evidence: dict) -> bool:
        if len(answer) < 8:
            return False
        bank = evidence.get("bank") or {}
        verified = bool(evidence.get("settlement_verified"))
        lower = answer.casefold()

        # Never claim money arrived without a verified settlement.
        if not verified and re.search(
            r"\b("
            r"(payment|transfer|invoice)\s+(was|is|has been|have been)\s+"
            r"(successful|successfully|completed|settled|received|paid|confirmed)"
            r"|we\s+(have\s+)?received\s+your\s+payment"
            r"|you\s+(have\s+)?paid"
            r"|(?:the\s+)?(?:vendor|recipient|payee)\s+(?:has\s+)?received\s+"
            r"(?:the\s+)?(?:payment|transfer|funds|money)"
            r"|funds\s+(?:(?:were|have been)\s+)?(?:transferred|sent|received|delivered)"
            r"|money\s+(?:has\s+been\s+)?(?:sent|transferred|received|delivered|arrived)"
            r"|(?:payment|transfer)\s+went\s+through"
            r"|(?:payment|transfer|funds)\s+arrived"
            r"|(?:funds|payment)\s+reached\s+(?:the\s+)?(?:vendor|recipient|payee)"
            r"|settled\s+successfully"
            r")\b",
            lower,
        ):
            return False

        # Never invent a bank reason when the bank supplied none.
        if not bank.get("failure_code") and not bank.get("failure_reason"):
            if re.search(
                r"\b(insufficient funds|invalid account|account closed|bank declined|"
                r"bank rejected|limit exceeded|timeout|timed out)\b",
                lower,
            ):
                return False

        # Never invent a Mock Bank reference.
        known_references = {
            str(value)
            for value in (
                bank.get("settlement_reference"),
                *(row.get("settlement_reference") for row in evidence.get("payment_history") or []),
            )
            if value
        }
        for reference in re.findall(r"\bMB-[A-Z0-9]{4,}\b", answer):
            if reference not in known_references:
                return False

        forbidden = (
            "i approved",
            "i have approved",
            "i moved the money",
            "i sent the payment",
            "i created a bank",
            "i processed the payment",
            "i retried the payment",
        )
        return not any(phrase in lower for phrase in forbidden)

    # ------------------------------------------------------------------
    # Deterministic fallback
    # ------------------------------------------------------------------
    def _fallback_reply(
        self,
        message: str,
        evidence: dict,
        action_id: str | None,
        escalation: dict | None,
    ) -> str:
        bank = evidence.get("bank") or {}
        status = (bank.get("status") or "").upper()
        invoice = evidence.get("invoice_number") or "this invoice"
        lower = message.casefold()
        money = " ".join(
            part for part in (evidence.get("amount"), evidence.get("currency")) if part
        )

        if evidence.get("bank_error") or status == "UNAVAILABLE":
            return (
                f"I can't safely verify the payment status for {invoice} because the "
                "case-scoped Mock Bank evidence is unavailable. I can't confirm that it was "
                "paid or unpaid, and I won't prepare another request while the outcome is "
                "unknown. Please try again later or ask for human assistance."
            )

        if escalation:
            return (
                f"I have escalated {invoice} to human review. The case status is now "
                "Needs a person. No payment was approved, processed, or retried. "
                "A person must check the records before any new payment or correction is made. "
                + (
                    f"The reason recorded is: {escalation['reason']}"
                    if escalation.get("reason")
                    else ""
                )
            )

        if evidence.get("settlement_verified"):
            reference = bank.get("settlement_reference")
            return (
                f"I checked the case-scoped Mock Bank evidence. {invoice} has matching settled debit "
                f"and credit entries, and the bank verifies the settlement"
                f"{f' with reference {reference}' if reference else ''}. "
                "No further payment is needed. If the vendor still cannot trace it, ask for human "
                "assistance and share the reference."
            )

        if status == "PENDING_APPROVAL":
            return (
                f"The Mock Bank has a pending payment request for {invoice}"
                + (f" for {money}" if money else "")
                + ", but no money has moved. A bank administrator must approve or decline it. "
                "After approval, Agent 5 submits it, Agent 2 reconciles the bank entries, and "
                "Agent 5 confirms only after both sides settle. Do not send a second payment."
            )
        if status == "APPROVED":
            return (
                f"The bank has approved the request for {invoice}, but it has not processed it yet. "
                "No money has moved. Agent 2 will reconcile the bank entries before Agent 5 confirms "
                "anything, so please do not create another request."
            )
        if status == "PROCESSING":
            return (
                f"The bank is still processing {invoice}. The payment is not confirmed and must not "
                "be retried. I can ask for human assistance if it stays stuck."
            )
        if status == "UNCERTAIN":
            return (
                f"The bank outcome for {invoice} is still uncertain. It is not confirmed, so it must "
                "not be retried. A person should check the bank before any new request; I can escalate "
                "this case for you."
            )
        if status in TERMINAL_FAILURE_STATUSES:
            code = bank.get("failure_code")
            reason = bank.get("failure_reason")
            detail = " — ".join(part for part in (code, reason) if part)
            return (
                f"The bank records this payment for {invoice} as {status.lower()}. "
                + (f"Its recorded reason is: {detail}. " if detail else "The bank did not record a reason. ")
                + "No money moved. GhostLedger will not retry it automatically. If you want to try again, "
                "I can prepare a new verified request, but it will need fresh bank approval."
            )
        if action_id == "help_pay" or re.search(r"\b(help me pay|haven'?t paid|have not paid|new payment)\b", lower):
            if evidence.get("can_request_payment"):
                return (
                    f"GhostLedger has no confirmed payment for {invoice}. I can prepare a new verified "
                    "request from this invoice. Agent 3 verifies the invoice and Agent 1 checks for "
                    "duplicates; if it is eligible, it becomes a PENDING_APPROVAL request in the Mock "
                    "Bank and waits for a human bank decision. No money moves without that approval."
                )
            return (
                f"I cannot prepare a new payment for {invoice} yet: "
                f"{evidence.get('reason_not_requestable') or 'the case needs human review first.'}"
            )
        if action_id == "already_paid" or re.search(r"\b(already paid|i paid|payment arrived|didn'?t receive|did not receive)\b", lower):
            return (
                f"I checked the Mock Bank records attached to {invoice}. I found no settled payment "
                "that I can verify, so I cannot confirm the vendor received it. If you have a payment "
                "reference, date, amount, or payer account, send it here. If money left your account, "
                "ask for human assistance so a person can investigate."
            )
        if action_id == "why_declined" or re.search(r"\b(declin|reject|fail|refus)", lower):
            return (
                f"The bank has no recorded decline or failure for {invoice} that I can verify, so I "
                "cannot give you a reason without inventing one. Share the payment reference or the "
                "bank's message if you have it, or ask for human assistance."
            )
        return (
            f"I checked the records attached to {invoice}. I cannot confirm a settled payment, and I "
            "will not guess. Tell me whether you are checking a payment, reporting a decline, or asking "
            "to start a new verified payment request. You can also ask for human assistance."
        )

    @staticmethod
    def _requests_human(message: str) -> bool:
        return bool(
            re.search(
                r"\b(human|person|representative|support team|staff|escalat)\b",
                message.casefold(),
            )
        )


# ---------------------------------------------------------------------------
# Payment intake analysis
# ---------------------------------------------------------------------------
def payment_intake_analysis(case: WorkflowCase):
    """Build Agent 3's verified invoice facts for a new payment request.

    Nothing is extracted from a new document here. The original case already
    holds the invoice number, vendor, amount and currency Agent 3 verified, and
    the customer's request is to pay exactly those facts. Agent 1 still runs on
    the new case and the Mock Bank still requires human approval.
    """

    from decimal import Decimal

    from agent3.schemas import (
        AnalysisResult,
        AnalysisSource,
        CustomerInformation,
        DetectedProblem,
        DocumentType,
        FinancialInformation,
        PrimaryProblem,
        ProblemStatus,
        ProblemType,
        RoutingDecision,
        VendorInformation,
    )

    amount = case.amount if isinstance(case.amount, Decimal) else Decimal(str(case.amount))
    customer = CustomerInformation(
        customer_name=case.customer_name,
        company_name=case.customer_name,
    )
    return AnalysisResult(
        document_type=DocumentType.invoice,
        document_id=case.invoice_number,
        currency=case.currency,
        customer=customer,
        customer_information=customer.model_copy(deep=True),
        vendor=VendorInformation(vendor_name=case.vendor_name),
        financial_information=FinancialInformation(
            currency=case.currency,
            subtotal=amount,
            tax_amount=Decimal("0.00"),
            total_amount=amount,
            amount_paid=Decimal("0.00"),
            outstanding_balance=amount,
            payment_status="Unpaid",
        ),
        detected_problem=DetectedProblem(
            type=ProblemType.none_identified,
            primary_problem=PrimaryProblem.payment_request,
            status=ProblemStatus.reported,
        ),
        analysis_source=AnalysisSource.synthetic_fixture,
        routing=RoutingDecision(
            recommended_agent="human_approval",
            reason=(
                "Agent 4 reused the invoice facts Agent 3 verified on the original case; "
                "the customer asked to pay this exact invoice."
            ),
            priority="normal",
            blocking=False,
        ),
        requires_review=False,
    )
