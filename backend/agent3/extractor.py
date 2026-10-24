"""LLM-backed and conservative offline extraction implementations."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .prompts import SYSTEM_PROMPT, user_prompt
from .schemas import (
    AnalysisSource,
    AnalysisResult,
    CustomerInformation,
    DetectedProblem,
    DocumentType,
    ExtractionPayload,
    FieldEvidence,
    FinancialInformation,
    LLMCallMetadata,
    LineItem,
    PrimaryProblem,
    ProblemStatus,
    ProblemType,
    TransactionInformation,
    VendorInformation,
)


class ExtractionError(RuntimeError):
    """An LLM or its response could not be used safely."""


class LLMUnavailableError(ExtractionError):
    """The required configured LLM cannot be used for this request."""


class CompletionClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class OpenAICompatibleClient:
    """Thin adapter around the official OpenAI-compatible Python SDK."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        response_format_mode: str = "json_schema",
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - environment configuration
            raise ExtractionError("OpenAI-compatible client dependency is unavailable.") from exc
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model
        self.response_format_mode = response_format_mode
        self.last_response_id: str | None = None
        self.last_usage: dict[str, int] | None = None

    def _response_format(self) -> dict[str, Any]:
        if self.response_format_mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": "customer_document_understanding",
                    "strict": True,
                    "schema": extraction_json_schema(),
                },
            }
        if self.response_format_mode == "json_object":
            return {"type": "json_object"}
        raise ExtractionError("Unsupported LLM response format configuration.")

    def complete(self, system: str, user: str) -> str:
        # Resolve the request shape before the try block: a misconfiguration is
        # not a provider failure and must not be reported as one.
        response_format = self._response_format()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                response_format=response_format,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            self.last_response_id = getattr(response, "id", None)
            self.last_usage = _usage_summary(getattr(response, "usage", None))
            content = response.choices[0].message.content
        except Exception as exc:  # API errors must not leak document data
            raise ExtractionError(_provider_error(exc)) from exc
        if not content:
            raise ExtractionError("The configured extraction provider returned an empty result.")
        return content


class OllamaNativeClient:
    """Client for a local Ollama server, using its native ``/api/chat`` API.

    Ollama's OpenAI-compatible ``/v1`` endpoint cannot disable reasoning for
    hybrid "thinking" models such as qwen3: their entire token budget is spent
    in the ``reasoning`` field and ``content`` comes back empty. The native
    endpoint accepts ``think: false``, so it is used instead whenever the
    configured base URL points at a local Ollama server.

    ``format: "json"`` makes Ollama constrain and validate the reply as JSON,
    which replaces the structured-output mode that only hosted providers offer.
    """

    def __init__(self, model: str, host: str, timeout: float = 300.0, num_predict: int = 2048):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.num_predict = num_predict
        self.last_response_id: str | None = None
        self.last_usage: dict[str, int] | None = None

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url=f"{self.host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            # Critical: without this a thinking model returns empty content.
            "think": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": 0, "num_predict": self.num_predict},
        }
        try:
            data = self._post("/api/chat", payload)
        except Exception as exc:  # network/HTTP errors must not leak document data
            raise ExtractionError(_provider_error(exc)) from exc

        self.last_response_id = data.get("created_at") or data.get("model")
        prompt_tokens = data.get("prompt_eval_count")
        completion_tokens = data.get("eval_count")
        if isinstance(prompt_tokens, int) or isinstance(completion_tokens, int):
            self.last_usage = {
                name: value
                for name, value in (
                    ("prompt_tokens", prompt_tokens),
                    ("completion_tokens", completion_tokens),
                    ("total_tokens", (prompt_tokens or 0) + (completion_tokens or 0)),
                )
                if isinstance(value, int)
            }

        message = data.get("message") or {}
        content = message.get("content") or ""
        if not content.strip():
            # Reasoning models can still report an empty answer when the limit
            # is hit. Say so precisely instead of reporting a generic failure.
            reason = data.get("done_reason")
            detail = " (the model used its whole output budget before answering)" if reason == "length" else ""
            raise ExtractionError(f"The local Ollama model returned an empty result{detail}.")
        return content


def _usage_summary(usage: Any) -> dict[str, int] | None:
    """Reduce a provider usage object to flat integer token counts."""

    if usage is None:
        return None
    summary: dict[str, int] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(field) if isinstance(usage, dict) else getattr(usage, field, None)
        if isinstance(value, int) and not isinstance(value, bool):
            summary[field] = value
    return summary or None


def _provider_error(exc: Exception) -> str:
    """Describe a provider failure without echoing document content.

    Only the exception type and, where the provider supplies one, its HTTP
    status are reported. Provider messages can quote request bodies, so they
    are intentionally not forwarded.
    """

    status = getattr(exc, "status_code", None)
    kind = type(exc).__name__
    if status:
        return f"The configured LLM provider request failed with HTTP {status} ({kind})."
    return f"The configured LLM provider could not be reached ({kind})."


