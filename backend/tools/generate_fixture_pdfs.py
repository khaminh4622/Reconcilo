"""Generate fictional customer PDFs used to exercise Agent 3 end to end.

Every document here is invented for development. No real customer, company, or
account data appears in it. The generator depends only on PyMuPDF so it runs in
the project virtual environment without any extra packages.

Usage (from ``ghostledger/backend``):

    python tools/generate_fixture_pdfs.py
"""

from __future__ import annotations

from pathlib import Path

import pymupdf


OUTPUT = Path(__file__).resolve().parents[1] / "fixtures" / "pdfs"
PAGE = pymupdf.paper_rect("letter")
MARGIN = 54


def _write_page(document: pymupdf.Document, lines: list[str]) -> None:
    """Write one page of text.

    ``Page.insert_textbox`` returns the unused vertical space: negative means
    the text did not fit. A negative result means the fixture text is too long
    for the page, which is a bug in the fixture and is reported loudly rather
    than silently truncated.
    """

    page = document.new_page()
    box = pymupdf.Rect(MARGIN, MARGIN, PAGE.width - MARGIN, PAGE.height - MARGIN)
    unused = page.insert_textbox(box, "\n".join(lines), fontsize=10, lineheight=1.35)
    if unused < 0:
        raise RuntimeError("Fixture text does not fit on a single page; shorten it.")


def build_pdf(pages: list[list[str]]) -> bytes:
    document = pymupdf.open()
    for lines in pages:
        _write_page(document, lines)
    content = document.tobytes()
    document.close()
    return content


# --------------------------------------------------------------------------
# 1. A normal, internally consistent invoice.
# --------------------------------------------------------------------------
NORMAL_INVOICE = [
    "INVOICE",
    "Harborline Office Supply (fictional vendor)",
    "Invoice Number: INV-2026-0441",
    "Issue Date: 2026-03-02",
    "Due Date: 2026-04-01",
    "Currency: USD",
    "Payment Terms: Net 30",
    "Customer: Avery Nguyen",
    "Customer ID: CUST-001",
    "Company: Aurora Studio 01",
    "Customer Email: avery.nguyen1@example.test",
    "Customer Address: 100 Lantern Avenue, Aurora",
    "Line Item: Ergonomic desk chair | Quantity: 2 | Unit Price: 180.00 | Line Total: 360.00",
    "Line Item: Replacement arm pads | Quantity: 2 | Unit Price: 20.00 | Line Total: 40.00",
    "Subtotal: 400.00",
    "Discount: 20.00",
    "Tax: 30.40",
    "Total Due: 410.40",
    "Amount Paid: 0.00",
    "Outstanding Balance: 410.40",
    "Payment Status: Unpaid",
]

# --------------------------------------------------------------------------
# 2. Customer payment dispute written in natural language, multi-page.
# --------------------------------------------------------------------------
DISPUTE_PAGE_ONE = [
    "CUSTOMER SUPPORT NOTE",
    "Ticket: SUPPORT-88213",
    "Date: 2026-04-14",
    "Received from: avery.nguyen1@example.test",
    "Customer: Avery Nguyen",
    "Customer ID: CUST-001",
    "Company: Aurora Studio 01",
    "",
    "Hello, I already paid invoice INV-2026-0441 on 2026-03-20 by bank transfer.",
    "The reference on the transfer was TRF-7741290 and the amount was 410.40 USD.",
    "Today I received another reminder saying the invoice is still unpaid.",
    "The portal still shows an outstanding balance of 410.40 even though the",
    "money left my account 25 days ago. Could someone please look into this?",
    "",
    "I am concerned that I will be charged twice or referred to collections.",
]

DISPUTE_PAGE_TWO = [
    "ATTACHED REMINDER - PAGE 2",
    "Reminder for Invoice Number: INV-2026-0441",
    "Original Issue Date: 2026-03-02",
    "Due Date: 2026-04-01",
    "Currency: USD",
    "Total Due: 410.40",
    "Amount Paid: 0.00",
    "Outstanding Balance: 410.40",
    "Payment Status: Unpaid",
    "Remark: Second notice. Please remit by 2026-05-01 to avoid a late fee.",
]

# --------------------------------------------------------------------------
# 3. A refund request.
# --------------------------------------------------------------------------
REFUND_REQUEST = [
    "REFUND REQUEST",
    "Request Reference: RF-2026-0078",
    "Request Date: 2026-05-06",
    "Customer: Dana Whitfield",
    "Customer Email: dana.whitfield@example.test",
    "Company: Loomer Craft Collective",
    "Order Reference: ORD-55901",
    "Currency: USD",
    "Amount Charged: 89.50",
    "Requested Refund Amount: 89.50",
    "",
    "On 2026-05-01 I was charged 89.50 for the order ORD-55901 but the shipment",
    "was returned to the sender because the address was wrong. I never received",
    "the goods. I am requesting a full refund of 89.50 to the original card.",
    "I have attached the return receipt. Please confirm once processed.",
]

