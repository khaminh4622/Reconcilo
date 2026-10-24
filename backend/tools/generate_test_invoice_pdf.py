"""Generate a single fictional invoice PDF for manual Agent 3 testing."""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


OUTPUT = Path(__file__).resolve().parents[1] / "output" / "pdf" / "agent3_test_invoice.pdf"


def money(value: str) -> Paragraph:
    return Paragraph(value, getSampleStyleSheet()["BodyText"])


def build() -> Path:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    styles["Title"].textColor = colors.HexColor("#101D32")
    styles["Title"].fontSize = 25
    styles["Title"].leading = 30
    styles["Heading2"].textColor = colors.HexColor("#1463DC")
    styles["Heading2"].fontSize = 11
    styles["Heading2"].leading = 13
    styles["BodyText"].fontSize = 8.5
    styles["BodyText"].leading = 10.5

    story = [
        Paragraph("INVOICE", styles["Title"]),
        Paragraph("Synthetic test document - no real customer information", styles["BodyText"]),
        Spacer(1, 0.08 * inch),
    ]
    metadata = [
        [Paragraph("<b>Invoice Number:</b> INV-TEST-1001", styles["BodyText"])],
        [Paragraph("<b>Issue Date:</b> 2026-10-23", styles["BodyText"])],
        [Paragraph("<b>Due Date:</b> 2026-11-22", styles["BodyText"])],
        [Paragraph("<b>Currency:</b> USD", styles["BodyText"])],
        [Paragraph("<b>Payment Terms:</b> Net 30", styles["BodyText"])],
        [Paragraph("<b>Payment Status:</b> unpaid", styles["BodyText"])],
    ]
    metadata_table = Table(metadata, colWidths=[5.10 * inch])
    metadata_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F7FB")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#DCE4EF")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DCE4EF")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [metadata_table, Spacer(1, 0.08 * inch)]

    story += [
        Paragraph("Bill To", styles["Heading2"]),
        Paragraph(
            "Customer: Avery Nguyen<br/>Customer ID: CUST-TEST-001<br/>"
            "Company: Aurora Studio 01<br/>Customer Email: avery.nguyen@example.test<br/>"
            "Customer Address: 100 Lantern Avenue, Aurora",
            styles["BodyText"],
        ),
        Spacer(1, 0.05 * inch),
        Paragraph("Vendor", styles["Heading2"]),
        Paragraph(
            "Vendor: Blue Finch Supplies<br/>Vendor ID: VEND-TEST-001<br/>"
            "Vendor Address: 20 Market Street, Fairview<br/>"
            "Vendor Email: billing@bluefinchsupplies.test",
            styles["BodyText"],
        ),
        Spacer(1, 0.08 * inch),
        Paragraph("Line Items", styles["Heading2"]),
    ]

    items = [[Paragraph(
        "<b>Line Item:</b> Fictional consulting service | <b>Quantity:</b> 2.00 | "
        "<b>Unit Price:</b> 500.00 | <b>Line Total:</b> 1000.00",
        styles["BodyText"],
    )]]
    items_table = Table(items, colWidths=[5.10 * inch])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#DCE4EF")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DCE4EF")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [items_table, Spacer(1, 0.08 * inch)]

    totals = [
        [Paragraph("Subtotal: 1000.00", styles["BodyText"])],
        [Paragraph("Discount: 0.00", styles["BodyText"])],
        [Paragraph("Tax: 80.00", styles["BodyText"])],
        [Paragraph("<b>Total Due: 1080.00</b>", styles["BodyText"])],
        [Paragraph("Amount Paid: 0.00", styles["BodyText"])],
        [Paragraph("Outstanding Balance: 1080.00", styles["BodyText"])],
    ]
    totals_table = Table(totals, colWidths=[2.80 * inch], hAlign="RIGHT")
    totals_table.setStyle(TableStyle([
        ("LINEABOVE", (0, 3), (-1, 3), 0.8, colors.HexColor("#1463DC")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#DCE4EF")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DCE4EF")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [totals_table, Spacer(1, 0.08 * inch)]
    story.append(Paragraph("Reference Number: REF-TEST-1001", styles["BodyText"]))
    story.append(Paragraph("This document is fictional and intended solely for Agent 3 testing.", styles["BodyText"]))

    document = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=letter,
        leftMargin=0.70 * inch,
        rightMargin=0.70 * inch,
        topMargin=0.50 * inch,
        bottomMargin=0.50 * inch,
        title="GhostLedger Agent 3 Test Invoice",
        author="GhostLedger",
    )
    document.build(story)
    return OUTPUT


if __name__ == "__main__":
    print(build())
