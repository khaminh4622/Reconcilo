"""Agent 4's LLM investigation report.

The model writes the *narrative* of an exception investigation. It never decides
the outcome: the failure code, the settlement status and the retry decision all
come from the database and the Mock Bank.

Three kinds of statement must stay visibly separate, so the report is built from
a facts object that labels every item:

    customer_claims   what the PDF says (not verified)
    bank_facts        what the Mock Bank or payment store actually recorded
    unknown           what nobody could establish

The model is told it may not move an item between those categories. A guard
rejects any reply that promotes a claim to a fact, or invents a failure reason
the records do not contain.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from agent3.extractor import ExtractionError, LLMExtractor

SYSTEM_PROMPT = """You are Agent 4, the payment-exception specialist in GhostLedger.

You are given VERIFIED FACTS gathered by the system. Your job is to write a clear
investigation report for a finance operations human.

Absolute rules:
- Use ONLY the facts provided. Never add an amount, reference, code, date or
  party that is not in the facts.
- Keep the three categories strictly separate:
    * CUSTOMER CLAIMS: what the customer said. Never call these verified.
    * VERIFIED BANK FACTS: what the bank or payment records actually show.
    * UNKNOWN: things no record establishes.
- If there is no failed bank transaction, say the claim cannot yet be verified.
  Do not invent a reason such as "insufficient funds" or "invalid account".
- A failure_reason_code is the ONLY acceptable source for a root cause. If it is
  absent, the root cause must be stated as unknown.
- Never promise a refund, never claim a payment will be retried, and never say a
  payment settled unless the facts say settlement_status is SETTLED.
- Be specific and useful. Plain prose, no markdown headings, no preamble.

