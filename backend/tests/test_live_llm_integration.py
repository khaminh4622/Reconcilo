"""Opt-in integration coverage for a real OpenAI-compatible model request.

This is the only test that contacts a provider. It exists to prove the
application genuinely calls a pretrained model rather than returning canned
data, so it asserts on the live response: a provider-assigned response ID, token
usage, and model output that reflects the semantics of the input document.

Run deliberately, after setting the configured LLM_* variables:

    $env:RUN_LIVE_LLM_TEST = "1"
    python -m pytest -m integration -v

It is skipped by default so the normal suite never sends data or incurs usage.
"""

from __future__ import annotations

import os

import pytest

from agent3.document_reader import read_pdf_upload
from agent3.extractor import (
    LLMExtractor,
    OllamaNativeClient,
    OpenAICompatibleClient,
    build_extractor_from_environment,
)
from agent3.schemas import AnalysisSource, ProblemType
from agent3.synthetic_data import reference_customers

from tools.generate_fixture_pdfs import fixture_documents


pytestmark = pytest.mark.integration

REQUIRED = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")

skip_without_credentials = pytest.mark.skipif(
    os.getenv("RUN_LIVE_LLM_TEST") != "1" or not all(os.getenv(name) for name in REQUIRED),
    reason="Set RUN_LIVE_LLM_TEST=1 and LLM_API_KEY, LLM_BASE_URL, LLM_MODEL to run a real provider request.",
)


@skip_without_credentials
def test_configured_llm_makes_a_real_structured_request():
    """A real request must return a provider response with real token usage."""

    extractor = build_extractor_from_environment()

    assert isinstance(extractor, LLMExtractor), "LLM is not configured; cannot run the live test."
    client = extractor.client
    # Either a hosted OpenAI-compatible provider or a local Ollama server is fine.
    assert isinstance(client, (OpenAICompatibleClient, OllamaNativeClient))
    assert client.model == os.getenv("LLM_MODEL")

    result = extractor.extract(
        "[Page 1]\nCustomer support note: I already paid invoice INV-LIVE-9 on 2026-02-01, "
        "but it still says unpaid and the portal shows an outstanding balance of 410.40 USD.\n"
        "[Page 2]\nReminder: Invoice Number: INV-LIVE-9, Total Due: 410.40, Payment Status: Unpaid"
    )

    # Proof the response came back from a provider, not from a fixture.
    assert client.last_response_id, "No provider response ID was recorded for the request."
    usage = extractor.last_usage
    assert usage is not None, "No token usage was reported; the call may have been stubbed."
    assert (usage.get("total_tokens") or 0) > 0

    assert result.analysis_source == AnalysisSource.live_llm
    # The model must actually understand the reported problem, not just the type.
    assert result.detected_problem.type in {
        ProblemType.payment_status_discrepancy,
        ProblemType.unpaid_invoice,
    }
    assert result.detected_problem.source_evidence
    assert result.financial_information.total_amount is not None


@skip_without_credentials
def test_live_model_reads_the_generated_dispute_pdf_end_to_end():
    """Upload a generated PDF and verify the model's semantics on real text."""

    from agent3.database import DocumentRepository
    from agent3.service import Agent3Service

    extractor = build_extractor_from_environment()
    assert isinstance(extractor, LLMExtractor)

    # Use a workspace-local database so the developer's data is untouched.
    from pathlib import Path

    repository = DocumentRepository(Path(".test-scratch") / "live-integration.db")
    repository.seed_customer_references(reference_customers())
    service = Agent3Service(repository, extractor)

    document = read_pdf_upload(
        "payment_dispute.pdf", fixture_documents()["payment_dispute.pdf"]["content"]
    )
    result = service.analyze(document)

    assert result.analysis_source == AnalysisSource.live_llm
    assert result.source.page_count == 2
    assert result.detected_problem.type == ProblemType.payment_status_discrepancy
    assert result.detected_problem.page == 1
    # The model named the customer that the PDF actually shows.
    assert (result.customer.customer_name or "").casefold() == "avery nguyen"
