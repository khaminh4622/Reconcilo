"""Optional LLM narrative for Agent 1 investigations.

The LLM may rewrite the *prose* of an investigation so it reads naturally. It
never produces the status, the score, or any financial figure - those come from
``agent1_engine``. If the model is unavailable, slow, or returns something
unusable, the deterministic summary is kept and the investigation is unaffected.
"""

from __future__ import annotations

from agent3.extractor import ExtractionError, LLMExtractor

from .agent1_schemas import DuplicateInvestigation

SYSTEM_PROMPT = """You are explaining an invoice duplicate investigation to a
finance operations human.

You are given facts that a deterministic system has already established. Your
only job is to summarise them clearly in two or three sentences.

Rules:
- Do not change the outcome, the status, the match score, or any amount.
- Do not say an invoice is duplicated unless the status says so.
- Do not claim a payment settled unless the facts say it was verified.
- Do not add any fact that is not in the data.
- Write plainly, for a busy human. No preamble, no bullet points, plain prose.
"""


def build_prompt(investigation: DuplicateInvestigation) -> str:
    best = investigation.best_match
    lines = [
        f"Status: {investigation.status.value}",
        f"Invoice: {investigation.invoice_number or 'unknown'}",
        f"Vendor: {investigation.vendor or 'unknown'}",
        f"Customer: {investigation.customer or 'unknown'}",
        f"Amount: {investigation.amount} {investigation.currency or ''}".strip(),
        f"Records compared: {investigation.records_compared}",
        f"History available: {investigation.history_available}",
        f"Highest match score: {investigation.highest_score}/100",
    ]
    if best is not None:
        lines.append(
            f"Closest match: {best.invoice_number or '(no number)'} from {best.vendor or 'unknown'}, "
            f"{best.amount} {best.currency or ''}, score {best.match_score}/100"
        )
        for signal in best.signals:
            lines.append(f"  - {signal.signal}: {signal.detail}")
    payment = investigation.payment_analysis
    lines.append(f"Payment conclusion: {payment.conclusion}")
    lines.append(f"Verified settlements: {payment.verified_settlements}")
    lines.append(f"Failed attempts: {payment.failed_attempts}")
    lines.append(f"Pending transactions: {payment.pending_transactions}")
    lines.append(f"Customer-reported payments: {payment.customer_reported}")
    return "\n".join(lines)


def make_summarizer(extractor) -> callable | None:
    """Build an LLM summariser bound to the configured extractor, or None.

    Returns None when no live model is configured, so callers simply fall back
    to the deterministic summary.
    """

    if not isinstance(extractor, LLMExtractor):
        return None

    def summarise(investigation: DuplicateInvestigation) -> str | None:
        try:
            reply = extractor.client.complete(SYSTEM_PROMPT, build_prompt(investigation))
        except ExtractionError:
            return None
        text = " ".join((reply or "").split()).strip()
        if not text or len(text) < 20:
            return None

        # Models sometimes wrap prose in JSON. Unwrap it so the summary reads
        # naturally, and reject anything still structured.
        if text.startswith("{"):
            try:
                import json

                parsed = json.loads(text)
                text = " ".join(str(parsed.get("summary") or parsed.get("text") or "").split())
            except Exception:  # noqa: BLE001
                return None
        if not text or len(text) < 20:
            return None

        lowered = text.casefold()
        status = investigation.status.value

        # A summary may never turn an unproven invoice into a clean bill of
        # health. If no comparable history existed, the warning must survive.
        if not investigation.history_available or investigation.records_compared == 0:
            markers = ("no comparison records", "no earlier invoice records", "cannot be confirmed unique",
                       "cannot say whether", "no records to compare")
            if not any(marker in lowered for marker in markers):
                return None

        # Reject verdicts that contradict the deterministic status.
        forbidden = {
            "no_duplicate_found": ("confirmed duplicate", "was duplicated", "is a duplicate",
                                   "cannot be confirmed unique"),
            "insufficient_information": ("is unique", "no duplicates", "no duplicate was found"),
            "suspected_duplicate_payment": ("confirmed duplicate payment", "payment was confirmed"),
            "possible_duplicate": ("confirmed duplicate",),
        }
        for phrase in forbidden.get(status, ()):
            if phrase in lowered:
                return None
        return text[:800]

    return summarise
