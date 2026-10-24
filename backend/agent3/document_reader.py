"""Safe text extraction for accepted customer uploads."""

from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass

import pymupdf


class DocumentReadError(ValueError):
    pass


class OCRRequiredError(DocumentReadError):
    """Raised when a PDF has no embedded text layer to send to the LLM."""


class DocumentTooLargeError(DocumentReadError):
    """Raised when extracted text would exceed the configured prompt budget."""


SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".csv", ".json"}
DEFAULT_MAX_TEXT_CHARS = 1_000_000


def max_text_chars() -> int:
    """Prompt-size budget, configurable through MAX_TEXT_CHARS."""

    try:
        value = int(os.getenv("MAX_TEXT_CHARS", str(DEFAULT_MAX_TEXT_CHARS)))
    except ValueError:
        return DEFAULT_MAX_TEXT_CHARS
    return value if value > 0 else DEFAULT_MAX_TEXT_CHARS


@dataclass(frozen=True)
class ReadDocument:
    text: str
    file_hash: str
    filename: str | None
    media_type: str | None
    page_count: int | None
    pages: tuple[str, ...] = ()


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentReadError("The uploaded text file could not be decoded.")


def _ensure_text(text: str) -> str:
    text = text.replace("\x00", "").strip()
    if not text:
        raise DocumentReadError("The submitted document is empty.")
    limit = max_text_chars()
    if len(text) > limit:
        raise DocumentTooLargeError(
            f"The document text exceeds the {limit:,}-character analysis limit. "
            "Split it into smaller documents before uploading."
        )
    return text


def read_upload(filename: str, content: bytes, media_type: str | None = None) -> ReadDocument:
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in SUPPORTED_EXTENSIONS:
        raise DocumentReadError("Unsupported file type. Upload PDF, TXT, CSV, or JSON.")
    file_hash = hashlib.sha256(content).hexdigest()
    try:
        if extension == ".pdf":
            with pymupdf.open(stream=content, filetype="pdf") as pdf:
                pages = tuple(page.get_text("text").replace("\x00", "").strip() for page in pdf)
            if not any(pages):
                raise OCRRequiredError(
                    "This PDF has no extractable text and needs OCR. Image-only/scanned PDFs are not supported yet."
                )
            # Page markers give the model an auditable way to return evidence
            # locations without claiming to have seen anything outside the PDF.
            text = _ensure_text("\n\n".join(f"[Page {index}]\n{page}" for index, page in enumerate(pages, start=1)))
            page_count = len(pages)
        elif extension == ".csv":
            # Keep Pandas scoped to CSV work so text/PDF paths are independent.
            import pandas as pd
            frame = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
            text = _ensure_text(frame.to_csv(index=False))
            page_count = None
        elif extension == ".json":
            parsed = json.loads(_decode_text(content))
            if not isinstance(parsed, (dict, list)):
                raise DocumentReadError("JSON must contain an object or array.")
            text = _ensure_text(json.dumps(parsed, indent=2, ensure_ascii=False))
            page_count = None
        else:
            text = _ensure_text(_decode_text(content))
            page_count = None
    except DocumentReadError:
        raise
    except Exception as exc:
        raise DocumentReadError("The uploaded document could not be read.") from exc
    return ReadDocument(
        text=text,
        file_hash=file_hash,
        filename=filename,
        media_type=media_type,
        page_count=page_count,
        pages=pages if extension == ".pdf" else (),
    )


def read_pdf_upload(filename: str, content: bytes, media_type: str | None = None) -> ReadDocument:
    """Read a user-submitted PDF.

    The public document-understanding workflow is intentionally PDF-only.  The
    broader ``read_upload`` helper remains available for the fictional corpus
    and direct unit fixtures, so it does not accidentally turn a test fixture
    into a claim that production analysed it with an LLM.
    """

    if not filename.lower().endswith(".pdf"):
        raise DocumentReadError("Agent 3 accepts PDF documents only.")
    return read_upload(filename, content, media_type)


def read_plain_text(text: str) -> ReadDocument:
    clean = _ensure_text(text)
    return ReadDocument(
        text=clean,
        file_hash=hashlib.sha256(clean.encode("utf-8")).hexdigest(),
        filename=None,
        media_type="text/plain",
        page_count=None,
        pages=(),
    )