def extraction_json_schema() -> dict[str, Any]:
    """Return a strict-schema-compatible form of the Pydantic extraction model."""

    schema = ExtractionPayload.model_json_schema()

    def normalize(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["additionalProperties"] = False
                node["required"] = list(properties.keys())
            for child in node.values():
                normalize(child)
        elif isinstance(node, list):
            for child in node:
                normalize(child)

    normalize(schema)
    return schema


def _string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


# Documents routinely print a placeholder where a real value belongs. Echoing
# one back would turn "we do not know who this customer is" into a fabricated
# customer identity, so these are treated as absent.
_PLACEHOLDERS = frozenset(
    {
        "-", "--", "---", "—", "n/a", "na", "nan", "none", "null", "nil", "unknown",
        "not provided", "not available", "not applicable", "not specified",
        "not stated", "not given", "tbd", "to be determined", "pending", "?",
        "no customer", "unavailable", "unspecified",
    }
)


def _is_placeholder(text: str) -> bool:
    normalized = " ".join(text.split()).casefold().rstrip(".:;")
    return normalized in _PLACEHOLDERS


# Matches money-shaped figures anywhere in the document: optional currency
# symbol, optional sign or surrounding parentheses, thousands separators, and a
# decimal part. Used to accept a model's normalised number ("2700.00") when the
# page prints it as "$2,700.00".
_AMOUNT_PATTERN = re.compile(
    r"(?P<open_paren>\(\s*)?"
    r"(?P<sign>[-\u2212+])?\s*"
    r"[$\u20ac\u00a3\u00a5]?\s*"
    r"(?P<value>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*\)?"
)


def _normalize_whitespace(text: str) -> str:
    """Collapse a PDF excerpt to single spaces for comparison.

    PDF text extraction inserts line breaks at the page width, so a sentence
    quoted verbatim from the visible document can differ from the extracted
    text only in its wrapping. Comparing whitespace-normalized forms keeps the
    grounding check strict about wording while tolerant of layout.
    """

    return " ".join(text.split())


def _contains_excerpt(source_text: str, excerpt: str) -> bool:
    """True when the excerpt appears in the document, ignoring line wrapping."""

    if not excerpt:
        return False
    if excerpt in source_text:
        return True
    return _normalize_whitespace(excerpt).casefold() in _normalize_whitespace(source_text).casefold()


def _sourced_string(value: Any, field: str, source_text: str, errors: list[str]) -> str | None:
    """Return a model value only when the PDF text itself supports it.

    A printed placeholder such as "Not provided" is treated as an absent value
    and reported, never as a real field value.
    """

    raw = _string(value)
    if raw is not None and _is_placeholder(raw):
        errors.append(f"{field} was a printed placeholder and is treated as missing.")
        return None
    if raw and not _contains_excerpt(source_text, raw):
        errors.append(f"Extracted {field} was not present in the supplied document and was discarded.")
        return None
    return raw


def _decimal(value: Any, field: str, errors: list[str]) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        errors.append(f"Malformed value for {field} was discarded.")
        return None
    text = str(value).strip().replace(",", "")
    text = re.sub(r"^[A-Za-z$€£¥]+\s*", "", text)
    text = text.replace("(", "-").replace(")", "")
    try:
        amount = Decimal(text)
        if not amount.is_finite():
            raise InvalidOperation
        return amount
    except (InvalidOperation, ValueError):
        errors.append(f"Malformed value for {field} was discarded.")
        return None


def _sourced_decimal(value: Any, field: str, source_text: str, errors: list[str]) -> Decimal | None:
    """Parse a number only when the document really contains that amount.

    A small model routinely normalises printed figures: the PDF says
    "$2,700.00" and the model answers "2700.00". Comparing the raw strings would
    discard a correct extraction, and the discarded value would then make
    validation silently skip its arithmetic. Amounts are therefore compared by
    numeric value against every number found in the document text, which keeps
    the grounding guarantee (the figure must be printed) without depending on
    currency symbols, thousands separators, or trailing zeros.
    """

    amount = _decimal(value, field, errors)
    if amount is None:
        return None
    document_amounts = _document_amounts(source_text)
    if not document_amounts:
        # No parseable numbers at all: fall back to a literal presence check.
        raw = _string(value)
        if raw and not _contains_excerpt(source_text, raw):
            errors.append(f"Extracted {field} was not present in the supplied document and was discarded.")
            return None
        return amount
    if amount in document_amounts:
        return amount
    errors.append(f"Extracted {field} ({value}) was not present in the supplied document and was discarded.")
    return None


def _document_amounts(source_text: str) -> set[Decimal]:
    """Return every amount-like number printed in the document.

    Handles currency symbols, thousands separators, a leading or trailing sign,
    and parenthesised negatives such as "(612.40)". Quantities and references
    are naturally excluded because only decimal figures are collected.
    """

    amounts: set[Decimal] = set()
    for match in _AMOUNT_PATTERN.finditer(source_text):
        raw = match.group("value")
        negative = match.group("open_paren") is not None or (
            match.group("sign") is not None and match.group("sign") in {"-", "\u2212"}
        )
        cleaned = raw.replace(",", "").replace("\u202f", "").replace("\u00a0", "")
        try:
            amount = Decimal(cleaned)
        except InvalidOperation:
            continue
        amounts.add(-amount if negative else amount)
    return amounts


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _document_type(value: Any, errors: list[str]) -> DocumentType:
    normalized = (_string(value) or "other").lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "purchaseorder": "purchase_order",
        "refund": "refund_request",
        "payment": "payment_record",
        "statement": "bank_statement",
        "report": "financial_report",
        "unknown": "other",
    }
    normalized = aliases.get(normalized, normalized)
    try:
        return DocumentType(normalized)
    except ValueError:
        errors.append("Unrecognized document type was classified as other.")
        return DocumentType.other


def _string_list(value: Any, field: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"Malformed value for {field} was discarded.")
        return []
    output: list[str] = []
    for item in value:
        text = _string(item)
        if text:
            output.append(text)
        else:
            errors.append(f"Malformed value for {field} was discarded.")
    return output


