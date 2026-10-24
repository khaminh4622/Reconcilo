"""Deterministically generate fictional financial documents and ground truth.

This module deliberately contains no real people, accounts, or external data.
"""

from __future__ import annotations

import copy
import json
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any


DOCUMENT_TYPES = (
    "invoice",
    "receipt",
    "purchase_order",
    "contract",
    "refund_request",
    "payment_record",
    "bank_statement",
    "financial_report",
    "other",
)

TYPE_TITLES = {
    "invoice": "INVOICE",
    "receipt": "RECEIPT",
    "purchase_order": "PURCHASE ORDER",
    "contract": "CONTRACT",
    "refund_request": "REFUND REQUEST",
    "payment_record": "PAYMENT RECORD",
    "bank_statement": "BANK STATEMENT",
    "financial_report": "FINANCIAL REPORT",
    "other": "FINANCIAL DOCUMENT",
}


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def _empty_document(document_type: str) -> dict[str, Any]:
    return {
        "document_type": document_type,
        "document_id": None,
        "issue_date": None,
        "due_date": None,
        "currency": None,
        "payment_terms": None,
        "customer": {
            "customer_name": None, "customer_id": None, "company_name": None,
            "email": None, "address": None,
        },
        "vendor": {
            "vendor_name": None, "vendor_id": None, "vendor_address": None,
            "vendor_email": None,
        },
        "financial_information": {
            "currency": None, "subtotal": None, "tax_amount": None, "discount": None,
            "total_amount": None, "amount_paid": None, "outstanding_balance": None,
            "payment_status": None,
        },
        "transaction_information": {
            "transaction_id": None, "transaction_date": None, "transaction_amount": None,
            "sender": None, "receiver": None, "reference_number": None,
        },
        "line_items": [],
    }


def _customer(index: int) -> dict[str, str]:
    cities = ("Aurora", "Cedar Point", "Juniper Bay", "Maple Ridge", "Northwind")
    first = ("Avery", "Jordan", "Morgan", "Riley", "Casey", "Taylor", "Quinn", "Parker")[index % 8]
    last = ("Nguyen", "Rivera", "Patel", "Chen", "Brooks", "Kim", "Morgan", "Diaz")[index % 8]
    city = cities[index % len(cities)]
    return {
        "customer_name": f"{first} {last}",
        "customer_id": f"CUST-{index + 1:03d}",
        "company_name": f"{city} Studio {index + 1:02d}",
        "email": f"{first.lower()}.{last.lower()}{index + 1}@example.test",
        "address": f"{100 + index} Lantern Avenue, {city}",
    }


def _vendor(index: int) -> dict[str, str]:
    names = ("Blue Finch Supplies", "Harbor Paper Co.", "Orbit Office Goods", "Cobalt Services", "Willow Tech")
    name = names[index % len(names)]
    return {
        "vendor_name": name,
        "vendor_id": f"VEND-{index % 35 + 1:03d}",
        "vendor_address": f"{20 + index % 80} Market Street, Fairview",
        "vendor_email": f"billing{index % 35 + 1}@{name.lower().replace(' ', '').replace('.', '')}.test",
    }


def _expected_missing(document: dict[str, Any]) -> list[str]:
    requirements = {
        "invoice": ("document_id", "vendor.vendor_name", "issue_date", "financial_information.total_amount", "currency"),
        "receipt": ("vendor.vendor_name", "issue_date", "financial_information.total_amount", "currency"),
        "purchase_order": ("document_id", "vendor.vendor_name", "issue_date", "financial_information.total_amount", "currency"),
        "contract": ("document_id", "issue_date"),
        "refund_request": ("document_id", "customer.customer_name", "financial_information.total_amount", "currency"),
        "payment_record": ("transaction_information.transaction_id", "transaction_information.transaction_date", "transaction_information.transaction_amount"),
        "bank_statement": ("issue_date", "currency"),
        "financial_report": ("issue_date", "currency"),
        "other": (),
    }
    def value(path: str) -> Any:
        current: Any = document
        for key in path.split("."):
            current = current.get(key) if isinstance(current, dict) else None
        return current
    return [field for field in requirements[document["document_type"]] if value(field) in (None, "")]


