from decimal import Decimal

import pytest

from agent3.schemas import AnalysisResult, DocumentType, FinancialInformation, VendorInformation
from agent3.validator import validate_document


def valid_invoice() -> AnalysisResult:
    return AnalysisResult(
        document_type=DocumentType.invoice,
        document_id="INV-1",
        issue_date="2026-01-10",
        currency="USD",
        vendor=VendorInformation(vendor_name="Blue Finch Supplies"),
        financial_information=FinancialInformation(
            currency="USD",
            subtotal=Decimal("100.00"),
            tax_amount=Decimal("8.00"),
            discount=Decimal("0.00"),
            total_amount=Decimal("108.00"),
        ),
    )


def test_incorrect_financial_total_requires_review():
    invoice = valid_invoice()
    invoice.financial_information.total_amount = Decimal("110.00")
    result = validate_document(invoice)
    assert any(item.code == "financial_total_mismatch" for item in result.inconsistencies)
    assert result.status == "requires_review"


def test_invalid_date_requires_review():
    invoice = valid_invoice()
    invoice.issue_date = "2026-99-99"
    result = validate_document(invoice)
    assert any(item.code == "invalid_issue_date" for item in result.inconsistencies)
    assert result.status == "requires_review"


@pytest.mark.parametrize(
    "supplied",
    [
        "2026-09-14",
        "2026/09/14",
        "14 Sep 2026",
        "14 September 2026",
        "14 Sep. 2026",
        "Sep 14, 2026",
        "September 14, 2026",
        "25/03/2026",
        "  14  Sep  2026  ",
    ],
)
def test_common_real_world_date_formats_are_recognized(supplied):
    """A valid date in any ordinary format must not be reported as invalid."""

    invoice = valid_invoice()
    invoice.issue_date = supplied
    result = validate_document(invoice)
    assert not any(item.code == "invalid_issue_date" for item in result.inconsistencies)


def test_dates_with_month_names_order_correctly():
    """Due-before-issue detection works when dates use month names."""

    invoice = valid_invoice()
    invoice.issue_date = "14 Sep 2026"
    invoice.due_date = "05 Sep 2026"
    result = validate_document(invoice)
    assert any(item.code == "due_before_issue" for item in result.inconsistencies)


@pytest.mark.parametrize("supplied", ["not a date", "2026-13-45", "Sep 2026"])
def test_unparseable_dates_are_still_rejected(supplied):
    invoice = valid_invoice()
    invoice.issue_date = supplied
    result = validate_document(invoice)
    assert any(item.code == "invalid_issue_date" for item in result.inconsistencies)