def _source_excerpt_list(value: Any, field: str, source_text: str, errors: list[str]) -> list[str]:
    """Keep only reported conflict evidence that can be traced to the PDF text."""

    excerpts = _string_list(value, field, errors)
    verified: list[str] = []
    for excerpt in excerpts:
        if _contains_excerpt(source_text, excerpt):
            verified.append(excerpt)
        else:
            errors.append(f"Unsupported {field} evidence was discarded.")
    return verified


def _locate_excerpt(source_text: str, excerpt: str) -> int:
    """Return the offset of the excerpt in the document, or -1.

    Exact matches are preferred; otherwise the match is located on a
    whitespace-normalized copy so line wrapping does not hide it.
    """

    position = source_text.find(excerpt)
    if position >= 0:
        return position
    target = _normalize_whitespace(excerpt).casefold()
    if not target:
        return -1
    # Build a normalized copy that records where each character came from.
    normalized_chars: list[str] = []
    origins: list[int] = []
    previous_space = False
    for index, character in enumerate(source_text):
        if character.isspace():
            if previous_space or not normalized_chars:
                continue
            normalized_chars.append(" ")
            origins.append(index)
            previous_space = True
        else:
            normalized_chars.append(character.casefold())
            origins.append(index)
            previous_space = False
    haystack = "".join(normalized_chars)
    found = haystack.find(target)
    return origins[found] if found >= 0 else -1


def _page_for_excerpt(source_text: str, excerpt: str | None) -> int | None:
    if not excerpt:
        return None
    position = _locate_excerpt(source_text, excerpt)
    if position < 0:
        return None
    markers = list(re.finditer(r"\[Page\s+(\d+)\]", source_text[: position + 1]))
    return int(markers[-1].group(1)) if markers else None


# Language cues used only when a model describes a problem but omits its type.
# Order matters: a payment dispute is reported more specifically than a bare
# unpaid balance, so the more specific patterns are tested first.
_PROBLEM_CUES: tuple[tuple[ProblemType, tuple[str, ...]], ...] = (
    (
        ProblemType.payment_status_discrepancy,
        (
            "already paid", "have paid", "has paid", "i paid", "still says unpaid",
            "still shows unpaid", "still marked unpaid", "but it still says",
            "not credited", "cannot trace", "can't trace", "not reflected",
            "shows unpaid", "remains unpaid", "payment was made", "money left my account",
        ),
    ),
    (
        ProblemType.refund_request,
        ("refund", "money back", "reimburse", "return my money", "chargeback"),
    ),
    (
        ProblemType.incorrect_transaction,
        (
            "duplicate charge", "charged twice", "double charge", "wrong amount",
            "incorrect amount", "overcharged", "undercharged", "wrong charge", "misapplied",
        ),
    ),
    (ProblemType.unpaid_invoice, ("unpaid", "overdue", "past due", "amount due", "outstanding")),
    (
        ProblemType.financial_document_question,
        ("could you clarify", "please explain", "question about the charge", "please confirm the amount"),
    ),
)


def _infer_problem_type(raw_problem: dict[str, Any], source_text: str) -> ProblemType | None:
    """Infer a problem type from the model's description when it omits one.

    Small local models sometimes fill in ``description`` but leave out ``type``.
    Silently reporting ``none_identified`` would discard a correctly understood
    problem, so the description and the cited evidence are matched against
    explicit language cues. A problem is never invented: when nothing matches,
    the caller keeps requires-clarification behaviour.
    """

    haystack = " ".join(
        part for part in (_string(raw_problem.get("description")), _string(raw_problem.get("source_evidence")))
        if part
    ).casefold()
    if not haystack:
        return None
    for problem_type, cues in _PROBLEM_CUES:
        if any(cue in haystack for cue in cues):
            return problem_type
    # A substantive description with no cue is still a reported financial issue.
    return ProblemType.other_financial_issue


def _problem_type(value: Any, errors: list[str]) -> ProblemType:
    normalized = (_string(value) or "none_identified").lower().replace(" ", "_").replace("-", "_")
    try:
        return ProblemType(normalized)
    except ValueError:
        errors.append("Unrecognized detected_problem.type was classified as other_financial_issue.")
        return ProblemType.other_financial_issue


def parse_primary_problem(value: Any, errors: list[str]) -> PrimaryProblem:
    """Map the model's primary-problem classification onto the routing enum.

    Anything unrecognised becomes ``unknown`` rather than being guessed, so the
    case escalates to human clarification instead of a wrong specialist.
    """

    raw = _string(value)
    if not raw:
        return PrimaryProblem.unknown
    normalized = raw.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "duplicate_payments": "duplicate_payment",
        "duplicated_payment": "duplicate_payment",
        "double_payment": "duplicate_payment",
        "duplicate_invoices": "duplicate_invoice",
        "reconciliation": "bank_reconciliation",
        "bank_recon": "bank_reconciliation",
        "payment_discrepancy": "bank_reconciliation",
        "payment_status_discrepancy": "bank_reconciliation",
        "failed_payment": "payment_failure",
        "payment_error": "payment_failure",
        "request_payment": "payment_request",
        "pay": "payment_request",
        "none": "unknown",
        "none_identified": "unknown",
    }
    normalized = aliases.get(normalized, normalized)
    try:
        return PrimaryProblem(normalized)
    except ValueError:
        errors.append(f"Unrecognized detected_problem.primary_problem '{raw}' was treated as unknown.")
        return PrimaryProblem.unknown


