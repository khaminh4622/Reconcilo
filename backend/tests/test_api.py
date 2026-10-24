"""API tests for the public PDF-only Agent 3 workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from fastapi import HTTPException

from agent3.database import DocumentRepository
from agent3.document_reader import read_pdf_upload
from agent3.extractor import ExtractionError, LLMExtractor
from agent3.service import Agent3Service
from agent3.synthetic_data import create_synthetic_dataset, load_synthetic_dataset
from main import create_app


def make_pdf(*pages: str) -> bytes:
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(72, 72, 540, 760), text, fontsize=10, lineheight=1.2)
    content = document.tobytes()
    document.close()
    return content


INVOICE_TEXT = """INVOICE
Invoice Number: INV-100
Vendor: Blue Finch Supplies
Customer: Avery Nguyen
Customer ID: CUST-NEW-0001
Customer Email: avery@example.test
Issue Date: 2026-01-10
Due Date: 2026-02-09
Currency: USD
Subtotal: 100.00
Tax: 8.00
Discount: 0.00
Total Due: 108.00
Amount Paid: 0.00
Outstanding Balance: 108.00
"""


class MockLLMClient:
    """Mock only the boundary to the remote provider, never the service flow."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return json.dumps(self.payload)


def mock_invoice_extractor() -> tuple[LLMExtractor, MockLLMClient]:
    client = MockLLMClient(
        {
            "document_type": "invoice",
            "document_id": "INV-100",
            "issue_date": "2026-01-10",
            "due_date": "2026-02-09",
            "currency": "USD",
            "customer": {"customer_name": "Avery Nguyen"},
            "vendor": {"vendor_name": "Blue Finch Supplies"},
            "financial_information": {
                "currency": "USD",
                "subtotal": "100.00",
                "tax_amount": "8.00",
                "discount": "0.00",
                "total_amount": "108.00",
                "amount_paid": "0.00",
                "outstanding_balance": "108.00",
                "payment_status": "unpaid",
            },
            "evidence": [{"field": "document_id", "value": "INV-100", "source_text": "Invoice Number: INV-100", "page": 1}],
            "detected_problem": {"type": "none_identified", "status": "requires_clarification"},
            "missing_information": [],
            "contradictions": [],
            "information_requiring_verification": [],
        }
    )
    return LLMExtractor(client), client


def make_app(workspace_tmp_path: Path, extractor=None):
    dataset = workspace_tmp_path / "synthetic"
    create_synthetic_dataset(dataset, count=9)
    selected_extractor, _ = mock_invoice_extractor() if extractor is None else (extractor, None)
    service = Agent3Service(DocumentRepository(workspace_tmp_path / "records.db"), selected_extractor)
    return create_app(service=service, dataset_directory=dataset)


def endpoint(app, path: str):
    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)


class InMemoryUpload:
    def __init__(self, filename: str, content: bytes, media_type: str):
        self.filename = filename
        self.content = content
        self.content_type = media_type

    async def read(self, size: int = -1) -> bytes:
        return self.content if size < 0 else self.content[:size]


def complete(coroutine):
    """Run endpoint coroutines that await only the in-memory upload once."""
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    raise AssertionError("Endpoint unexpectedly awaited external work.")


def test_pdf_analysis_uses_configured_extractor_and_preserves_page_metadata(workspace_tmp_path):
    extractor, client = mock_invoice_extractor()
    app = make_app(workspace_tmp_path, extractor)
    analyze = endpoint(app, "/agent3/analyze")

    result = complete(analyze(InMemoryUpload("invoice.pdf", make_pdf(INVOICE_TEXT), "application/pdf")))

    assert len(client.calls) == 1
    assert "[Page 1]" in client.calls[0][1]
    assert result.analysis_source.value == "live_llm"
    assert result.source.page_count == 1
    assert result.evidence[0].page == 1
    assert result.validation.financial_consistency == "passed"


def test_duplicate_pdf_is_flagged(workspace_tmp_path):
    app = make_app(workspace_tmp_path)
    analyze = endpoint(app, "/agent3/analyze")
    content = make_pdf(INVOICE_TEXT)
    first = complete(analyze(InMemoryUpload("invoice.pdf", content, "application/pdf")))
    second = complete(analyze(InMemoryUpload("invoice.pdf", content, "application/pdf")))

    assert first.validation.possible_duplicate is False
    assert second.validation.possible_duplicate is True


@pytest.mark.parametrize(
    ("filename", "content"),
    [("invoice.txt", INVOICE_TEXT.encode()), ("statement.exe", b"not a pdf")],
)
def test_non_pdf_upload_is_rejected(workspace_tmp_path, filename, content):
    app = make_app(workspace_tmp_path)
    analyze = endpoint(app, "/agent3/analyze")

    with pytest.raises(HTTPException) as error:
        complete(analyze(InMemoryUpload(filename, content, "application/octet-stream")))

    assert error.value.status_code == 400
    assert "PDF" in error.value.detail


def test_scanned_or_empty_pdf_reports_ocr_requirement(workspace_tmp_path):
    app = make_app(workspace_tmp_path)
    analyze = endpoint(app, "/agent3/analyze")

    with pytest.raises(HTTPException) as error:
        complete(analyze(InMemoryUpload("scan.pdf", make_pdf(""), "application/pdf")))

    assert error.value.status_code == 400
    assert "OCR" in error.value.detail


class FailingExtractor:
    def extract(self, source_text):
        raise ExtractionError("provider failure")


def test_llm_api_failure_returns_explicit_unavailable_result(workspace_tmp_path):
    app = make_app(workspace_tmp_path, FailingExtractor())
    analyze = endpoint(app, "/agent3/analyze")

    result = complete(analyze(InMemoryUpload("invoice.pdf", make_pdf(INVOICE_TEXT), "application/pdf")))

    assert result.analysis_source.value == "unavailable"
    assert result.validation.status == "extraction_failed"
    # The concrete provider failure is surfaced, and no synthetic data is offered.
    assert len(result.processing_errors) == 1
    assert "provider failure" in result.processing_errors[0]
    assert "No synthetic or heuristic result" in result.processing_errors[0]
    assert result.customer.customer_name is None
    assert result.financial_information.total_amount is None


def test_customer_claim_is_matched_only_against_an_existing_reference_record(workspace_tmp_path):
    repository = DocumentRepository(workspace_tmp_path / "records.db")
    profile = repository.add_customer_reference(
        customer_id="CUST-NEW-0001",
        customer_name="Avery Nguyen",
        email="avery@example.test",
    )
    extractor, _ = mock_invoice_extractor()
    # The mock represents a document claim containing the already-stored ID.
    extractor.client.payload["customer"] = {
        "customer_name": "Avery Nguyen",
        "customer_id": profile.customer_id,
        "email": "avery@example.test",
    }
    service = Agent3Service(repository, extractor)

    result = service.analyze(read_pdf_upload("invoice.pdf", make_pdf(INVOICE_TEXT)))

    assert result.validation.customer_verification == "matched"
    assert "customer identity" not in result.validation.unverified_information


def test_synthetic_corpus_is_preserved_for_non_live_evaluation(workspace_tmp_path):
    output = workspace_tmp_path / "dataset"
    create_synthetic_dataset(output, count=9)
    manifest = load_synthetic_dataset(output)

    assert manifest["count"] == 9
    assert manifest["count"] == len(manifest["records"])
    assert all((output / "documents" / record["filename"]).is_file() for record in manifest["records"])