Respond as JSON with exactly these keys:
{
  "customer_problem": "one sentence describing what the customer reported",
  "investigation_summary": "what GhostLedger checked: records, bank ledger, attempts",
  "bank_findings": "what the Mock Bank actually shows, or that it has no record",
  "root_cause": "the cause, ONLY if a failure_reason_code is present; else 'Unknown - the records do not establish a cause'",
  "recommended_next_steps": "concrete, ordered steps for a human",
  "customer_explanation": "what to tell the customer, honest about uncertainty",
  "human_review_required": true or false,
  "evidence_gaps": ["anything that could not be established"]
}
"""


@dataclass
class InvestigationFacts:
    """Everything Agent 4 knows, separated by how strongly it is known."""

    # Customer claims, taken from the PDF. Never verified by themselves.
    customer_claims: dict = field(default_factory=dict)
    # Facts read from the database, the payment store and the Mock Bank.
    bank_facts: dict = field(default_factory=dict)
    # Explicit gaps.
    unknown: list = field(default_factory=list)
    # Deterministic conclusions the model must respect.
    failure_reason_code: str | None = None
    settlement_status: str | None = None
    has_failed_bank_transaction: bool = False
    retry_permitted: bool = False
    requires_human: bool = True

    def to_prompt(self) -> str:
        return json.dumps(
            {
                "customer_claims": self.customer_claims,
                "verified_bank_facts": self.bank_facts,
                "unknown": self.unknown,
                "failure_reason_code": self.failure_reason_code,
                "settlement_status": self.settlement_status,
                "has_failed_bank_transaction": self.has_failed_bank_transaction,
                "retry_permitted": self.retry_permitted,
                "requires_human": self.requires_human,
            },
            indent=2,
            default=str,
        )


REQUIRED_KEYS = (
    "customer_problem",
    "investigation_summary",
    "bank_findings",
    "root_cause",
    "recommended_next_steps",
    "customer_explanation",
)

# Phrases that would turn an unverified claim into a fact.
UNSAFE_WHEN_NO_BANK_EVIDENCE = (
    "insufficient funds",
    "insufficient balance",
    "invalid account",
    "invalid recipient",
    "account closed",
    "payment rejected",
    "was rejected by the bank",
    "bank timeout",
    "timed out",
    "the bank declined",
)

# Phrases that would promise something Agent 4 cannot offer. A statement such as
# "no automatic retry will occur" is correct and must not be flagged, so each
# pattern requires the promise to be asserted rather than denied.
FORBIDDEN_PROMISE_PATTERNS = (
    r"(?<!no )(?<!without )(?<!not )\bwe will retry\b",
    r"(?<!not )\bwill be retried automatically\b",
    r"(?<!never )(?<!not )(?<!no )\bautomatically retry the payment\b",
    r"(?<!not )(?<!no )\bwe will refund\b",
    r"\brefund has been issued\b",
    r"\bmoney has been returned to you\b",
    r"\bfunds have been returned to you\b",
    r"(?<!not )\bpayment has been retried\b",
)


def _clean(value) -> str:
    return " ".join(str(value or "").split()).strip()


class Agent4ReportWriter:
    """Builds the Agent 4 narrative with the configured LLM."""

    def __init__(self, extractor):
        self.extractor = extractor

    @property
    def available(self) -> bool:
        return isinstance(self.extractor, LLMExtractor)

    # ------------------------------------------------------------------
    def write(self, facts: InvestigationFacts) -> dict | None:
        """Return the report dict, or None when unavailable or unsafe."""

        if not self.available:
            return None
        try:
            reply = self.extractor.client.complete(SYSTEM_PROMPT, facts.to_prompt())
        except ExtractionError:
            return None
        except Exception:  # noqa: BLE001
            return None

        report = self._parse(reply)
        if report is None:
            return None
        if not self._is_safe(report, facts):
            return None
        return report

    # ------------------------------------------------------------------
    @staticmethod
    def _parse(reply: str) -> dict | None:
        text = (reply or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            # Some models wrap the JSON in prose; take the outermost object.
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
            except ValueError:
                return None
        if not isinstance(data, dict):
            return None

        report: dict = {}
        for key in REQUIRED_KEYS:
            report[key] = _clean(data.get(key))
        report["human_review_required"] = bool(data.get("human_review_required", True))
        gaps = data.get("evidence_gaps") or []
        report["evidence_gaps"] = [
            _clean(g) for g in gaps if _clean(g)
        ] if isinstance(gaps, list) else []

        # A report with no substance is not worth showing.
        if len(report["investigation_summary"]) < 20 or len(report["bank_findings"]) < 10:
            return None
        return report

    @staticmethod
    def _is_safe(report: dict, facts: InvestigationFacts) -> bool:
        """Reject any report that overstates what the evidence supports."""

        blob = " ".join(
            _clean(report.get(k)) for k in REQUIRED_KEYS
        ).casefold()

        # 1. No invented failure reason when no bank evidence exists.
        if not facts.has_failed_bank_transaction:
            for phrase in UNSAFE_WHEN_NO_BANK_EVIDENCE:
                if phrase in blob:
                    return False

        # 2. A root cause may only be asserted when a code exists.
        root = _clean(report.get("root_cause")).casefold()
        if not facts.failure_reason_code:
            unknown_markers = ("unknown", "not established", "no record", "cannot be determined",
                               "not available", "not supported", "insufficient evidence")
            if not any(marker in root for marker in unknown_markers):
                return False

        # 3. Never promise a retry or a refund.
        for pattern in FORBIDDEN_PROMISE_PATTERNS:
            if re.search(pattern, blob):
                return False

        # 4. Never claim settlement unless the facts say so.
        if (facts.settlement_status or "").upper() != "SETTLED":
            if "has settled" in blob or "payment settled successfully" in blob:
                return False

        # 5. Human review must be requested whenever the facts require it.
        if facts.requires_human and report.get("human_review_required") is False:
            return False

        return True


def build_report_writer(extractor) -> Agent4ReportWriter:
    return Agent4ReportWriter(extractor)