def _payment_count(value: Any, errors: list[str]) -> int | None:
    """Parse how many separate payments the document reports, if stated."""

    if value is None:
        return None
    if isinstance(value, bool):
        errors.append("Malformed reported_payment_count was discarded.")
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    errors.append("Malformed reported_payment_count was discarded.")
    return None


def parse_problem_type(raw_problem: dict[str, Any], source_text: str, errors: list[str]) -> ProblemType:
    """Resolve the problem type, recovering from a model that omitted it.

    An explicit type is always honoured. When it is missing but the model
    described a problem, the type is inferred and the substitution is recorded
    in ``processing_errors`` so the result never silently looks authoritative.
    """

    supplied = _string(raw_problem.get("type"))
    if supplied:
        return _problem_type(supplied, errors)

    inferred = _infer_problem_type(raw_problem, source_text)
    if inferred is None:
        return ProblemType.none_identified
    errors.append(
        f"detected_problem.type was missing and was inferred as {inferred.value} from the model's description."
    )
    return inferred


def _problem_status(value: Any, errors: list[str]) -> ProblemStatus:
    normalized = (_string(value) or "requires_clarification").lower().replace(" ", "_").replace("-", "_")
    try:
        return ProblemStatus(normalized)
    except ValueError:
        errors.append("Unrecognized detected_problem.status requires clarification.")
        return ProblemStatus.requires_clarification


def _magnitude(amount: Decimal | None, field: str, errors: list[str]) -> Decimal | None:
    """Normalise a money field that is conventionally a positive magnitude.

    Statements often print payments as negative ledger lines, so a model can
    copy "-612.40" into ``amount_paid``. The sign is a ledger presentation
    detail, not part of the amount, so it is dropped and the change is
    recorded. Totals are deliberately left untouched: a negative total is a
    real inconsistency that validation should surface.
    """

    if amount is None or amount >= 0:
        return amount
    errors.append(f"{field} was reported as a negative ledger amount and was normalised to its magnitude.")
    return -amount


def _verified_evidence(raw_evidence: Any, source_text: str) -> list[FieldEvidence]:
    if not isinstance(raw_evidence, list):
        return []
    output: list[FieldEvidence] = []
    for candidate in raw_evidence:
        if not isinstance(candidate, dict):
            continue
        field = _string(candidate.get("field"))
        excerpt = _string(candidate.get("source_text"))
        if not field:
            continue
        available = bool(excerpt and _contains_excerpt(source_text, excerpt))
        page = _page_for_excerpt(source_text, excerpt)
        value = _string(candidate.get("value"))
        verified_value = value if value and excerpt and value.casefold() in excerpt.casefold() else None
        output.append(
            FieldEvidence(
                field=field,
                value=verified_value,
                source_text=excerpt if available else None,
                page=page if available else None,
                available=available,
            )
        )
    return output


def _auto_evidence(result: AnalysisResult, source_text: str) -> list[FieldEvidence]:
    fields: list[tuple[str, object]] = [
        ("document_id", result.document_id),
        ("vendor.vendor_name", result.vendor.vendor_name),
        ("customer.customer_name", result.customer.customer_name),
        ("issue_date", result.issue_date),
        ("due_date", result.due_date),
        ("currency", result.currency),
        ("financial_information.subtotal", result.financial_information.subtotal),
        ("financial_information.tax_amount", result.financial_information.tax_amount),
        ("financial_information.discount", result.financial_information.discount),
        ("financial_information.total_amount", result.financial_information.total_amount),
    ]
    evidence: list[FieldEvidence] = []
    for name, value in fields:
        if value is None:
            continue
        text_value = str(value)
        position = source_text.find(text_value)
        if position < 0:
            evidence.append(FieldEvidence(field=name, value=text_value, available=False))
            continue
        start = source_text.rfind("\n", 0, position) + 1
        end = source_text.find("\n", position)
        excerpt = source_text[start:] if end < 0 else source_text[start:end]
        evidence.append(
            FieldEvidence(
                field=name,
                value=text_value,
                source_text=excerpt,
                page=_page_for_excerpt(source_text, excerpt),
                available=True,
            )
        )
    return evidence


# Alternative labels a document may use for each money field. Small models miss
# fields far more often than they misread them, so when the model returns null
# the printed line is recovered deterministically from the PDF text itself.
# Longest/most specific labels come first so "total amount due" wins over
# "total" and "subtotal" is never mistaken for the grand total.
_AMOUNT_LABELS: dict[str, tuple[str, ...]] = {
    "subtotal": ("subtotal", "sub-total", "sub total", "net amount", "goods total"),
    "tax_amount": (
        "sales tax", "tax amount", "gst", "hst", "pst", "vat", "tax",
    ),
    "discount": ("discount", "credit adjustment", "less discount"),
    "total_amount": (
        # The amount the customer is actually asked to pay outranks a pre-discount
        # "grand total", so payable-style labels are tested first.
        "total payable", "amount payable", "invoice total", "total invoice",
        "total amount due", "amount due", "balance due", "total due",
        "invoice amount", "grand total", "total",
    ),
    "amount_paid": (
        "payment reported", "amount paid", "payment received", "payments received",
        "paid amount", "amount remitted", "remittance", "payment amount",
        "amount received", "paid",
    ),
    "outstanding_balance": (
        "outstanding balance", "balance outstanding", "outstanding amount",
        "remaining balance", "open balance", "balance owing", "outstanding",
    ),
}

