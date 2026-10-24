"""Agent 3 orchestration: extract a customer PDF, validate it, and persist it."""

from __future__ import annotations

from typing import Protocol

from .database import DocumentRepository
from .document_reader import ReadDocument
from .extractor import ExtractionError, LLMUnavailableError
from .schemas import (
    AnalysisSource,
    AnalysisResult,
    DetectedProblem,
    DocumentInformation,
    DocumentType,
    ProblemStatus,
    SourceMetadata,
)
from .validator import build_reasoning, decide_routing, validate_document


class Extractor(Protocol):
    def extract(self, source_text: str) -> AnalysisResult: ...


class Agent3Service:
    """Keep model extraction separate from deterministic validation decisions."""

    def __init__(self, repository: DocumentRepository, extractor: Extractor):
        self.repository = repository
        self.extractor = extractor

    def analyze(self, document: ReadDocument) -> AnalysisResult:
        source = SourceMetadata(
            filename=document.filename,
            media_type=document.media_type,
            page_count=document.page_count,
            file_hash=document.file_hash,
        )
        try:
            result = self.extractor.extract(document.text)
            result.source = source
            customer_verification, unverified = self._customer_record_verification(result)
            unverified = list(dict.fromkeys([*result.information_requiring_verification, *unverified]))
            duplicates = self.repository.find_duplicates(result, document.file_hash)
            result.validation = validate_document(
                result,
                duplicates,
                customer_verification=customer_verification,
                unverified_information=unverified,
                source_text=document.text,
            )
            # This alias is part of the public output contract.  It contains
            # LLM-extracted claims, not a confirmation of customer identity.
            result.customer_information = result.customer.model_copy(deep=True)
            result.vendor_information = result.vendor.model_copy(deep=True)
            result.document_information = DocumentInformation(
                document_type=result.document_type,
                document_number=result.document_id,
                issue_date=result.issue_date,
                due_date=result.due_date,
                currency=result.currency,
                payment_terms=result.payment_terms,
            )
            result.validation_findings = list(result.validation.validation_findings)
            result.routing = decide_routing(
                result,
                result.validation,
                result.financial_information.payment_claim,
                source_text=document.text,
            )
            # Routing may add findings (duplicate payment), so re-sync the list.
            result.validation_findings = list(result.validation.validation_findings)
            result.reasoning = build_reasoning(result, result.validation, result.routing)
            result.missing_information = list(dict.fromkeys([*result.missing_information, *result.validation.missing_fields]))
            result.requires_review = bool(
                result.validation.requires_review
                or result.contradictions
                or result.information_requiring_verification
                or result.validation.requires_reconciliation
                or result.detected_problem.type.value != "none_identified"
                or result.detected_problem.status == ProblemStatus.requires_clarification
            )
        except LLMUnavailableError as exc:
            result = AnalysisResult(
                source=source,
                analysis_source=AnalysisSource.unavailable,
                detected_problem=DetectedProblem(status=ProblemStatus.unavailable),
                processing_errors=[str(exc)],
            )
            result.validation = validate_document(result, extraction_failed=True)
        except ExtractionError as exc:
            # Do not leak provider errors or PDF content.  Most importantly,
            # never replace a failed model request with a heuristic answer.
            result = AnalysisResult(
                document_type=DocumentType.other,
                source=source,
                analysis_source=AnalysisSource.unavailable,
                detected_problem=DetectedProblem(status=ProblemStatus.unavailable),
                processing_errors=[
                    f"{exc} No synthetic or heuristic result was produced."
                ],
            )
            result.validation = validate_document(result, extraction_failed=True)
        self.repository.save(result, document.file_hash)
        return result

    def _customer_record_verification(self, result: AnalysisResult) -> tuple[str, list[str]]:
        """Match only against reference records already held in GhostLedger.

        A document claim is never considered proof by itself.  An exact stored
        customer ID may establish a match after checking any accompanying name,
        email, and company claims.  Otherwise an exact existing record candidate
        may be matched; absence of a reference record stays unverified.
        """

        customer = result.customer
        if not any((customer.customer_name, customer.customer_id, customer.email)):
            return "not_available", []
        if customer.customer_id:
            profile = self.repository.get_customer(customer.customer_id)
            if profile:
                conflicts = self._profile_conflicts(result, profile)
                return ("mismatch", conflicts) if conflicts else ("matched", [])

        candidates = self.repository.find_customer_matches(
            customer_name=customer.customer_name,
            company_name=customer.company_name,
            email=customer.email,
        )
        conflicts: list[str] = []
        for profile in candidates:
            candidate_conflicts = self._profile_conflicts(result, profile)
            if not candidate_conflicts:
                return "matched", []
            conflicts.extend(candidate_conflicts)
        if conflicts:
            return "mismatch", list(dict.fromkeys(conflicts))
        return "unverified", ["customer identity"]

    @classmethod
    def _profile_conflicts(cls, result: AnalysisResult, profile: object) -> list[str]:
        customer = result.customer
        return [
            label
            for label, supplied, recorded in (
                ("customer name", customer.customer_name, getattr(profile, "customer_name", None)),
                ("email", customer.email, getattr(profile, "email", None)),
                ("company name", customer.company_name, getattr(profile, "company_name", None)),
            )
            if supplied and recorded and not cls._same(supplied, recorded)
        ]

    @staticmethod
    def _clean(value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @classmethod
    def _same(cls, left: str | None, right: str | None) -> bool:
        clean_left, clean_right = cls._clean(left), cls._clean(right)
        return bool(clean_left and clean_right and clean_left.casefold() == clean_right.casefold())
