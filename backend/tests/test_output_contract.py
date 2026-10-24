"""The public JSON contract required by the Agent 3 specification.

These tests assert on the serialized response, not on Python objects, because
the contract is what an integration consumer actually receives.
"""

from __future__ import annotations

import json

from agent3.database import DocumentRepository
from agent3.document_reader import read_pdf_upload
from agent3.extractor import LLMExtractor
from agent3.service import Agent3Service
from agent3.synthetic_data import reference_customers

from tools.generate_fixture_pdfs import fixture_documents


REQUIRED_TOP_LEVEL = {
    "document_type",
    "customer_information",
    "financial_information",
    "detected_problem",
    "validation",
    "missing_information",
    "requires_review",
}

REQUIRED_PROBLEM = {"type", "description", "source_evidence", "status"}
REQUIRED_VALIDATION = {"financial_consistency", "customer_verification", "unverified_information"}


class _Client:
    def __init__(self, payload: dict):
        self.payload = payload

    def complete(self, system: str, user: str) -> str:
        return json.dumps(self.payload)


def _analyze(workspace_tmp_path, payload: dict) -> dict:
    repository = DocumentRepository(workspace_tmp_path / "c.db")
    repository.seed_customer_references(reference_customers())
    service = Agent3Service(repository, LLMExtractor(_Client(payload)))
    pdf = fixture_documents()["normal_invoice.pdf"]["content"]
    return service.analyze(read_pdf_upload("normal_invoice.pdf", pdf)).model_dump(mode="json")


PAYLOAD = {
    "document_type": "invoice",
    "document_id": "INV-2026-0441",
    "customer": {"customer_name": "Avery Nguyen", "customer_id": "CUST-001"},
    "financial_information": {
        "currency": "USD",
        "subtotal": "400.00",
        "discount": "20.00",
        "tax_amount": "30.40",
        "total_amount": "410.40",
        "amount_paid": "0.00",
        "outstanding_balance": "410.40",
    },
    "detected_problem": {"type": "none_identified"},
}


def test_serialized_result_has_every_required_top_level_field(workspace_tmp_path):
    payload = _analyze(workspace_tmp_path, PAYLOAD)
    assert REQUIRED_TOP_LEVEL <= set(payload)


def test_detected_problem_and_validation_have_required_nested_fields(workspace_tmp_path):
    payload = _analyze(workspace_tmp_path, PAYLOAD)
    assert REQUIRED_PROBLEM <= set(payload["detected_problem"])
    assert REQUIRED_VALIDATION <= set(payload["validation"])


def test_customer_information_matches_the_extracted_customer(workspace_tmp_path):
    payload = _analyze(workspace_tmp_path, PAYLOAD)
    assert payload["customer_information"]["customer_name"] == "Avery Nguyen"
    assert payload["customer_information"]["customer_id"] == "CUST-001"


def test_amounts_serialize_as_strings_not_floats(workspace_tmp_path):
    payload = _analyze(workspace_tmp_path, PAYLOAD)
    financial = payload["financial_information"]
    assert financial["total_amount"] == "410.40"
    assert isinstance(financial["total_amount"], str)


def test_validation_reports_verified_and_unverified_separately(workspace_tmp_path):
    payload = _analyze(workspace_tmp_path, PAYLOAD)
    validation = payload["validation"]
    # Identity was checked against stored reference data; it did not come from the model.
    assert validation["customer_verification"] == "matched"
    # Math was recomputed independently of the model.
    assert validation["financial_consistency"] == "passed"
    # The model's own claim corroboration is reported, never trusted silently.
    assert isinstance(validation["unverified_information"], list)


def test_missing_information_is_a_list_and_present(workspace_tmp_path):
    payload = _analyze(
        workspace_tmp_path,
        {
            "document_type": "invoice",
            "missing_information": ["customer email", "due date"],
            "detected_problem": {"type": "none_identified"},
        },
    )
    assert isinstance(payload["missing_information"], list)
    assert "customer email" in payload["missing_information"]
