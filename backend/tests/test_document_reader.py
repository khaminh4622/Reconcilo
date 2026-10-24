"""PyMuPDF extraction and PDF failure-mode coverage."""

from __future__ import annotations

import pymupdf
import pytest

from agent3.document_reader import (
    DocumentReadError,
    DocumentTooLargeError,
    OCRRequiredError,
    read_pdf_upload,
)


def pdf_bytes(*pages: str) -> bytes:
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(72, 72, 540, 760), text, fontsize=11)
    output = document.tobytes()
    document.close()
    return output


def test_pymupdf_extracts_all_pdf_pages_with_auditable_page_markers():
    result = read_pdf_upload("customer.pdf", pdf_bytes("First page customer note", "Second page payment note"))

    assert result.page_count == 2
    assert result.pages == ("First page customer note", "Second page payment note")
    assert "[Page 1]" in result.text
    assert "[Page 2]" in result.text


def test_image_only_or_empty_pdf_requires_ocr():
    with pytest.raises(OCRRequiredError, match="needs OCR"):
        read_pdf_upload("scan.pdf", pdf_bytes(""))


def test_corrupted_pdf_returns_a_safe_read_error():
    with pytest.raises(DocumentReadError, match="could not be read"):
        read_pdf_upload("corrupt.pdf", b"not actually a PDF")


def test_pdf_workflow_rejects_non_pdf_file_names():
    with pytest.raises(DocumentReadError, match="PDF documents only"):
        read_pdf_upload("customer.txt", b"Customer: Avery")


def test_text_exceeding_the_configured_budget_is_rejected(monkeypatch):
    monkeypatch.setenv("MAX_TEXT_CHARS", "50")
    with pytest.raises(DocumentTooLargeError, match="character analysis limit"):
        read_pdf_upload("long.pdf", pdf_bytes("x" * 200))


def test_text_within_the_budget_is_accepted(monkeypatch):
    monkeypatch.setenv("MAX_TEXT_CHARS", "100000")
    result = read_pdf_upload("ok.pdf", pdf_bytes("A short customer note."))
    assert "short customer note" in result.text