# How to read a figure that appears on the line after its label (common in
# two-column invoice layouts).
_LABEL_LINE_PATTERN = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z /&-]{2,40}?)\s*(?:[:.=\u2026]+|\s{2,})\s*"
    r"(?P<value>[(\-$\u20ac\u00a3\u00a5]?\s*[\d,]+(?:\.\d{1,3})?\)?)"
)


def recover_amount_from_text(
    source_text: str, field: str, *, exclude: set[Decimal] | None = None
) -> tuple[Decimal, str] | None:
    """Find a money field by its printed label when the model returned null.

    Returns the amount and the exact line it came from, so the recovery is as
    auditable as a model extraction. Nothing is invented: a value is only
    returned when a label is followed by a figure on the same line.
    """

    labels = _AMOUNT_LABELS.get(field, ())
    if not labels:
        return None
    excluded = exclude or set()
    lines = [line for line in source_text.splitlines() if line.strip()]

    # Walk label priorities first so that, for example, "Total Payable" outranks
    # "Grand Total" wherever they appear in the document.
    for label_priority, candidate in enumerate(labels):
        for line in lines:
            hit = _LABEL_LINE_PATTERN.search(line)
            if not hit:
                continue
            label = " ".join(hit.group("label").lower().split())
            if not (label == candidate or label.endswith(" " + candidate)):
                continue
            amount = _decimal(hit.group("value"), field, [])
            if amount is None or amount in excluded:
                continue
            return amount, line.strip()
    return None


# ISO-4217 codes and the symbols/short names real invoices print instead.
_CURRENCY_WORDS = (
    "USD", "EUR", "GBP", "JPY", "CNY", "CAD", "AUD", "NZD", "CHF", "INR",
    "SGD", "HKD", "SEK", "NOK", "DKK", "ZAR", "BRL", "MXN", "AED", "SAR",
    "VND", "THB", "IDR", "MYR", "PHP", "KRW", "TRY", "PLN", "CZK", "HUF",
)
_CURRENCY_SYMBOLS = (
    ("\u0024", "USD"), ("\u20ac", "EUR"), ("\u00a3", "GBP"), ("\u00a5", "JPY"),
)
_CURRENCY_NAME_PATTERN = re.compile(
    r"\b(?:currency|billed in|denominated in|all amounts in)\b[^A-Za-z]{0,4}(?P<code>[A-Za-z]{3})\b",
    re.IGNORECASE,
)


def recover_currency_from_text(source_text: str) -> tuple[str, str] | None:
    """Find the document currency when the model did not report one."""

    for match in _CURRENCY_NAME_PATTERN.finditer(source_text):
        code = match.group("code").upper()
        if code in _CURRENCY_WORDS:
            return code, match.group(0).strip()
    # A plain "USD" token anywhere (e.g. "Total: 2,700.00 USD").
    for word in _CURRENCY_WORDS:
        hit = re.search(rf"\b{word}\b", source_text)
        if hit:
            line_start = source_text.rfind("\n", 0, hit.start()) + 1
            line_end = source_text.find("\n", hit.start())
            return word, source_text[line_start : line_end if line_end > 0 else len(source_text)].strip()
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in source_text:
            return code, symbol
    return None


def _resolve_amount(
    financial: dict[str, Any], field: str, source_text: str, errors: list[str]
) -> Decimal | None:
    """Take the model's figure when grounded, else recover it from a printed label.

    Order matters: a grounded model value always wins, because the model has
    the document's context. Recovery only fills genuine gaps and records every
    recovery so the result stays auditable.
    """

    dotted = f"financial_information.{field}"
    amount = _sourced_decimal(financial.get(field), dotted, source_text, errors)
    if amount is not None:
        return amount

    recovered = recover_amount_from_text(source_text, field)
    if recovered is None:
        return None
    value, line = recovered
    errors.append(f"{dotted} was missing from the model output and was recovered from the document.")
    return value