def _render_document(document: dict[str, Any]) -> str:
    title = TYPE_TITLES[document["document_type"]]
    type_label = {
        "invoice": "Invoice Number", "receipt": "Receipt Number", "purchase_order": "PO Number",
        "contract": "Document Number", "refund_request": "Document Number",
        "payment_record": "Document Number", "bank_statement": "Document Number",
        "financial_report": "Document Number", "other": "Document Number",
    }[document["document_type"]]
    lines = [title]
    def add(label: str, value: Any) -> None:
        if value is not None:
            lines.append(f"{label}: {value}")

    add(type_label, document["document_id"])
    add("Vendor", document["vendor"]["vendor_name"])
    add("Vendor ID", document["vendor"]["vendor_id"])
    add("Vendor Address", document["vendor"]["vendor_address"])
    add("Vendor Email", document["vendor"]["vendor_email"])
    add("Customer", document["customer"]["customer_name"])
    add("Customer ID", document["customer"]["customer_id"])
    add("Company", document["customer"]["company_name"])
    add("Customer Email", document["customer"]["email"])
    add("Customer Address", document["customer"]["address"])
    add("Issue Date", document["issue_date"])
    add("Due Date", document["due_date"])
    add("Currency", document["currency"])
    add("Payment Terms", document["payment_terms"])
    financial = document["financial_information"]
    add("Subtotal", financial["subtotal"])
    add("Tax", financial["tax_amount"])
    add("Discount", financial["discount"])
    add("Total Due", financial["total_amount"])
    add("Amount Paid", financial["amount_paid"])
    add("Outstanding Balance", financial["outstanding_balance"])
    add("Payment Status", financial["payment_status"])
    transaction = document["transaction_information"]
    add("Transaction ID", transaction["transaction_id"])
    add("Transaction Date", transaction["transaction_date"])
    add("Transaction Amount", transaction["transaction_amount"])
    add("Sender", transaction["sender"])
    add("Receiver", transaction["receiver"])
    add("Reference Number", transaction["reference_number"])
    for item in document["line_items"]:
        lines.append(
            "Line Item: {description} | Quantity: {quantity} | Unit Price: {unit_price} | Line Total: {total_price}".format(**item)
        )
    return "\n".join(lines) + "\n"