# --------------------------------------------------------------------------
# 4. Missing customer information.
# --------------------------------------------------------------------------
MISSING_CUSTOMER_INFO = [
    "INVOICE",
    "Invoice Number: INV-2026-0612",
    "Issue Date: 2026-06-01",
    "Due Date: 2026-07-01",
    "Currency: USD",
    "Customer: -",
    "Customer ID: Not provided",
    "Company:",
    "Customer Email:",
    "",
    "Line Item: Annual support plan | Quantity: 1 | Unit Price: 1200.00 | Line Total: 1200.00",
    "Subtotal: 1200.00",
    "Tax: 0.00",
    "Total Due: 1200.00",
    "Amount Paid: 0.00",
    "Outstanding Balance: 1200.00",
    "Payment Status: Unpaid",
    "",
    "This invoice was generated automatically. Billing contact details were not",
    "available in the workspace configuration at the time of issue.",
]

# --------------------------------------------------------------------------
# 5. Contradictory financial information.
# --------------------------------------------------------------------------
CONTRADICTORY_TOTALS = [
    "INVOICE",
    "Invoice Number: INV-2026-0733",
    "Issue Date: 2026-07-05",
    "Due Date: 2026-06-20",
    "Currency: USD",
    "Customer: Marcus Ibe",
    "Customer Email: marcus.ibe@example.test",
    "Company: Kestrel Logistics",
    "Line Item: Freight handling | Quantity: 3 | Unit Price: 100.00 | Line Total: 300.00",
    "Subtotal: 900.00",
    "Tax: 72.00",
    "Discount: 0.00",
    "Total Due: 1062.00",
    "Amount Paid: 200.00",
    "Outstanding Balance: 500.00",
    "Payment Status: Partially Paid",
    "",
    "Note from billing clerk: the subtotal above was copied from the previous",
    "quarter's contract and may not reflect this month's three shipments.",
    "The customer was told the amount owed is 500.00. Please reconcile before",
    "sending a statement; the figures on this page do not agree with each other.",
]

# --------------------------------------------------------------------------
# 6. A document the system has never seen: no invoice-like structure.
# --------------------------------------------------------------------------
UNKNOWN_DOCUMENT = [
    "FIELD SERVICE REPORT",
    "Reference: FSR-AA-11",
    "Site inspected: Emerson Way warehouse (fictional)",
    "Technician notes follow.",
    "",
    "Ambient temperature recorded at 21 C during the walkthrough.",
    "The mezzanine stair handrail has two loose anchor bolts near the landing.",
    "Lighting in aisle nine flickers intermittently and should be replaced.",
    "No water ingress observed in the north bay. Drainage channel is clear.",
    "Recommended follow-up visit within 30 days to confirm the repairs.",
    "",
    "There are no financial amounts, customer identifiers, or payment terms",
    "on this form.",
]

# --------------------------------------------------------------------------
# 7. Previously unseen phrasing / layout, presented as a letter.
# --------------------------------------------------------------------------
UNSEEN_LETTER = [
    "Selby & Crane Accounts Desk",
    "PO Box 4412, Riverton (fictional)",
    "",
    "12 August 2026",
    "",
    "Dear Ms Okafor,",
    "",
    "We are writing about statement ST-2026-88 issued to your business.",
    "Our records show a carried-forward balance of 2,340.00 GBP from July",
    "and a further 780.00 GBP of charges for August, giving 3,120.00 GBP in total.",
    "",
    "Your office advised our team on 3 August that 2,340.00 GBP had already been",
    "settled by standing order. We cannot trace that payment on our side, so the",
    "statement has been issued showing the balance as fully outstanding.",
    "",
    "Please send us the bank advice slip so we can trace it. If the funds were in",
    "fact received we will correct the statement and close the query.",
    "",
    "Kind regards,",
    "J. Crane, Accounts Desk",
]


def fixture_documents() -> dict[str, dict]:
    """Return every fixture as name -> {pages, bytes, description}."""

    definitions = {
        "normal_invoice.pdf": ([NORMAL_INVOICE], "A normal, internally consistent invoice."),
        "payment_dispute.pdf": ([DISPUTE_PAGE_ONE, DISPUTE_PAGE_TWO], "Customer says a paid invoice still shows unpaid."),
        "refund_request.pdf": ([REFUND_REQUEST], "Customer asks for a refund of a charge."),
        "missing_customer_information.pdf": ([MISSING_CUSTOMER_INFO], "An invoice with no customer identifying fields."),
        "contradictory_financials.pdf": ([CONTRADICTORY_TOTALS], "Totals that cannot all be correct."),
        "unknown_document_type.pdf": ([UNKNOWN_DOCUMENT], "A document with no financial content at all."),
        "unseen_letter.pdf": ([UNSEEN_LETTER], "Unfamiliar letter phrasing and layout."),
    }
    output: dict[str, dict] = {}
    for filename, (pages, description) in definitions.items():
        output[filename] = {
            "pages": pages,
            "content": build_pdf(pages),
            "description": description,
        }
    return output


def generate(directory: Path = OUTPUT) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for filename, document in fixture_documents().items():
        path = directory / filename
        path.write_bytes(document["content"])
        written[filename] = path
    # A deliberately corrupt upload, for the reader failure path.
    corrupt = directory / "corrupted.pdf"
    corrupt.write_bytes(b"%PDF-1.7\nthis is not a real pdf body\n%%EOF")
    written["corrupted.pdf"] = corrupt
    return written


if __name__ == "__main__":
    for name, path in generate().items():
        print(f"{name:40s} {path.stat().st_size:>7d} bytes")