def parse_llm_payload(payload: Any, source_text: str) -> AnalysisResult:
    """Safely convert untrusted model JSON into the public Pydantic schema."""

    if not isinstance(payload, dict):
        raise ExtractionError("The extraction provider returned malformed structured data.")
    raw = _mapping(payload.get("document", payload))
    errors: list[str] = []
    customer = _mapping(raw.get("customer"))
    vendor = _mapping(raw.get("vendor"))
    financial = _mapping(raw.get("financial_information"))
    transaction = _mapping(raw.get("transaction_information"))
    currency = _string(raw.get("currency")) or _string(financial.get("currency"))
    if not currency:
        recovered_currency = recover_currency_from_text(source_text)
        if recovered_currency:
            currency, _line = recovered_currency
            errors.append("currency was missing from the model output and was recovered from the document.")
    parsed_customer = CustomerInformation(
        customer_name=_sourced_string(customer.get("customer_name"), "customer.customer_name", source_text, errors),
        customer_id=_sourced_string(customer.get("customer_id"), "customer.customer_id", source_text, errors),
        company_name=_sourced_string(customer.get("company_name"), "customer.company_name", source_text, errors),
        email=_sourced_string(customer.get("email"), "customer.email", source_text, errors),
        address=_sourced_string(customer.get("address"), "customer.address", source_text, errors),
    )
    raw_problem = _mapping(raw.get("detected_problem"))
    problem_evidence = _string(raw_problem.get("source_evidence"))
    verified_problem_evidence = (
        problem_evidence if problem_evidence and _contains_excerpt(source_text, problem_evidence) else None
    )
    if problem_evidence and not verified_problem_evidence:
        errors.append("Detected problem evidence was not present in the supplied document and was discarded.")
    detected_problem = DetectedProblem(
        type=parse_problem_type(raw_problem, source_text, errors),
        description=_string(raw_problem.get("description")),
        source_evidence=verified_problem_evidence,
        page=_page_for_excerpt(source_text, verified_problem_evidence),
        status=_problem_status(raw_problem.get("status"), errors),
        primary_problem=parse_primary_problem(raw_problem.get("primary_problem"), errors),
        raw_primary_problem=_string(raw_problem.get("primary_problem")),
        reported_payment_count=_payment_count(raw_problem.get("reported_payment_count"), errors),
    )
    if detected_problem.type != ProblemType.none_identified and not detected_problem.source_evidence:
        errors.append("Detected problem was not supported by a source excerpt and requires clarification.")
        detected_problem.status = ProblemStatus.requires_clarification

    line_items: list[LineItem] = []
    supplied_lines = raw.get("line_items")
    if supplied_lines is not None and not isinstance(supplied_lines, list):
        errors.append("Malformed line items were discarded.")
    for index, item in enumerate(supplied_lines if isinstance(supplied_lines, list) else []):
        if not isinstance(item, dict):
            errors.append(f"Malformed line item {index + 1} was discarded.")
            continue
        line_items.append(
            LineItem(
            description=_sourced_string(item.get("description"), f"line_items[{index}].description", source_text, errors),
            quantity=_sourced_decimal(item.get("quantity"), f"line_items[{index}].quantity", source_text, errors),
            unit_price=_sourced_decimal(item.get("unit_price"), f"line_items[{index}].unit_price", source_text, errors),
            total_price=_sourced_decimal(item.get("total_price"), f"line_items[{index}].total_price", source_text, errors),
            )
        )

    result = AnalysisResult(
        document_type=_document_type(raw.get("document_type"), errors),
        document_id=_sourced_string(raw.get("document_id"), "document_id", source_text, errors),
        issue_date=_sourced_string(raw.get("issue_date"), "issue_date", source_text, errors),
        due_date=_sourced_string(raw.get("due_date"), "due_date", source_text, errors),
        currency=_sourced_string(currency, "currency", source_text, errors),
        payment_terms=_sourced_string(raw.get("payment_terms"), "payment_terms", source_text, errors),
        customer=parsed_customer,
        customer_information=parsed_customer.model_copy(deep=True),
        vendor=VendorInformation(
            vendor_name=_sourced_string(vendor.get("vendor_name"), "vendor.vendor_name", source_text, errors),
            vendor_id=_sourced_string(vendor.get("vendor_id"), "vendor.vendor_id", source_text, errors),
            vendor_address=_sourced_string(vendor.get("vendor_address"), "vendor.vendor_address", source_text, errors),
            vendor_email=_sourced_string(vendor.get("vendor_email"), "vendor.vendor_email", source_text, errors),
        ),
        financial_information=FinancialInformation(
            currency=_sourced_string(currency, "financial_information.currency", source_text, errors),
            subtotal=_resolve_amount(financial, "subtotal", source_text, errors),
            tax_amount=_resolve_amount(financial, "tax_amount", source_text, errors),
            discount=_resolve_amount(financial, "discount", source_text, errors),
            total_amount=_resolve_amount(financial, "total_amount", source_text, errors),
            amount_paid=_magnitude(
                _resolve_amount(financial, "amount_paid", source_text, errors),
                "financial_information.amount_paid",
                errors,
            ),
            outstanding_balance=_magnitude(
                _resolve_amount(financial, "outstanding_balance", source_text, errors),
                "financial_information.outstanding_balance",
                errors,
            ),
            payment_status=_sourced_string(financial.get("payment_status"), "financial_information.payment_status", source_text, errors),
        ),
        transaction_information=TransactionInformation(
            transaction_id=_sourced_string(transaction.get("transaction_id"), "transaction_information.transaction_id", source_text, errors),
            transaction_date=_sourced_string(transaction.get("transaction_date"), "transaction_information.transaction_date", source_text, errors),
            transaction_amount=_sourced_decimal(transaction.get("transaction_amount"), "transaction_information.transaction_amount", source_text, errors),
            sender=_sourced_string(transaction.get("sender"), "transaction_information.sender", source_text, errors),
            receiver=_sourced_string(transaction.get("receiver"), "transaction_information.receiver", source_text, errors),
            reference_number=_sourced_string(transaction.get("reference_number"), "transaction_information.reference_number", source_text, errors),
        ),
        line_items=line_items,
        evidence=_verified_evidence(raw.get("evidence"), source_text),
        detected_problem=detected_problem,
        missing_information=_string_list(raw.get("missing_information"), "missing_information", errors),
        contradictions=_source_excerpt_list(raw.get("contradictions"), "contradictions", source_text, errors),
        information_requiring_verification=_string_list(
            raw.get("information_requiring_verification"), "information_requiring_verification", errors
        ),
        analysis_source=AnalysisSource.live_llm,
        processing_errors=errors,
    )
    if not result.evidence:
        result.evidence = _auto_evidence(result, source_text)
    return result