def _build_record(index: int, randomizer: random.Random) -> dict[str, Any]:
    document_type = DOCUMENT_TYPES[(index - 1) % len(DOCUMENT_TYPES)]
    customer = _customer(index % 50)
    vendor = _vendor(index % 35)
    document = _empty_document(document_type)
    document["document_id"] = f"{document_type[:3].upper()}-{10000 + index}"
    issued = date(2026, 1, 1) + timedelta(days=index % 360)
    document["issue_date"] = issued.isoformat()
    document["due_date"] = (issued + timedelta(days=30)).isoformat()
    document["currency"] = "USD"
    document["payment_terms"] = "Net 30"
    document["customer"] = customer
    document["vendor"] = vendor

    quantity = Decimal((index % 4) + 1)
    unit_price = Decimal(25 + (index * 7) % 175)
    subtotal = quantity * unit_price
    discount = Decimal("5.00") if index % 6 == 0 else Decimal("0.00")
    tax = (subtotal - discount) * Decimal("0.08")
    total = subtotal - discount + tax
    paid = total if index % 3 == 0 else Decimal("0.00")
    status = "paid" if paid == total else "unpaid"
    document["financial_information"] = {
        "currency": "USD",
        "subtotal": _money(subtotal),
        "tax_amount": _money(tax),
        "discount": _money(discount),
        "total_amount": _money(total),
        "amount_paid": _money(paid),
        "outstanding_balance": _money(total - paid),
        "payment_status": status,
    }
    document["line_items"] = [{
        "description": f"Fictional service package {index % 12 + 1}",
        "quantity": _money(quantity),
        "unit_price": _money(unit_price),
        "total_price": _money(subtotal),
    }]

    if document_type == "payment_record":
        document["transaction_information"] = {
            "transaction_id": f"TXN-{700000 + index}",
            "transaction_date": issued.isoformat(),
            "transaction_amount": _money(total),
            "sender": customer["company_name"],
            "receiver": vendor["vendor_name"],
            "reference_number": f"REF-{index:05d}",
        }

    expected_codes: list[str] = ["unclassified_document"] if document_type == "other" else []
    anomalies: list[str] = []
    if index % 37 == 0:
        if document_type == "payment_record":
            document["transaction_information"]["transaction_id"] = None
            anomalies.append("missing_transaction_id")
        else:
            document["document_id"] = None
            anomalies.append("missing_document_id")
    if index % 41 == 0:
        document["financial_information"]["total_amount"] = _money(total + Decimal("7.00"))
        document["financial_information"]["outstanding_balance"] = _money(total - paid + Decimal("7.00"))
        expected_codes.append("financial_total_mismatch")
        anomalies.append("inconsistent_total")
    if index % 67 == 0:
        document["issue_date"] = "2026-99-99"
        expected_codes.append("invalid_issue_date")
        anomalies.append("invalid_issue_date")
    if index % 71 == 0:
        document["due_date"] = (issued - timedelta(days=1)).isoformat()
        expected_codes.append("due_before_issue")
        anomalies.append("contradictory_dates")

    return {
        "synthetic_id": f"SYN-{index:04d}",
        "filename": f"synthetic_{index:04d}.txt",
        "customer_id": customer["customer_id"],
        "customer_name": customer["customer_name"],
        "document": document,
        "expected_validation": {
            "missing_fields": _expected_missing(document),
            "inconsistency_codes": expected_codes,
            "possible_duplicate": False,
        },
        "anomalies": anomalies,
        "text": _render_document(document),
    }


def create_synthetic_dataset(output_directory: str | Path, count: int = 500, seed: int = 941) -> Path:
    """Create count fictional text documents and an indexed label file."""

    if count < 1:
        raise ValueError("count must be positive")
    output = Path(output_directory)
    documents_dir = output / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)
    randomizer = random.Random(seed)
    records: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        record = _build_record(index, randomizer)
        if index % 50 == 0:
            original = records[-1]
            record["document"] = copy.deepcopy(original["document"])
            record["text"] = _render_document(record["document"])
            record["customer_id"] = original["customer_id"]
            record["customer_name"] = original["customer_name"]
            record["expected_validation"] = copy.deepcopy(original["expected_validation"])
            record["expected_validation"]["possible_duplicate"] = True
            record["anomalies"] = list(original["anomalies"]) + ["identical_file_duplicate"]
        (documents_dir / record["filename"]).write_text(record["text"], encoding="utf-8")
        record.pop("text")
        records.append(record)
    manifest = {
        "dataset": "GhostLedger synthetic financial documents",
        "synthetic_only": True,
        "seed": seed,
        "count": count,
        "records": records,
    }
    label_path = output / "ground_truth.json"
    label_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return label_path


def load_synthetic_dataset(output_directory: str | Path) -> dict[str, Any]:
    label_path = Path(output_directory) / "ground_truth.json"
    return json.loads(label_path.read_text(encoding="utf-8"))


def reference_customers(count: int = 40) -> list[dict[str, Any]]:
    """Return fictional customer reference records for deterministic matching.

    These are the same fictional identities used to label the synthetic corpus,
    so a document that names ``CUST-001`` can be checked against a stored record.
    Nothing here is derived from real people.
    """

    if count < 1:
        raise ValueError("count must be positive")
    profiles: list[dict[str, Any]] = []
    for index in range(count):
        customer = _customer(index)
        profiles.append(
            {
                "customer_id": customer["customer_id"],
                "customer_name": customer["customer_name"],
                "customer_type": "individual",
                "company_name": customer["company_name"],
                "email": customer["email"],
                "address": customer["address"],
                "onboarding_status": "reference",
                "verification_status": "reference",
            }
        )
    return profiles


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / "synthetic_data"
    generated = create_synthetic_dataset(destination)
    print(generated)