def _json_from_content(content: str) -> Any:
    """Parse the model reply, tolerating a fenced block or a short preamble.

    Strict JSON parsing first; then a ```json fence; then the outermost object.
    A reply that cannot yield an object is an explicit extraction failure so the
    service can report the provider as unusable rather than inventing values.
    """

    text = content.strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        text = re.sub(r"^" + re.escape(fence) + r"(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*" + re.escape(fence) + r"\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ExtractionError("The extraction provider returned invalid JSON.")


class LLMExtractor:
    def __init__(self, client: CompletionClient):
        self.client = client

    @property
    def last_usage(self) -> dict[str, int] | None:
        """Token usage reported by the most recent provider response."""

        return getattr(self.client, "last_usage", None)

    def extract(self, source_text: str) -> AnalysisResult:
        response = self.client.complete(SYSTEM_PROMPT, user_prompt(source_text))
        result = parse_llm_payload(_json_from_content(response), source_text)
        # Record that a real request happened, for audit and for the UI badge.
        result.llm_call = LLMCallMetadata(
            invoked=True,
            provider_response_id=getattr(self.client, "last_response_id", None),
            model=getattr(self.client, "model", None),
            usage=getattr(self.client, "last_usage", None),
        )
        return result


class UnavailableExtractor:
    """Makes missing LLM configuration explicit instead of fabricating a result."""

    def __init__(self, reason: str):
        self.reason = reason

    def extract(self, source_text: str) -> AnalysisResult:
        raise LLMUnavailableError(self.reason)


@dataclass
class Match:
    value: str | None
    source: str | None


class HeuristicExtractor:
    """Deterministic fixture extractor used only by synthetic tests/evaluations.

    It is deliberately not selected by ``build_extractor_from_environment``.
    Uploaded PDFs therefore never receive heuristic output masquerading as an
    LLM result when the configured model is unavailable.
    """

    @staticmethod
    def _match(text: str, pattern: str) -> Match:
        hit = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if not hit:
            return Match(None, None)
        value = hit.group(1).strip()
        line_start = text.rfind("\n", 0, hit.start()) + 1
        line_end = text.find("\n", hit.end())
        return Match(value or None, text[line_start:] if line_end < 0 else text[line_start:line_end])

    @staticmethod
    def _amount(match: Match, field: str, errors: list[str]) -> Decimal | None:
        return _decimal(match.value, field, errors) if match.value else None

    def extract(self, source_text: str) -> AnalysisResult:
        lower = source_text.lower()
        if "invoice" in lower:
            doc_type = DocumentType.invoice
        elif "receipt" in lower:
            doc_type = DocumentType.receipt
        elif "purchase order" in lower:
            doc_type = DocumentType.purchase_order
        elif "refund" in lower:
            doc_type = DocumentType.refund_request
        elif "bank statement" in lower:
            doc_type = DocumentType.bank_statement
        elif "payment record" in lower or "transaction record" in lower:
            doc_type = DocumentType.payment_record
        elif "contract" in lower or "agreement" in lower:
            doc_type = DocumentType.contract
        elif "financial report" in lower:
            doc_type = DocumentType.financial_report
        else:
            doc_type = DocumentType.other

        errors: list[str] = []
        at_line = r"(?:^|\n)\s*"
        document_id = self._match(source_text, at_line + r"(?:invoice|document|receipt|po)\s*(?:number|no\.?|#)?\s*[:#]\s*([^\n]+)")
        vendor = self._match(source_text, at_line + r"(?:vendor|seller|from)\s*:\s*([^\n]+)")
        vendor_id = self._match(source_text, at_line + r"vendor\s*id\s*:\s*([^\n]+)")
        vendor_address = self._match(source_text, at_line + r"vendor\s*address\s*:\s*([^\n]+)")
        vendor_email = self._match(source_text, at_line + r"vendor\s*email\s*:\s*([^\n]+)")
        customer = self._match(source_text, at_line + r"(?:customer|bill\s*to|buyer|to)\s*:\s*([^\n]+)")
        customer_id = self._match(source_text, at_line + r"customer\s*id\s*:\s*([^\n]+)")
        company = self._match(source_text, at_line + r"company\s*:\s*([^\n]+)")
        customer_email = self._match(source_text, at_line + r"customer\s*email\s*:\s*([^\n]+)")
        customer_address = self._match(source_text, at_line + r"customer\s*address\s*:\s*([^\n]+)")
        issue_date = self._match(source_text, at_line + r"(?:issue\s*date|invoice\s*date|date)\s*:\s*([^\n]+)")
        due_date = self._match(source_text, at_line + r"due\s*date\s*:\s*([^\n]+)")
        currency = self._match(source_text, at_line + r"currency\s*:\s*([A-Za-z]{3})")
        terms = self._match(source_text, at_line + r"payment\s*terms\s*:\s*([^\n]+)")
        subtotal = self._match(source_text, at_line + r"subtotal\s*:\s*([^\n]+)")
        tax = self._match(source_text, at_line + r"(?:tax|vat)\s*:\s*([^\n]+)")
        discount = self._match(source_text, at_line + r"discount\s*:\s*([^\n]+)")
        # Prefer an explicitly labelled document total.  The fallback deliberately
        # requires the word "due" or "amount" so a line-item label such as
        # "Line Total: 100.00" cannot be mistaken for the invoice total.
        total = self._match(source_text, at_line + r"(?:total\s*(?:due|amount)|amount\s*due)\s*:\s*([^\n]+)")
        paid = self._match(source_text, at_line + r"(?:amount\s*paid|paid)\s*:\s*([^\n]+)")
        balance = self._match(source_text, at_line + r"(?:outstanding\s*balance|balance\s*due)\s*:\s*([^\n]+)")
        payment_status = self._match(source_text, at_line + r"payment\s*status\s*:\s*([^\n]+)")
        transaction_id = self._match(source_text, at_line + r"transaction\s*id\s*:\s*([^\n]+)")
        transaction_date = self._match(source_text, at_line + r"transaction\s*date\s*:\s*([^\n]+)")
        transaction_amount = self._match(source_text, at_line + r"transaction\s*amount\s*:\s*([^\n]+)")
        sender = self._match(source_text, at_line + r"sender\s*:\s*([^\n]+)")
        receiver = self._match(source_text, at_line + r"receiver\s*:\s*([^\n]+)")
        reference_number = self._match(source_text, at_line + r"reference\s*number\s*:\s*([^\n]+)")

        parsed_customer = CustomerInformation(
            customer_name=customer.value,
            customer_id=customer_id.value,
            company_name=company.value,
            email=customer_email.value,
            address=customer_address.value,
        )
        result = AnalysisResult(
            document_type=doc_type,
            document_id=document_id.value,
            issue_date=issue_date.value,
            due_date=due_date.value,
            currency=(currency.value or "").upper() or None,
            payment_terms=terms.value,
            customer=parsed_customer,
            customer_information=parsed_customer.model_copy(deep=True),
            vendor=VendorInformation(
                vendor_name=vendor.value, vendor_id=vendor_id.value, vendor_address=vendor_address.value,
                vendor_email=vendor_email.value,
            ),
            financial_information=FinancialInformation(
                currency=(currency.value or "").upper() or None,
                subtotal=self._amount(subtotal, "financial_information.subtotal", errors),
                tax_amount=self._amount(tax, "financial_information.tax_amount", errors),
                discount=self._amount(discount, "financial_information.discount", errors),
                total_amount=self._amount(total, "financial_information.total_amount", errors),
                amount_paid=self._amount(paid, "financial_information.amount_paid", errors),
                outstanding_balance=self._amount(balance, "financial_information.outstanding_balance", errors),
                payment_status=payment_status.value,
            ),
            transaction_information=TransactionInformation(
                transaction_id=transaction_id.value, transaction_date=transaction_date.value,
                transaction_amount=self._amount(transaction_amount, "transaction_information.transaction_amount", errors),
                sender=sender.value, receiver=receiver.value, reference_number=reference_number.value,
            ),
            analysis_source=AnalysisSource.synthetic_fixture,
            processing_errors=errors,
        )
        line_pattern = (
            r"(?:^|\n)\s*Line Item:\s*([^|]+?)\s*\|\s*Quantity:\s*([^|]+?)"
            r"\s*\|\s*Unit Price:\s*([^|]+?)\s*\|\s*Line\s*Total:\s*([^\n]+)"
        )
        for index, match in enumerate(re.finditer(line_pattern, source_text, re.IGNORECASE)):
            result.line_items.append(
                LineItem(
                    description=match.group(1).strip(),
                    quantity=_decimal(match.group(2), f"line_items[{index}].quantity", errors),
                    unit_price=_decimal(match.group(3), f"line_items[{index}].unit_price", errors),
                    total_price=_decimal(match.group(4), f"line_items[{index}].total_price", errors),
                )
            )
        labeled = {
            "document_id": document_id, "vendor.vendor_name": vendor, "customer.customer_name": customer,
            "issue_date": issue_date, "due_date": due_date, "currency": currency,
            "financial_information.subtotal": subtotal, "financial_information.tax_amount": tax,
            "financial_information.discount": discount, "financial_information.total_amount": total,
            "financial_information.amount_paid": paid, "financial_information.outstanding_balance": balance,
        }
        result.evidence = [
            FieldEvidence(field=field, value=match.value, source_text=match.source, available=True)
            for field, match in labeled.items()
            if match.value and match.source
        ]
        return result


def _ollama_host(base_url: str | None) -> str | None:
    """Return the Ollama server root if this base URL points at a local one.

    Ollama is normally reachable as ``http://127.0.0.1:11434/v1``. The native
    API lives one path segment up, so the trailing ``/v1`` is stripped.
    """

    if not base_url:
        return None
    match = re.match(r"^(https?://[^/]+)(?:/.*)?$", base_url.strip())
    if not match:
        return None
    host = match.group(1)
    hostname = host.split("//", 1)[-1].lower()
    if hostname.startswith("localhost") or hostname.startswith("127.0.0.1"):
        return host
    if "ollama" in hostname:
        return host
    return None


def build_extractor_from_environment() -> LLMExtractor | UnavailableExtractor:
    """Build the live configured extractor; no-key operation is explicitly unavailable."""

    model = (os.getenv("LLM_MODEL") or "").strip()
    base_url = (os.getenv("LLM_BASE_URL") or "").strip() or None
    api_key = (os.getenv("LLM_API_KEY") or "").strip()

    # A local Ollama server needs no credential, so the key is optional there.
    ollama_host = _ollama_host(base_url)
    if not model:
        return UnavailableExtractor("Live LLM analysis is unavailable. Set LLM_MODEL before analyzing a PDF.")
    if not api_key and not ollama_host:
        return UnavailableExtractor(
            "Live LLM analysis is unavailable. Set LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL."
        )

    response_format_mode = os.getenv("LLM_RESPONSE_FORMAT", "json_schema").strip().lower() or "json_schema"
    try:
        timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    except ValueError:
        timeout = 60.0

    if ollama_host:
        try:
            num_predict = int(os.getenv("LLM_MAX_TOKENS", "2048"))
        except ValueError:
            num_predict = 2048
        return LLMExtractor(
            OllamaNativeClient(model=model, host=ollama_host, timeout=timeout, num_predict=num_predict)
        )

    return LLMExtractor(
        OpenAICompatibleClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            response_format_mode=response_format_mode,
            timeout=timeout,
        )
    )
