"""Demo scenarios: the fixture PDFs, run through the real pipeline on request.

Every step after the model is the production code path: PyMuPDF reads the real
fixture PDF, Agent 3's Python validation and routing run, the orchestrator and
every agent run, and the case is owned by a real demo customer account.

The only stand-in is the model's reading of the document. By default a demo run
uses the scripted reading stored here (``mode="scripted"``), so the result is
the same every time and needs no API key. Such an analysis is labelled
``analysis_source="synthetic_fixture"`` with ``llm_call.invoked = false``; it is
never presented as a live model result. ``mode="live"`` uses the configured
model instead, whose outcome may differ.

``expected`` was derived by running each fixture once on a fresh database and is
pinned by ``tests/test_demo_scenarios.py``. Outcomes on a database that already
holds earlier runs can differ (a second run of an invoice is a repeat).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent3.document_reader import DocumentReadError, read_pdf_upload
from agent3.extractor import LLMExtractor, parse_llm_payload
from agent3.schemas import AnalysisResult, AnalysisSource, LLMCallMetadata

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "pdfs"


class DemoError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class ScriptedReading:
    """A stored model reply, validated against the real PDF text like a live one.

    ``parse_llm_payload`` grounds the reply in the document: figures that are
    not printed in the PDF are discarded, exactly as for a live model.
    """

    def __init__(self, reply: dict[str, Any]):
        self.reply = reply

    def extract(self, source_text: str) -> AnalysisResult:
        result = parse_llm_payload(self.reply, source_text)
        result.analysis_source = AnalysisSource.synthetic_fixture
        result.llm_call = LLMCallMetadata(invoked=False)
        return result


def _invoice(number, vendor, customer, total, *, subtotal=None, tax=None, issue=None, currency="USD",
             company=None, email=None, customer_id=None, extra=None):
    reply = {
        "document_type": "invoice",
        "document_id": number,
        "issue_date": issue,
        "currency": currency,
        "customer": {
            "customer_name": customer, "company_name": company, "email": email, "customer_id": customer_id,
        },
        "vendor": {"vendor_name": vendor},
        "financial_information": {
            "currency": currency,
            "subtotal": subtotal or total,
            "tax_amount": tax or "0.00",
            "total_amount": total,
            "amount_paid": "0.00",
            "outstanding_balance": total,
            "payment_status": "Unpaid",
        },
        "detected_problem": {"type": "none_identified", "primary_problem": "payment_request"},
        "missing_information": [],
        "contradictions": [],
        "information_requiring_verification": [],
    }
    reply.update(extra or {})
    return reply


SCENARIOS: dict[str, dict[str, Any]] = {
    "normal_invoice.pdf": {
        "description": "A clean, internally consistent invoice (INV-2026-0441, 410.40 USD) to pay.",
        "reading": _invoice(
            "INV-2026-0441", "Harborline Office Supply", "Avery Nguyen", "410.40",
            subtotal="400.00", tax="30.40", issue="2026-03-02", company="Aurora Studio 01",
            email="avery.nguyen1@example.test", customer_id="CUST-001",
            extra={"financial_information": {
                "currency": "USD", "subtotal": "400.00", "discount": "20.00", "tax_amount": "30.40",
                "total_amount": "410.40", "amount_paid": "0.00", "outstanding_balance": "410.40",
                "payment_status": "Unpaid",
            }},
        ),
    },
    "Agent1_Test_Original_Unpaid_Invoice.pdf": {
        "description": "A new unpaid invoice (INV-2026-7701, 2,592.00 USD) submitted for approval.",
        "reading": _invoice(
            "INV-2026-7701", "Harborline Office Supply", "Avery Nguyen", "2592.00",
            subtotal="2400.00", tax="192.00", issue="15 Mar 2026", company="Lantern Studio",
            email="avery.nguyen@example.test",
        ),
    },
    "agent4_new_invoice.pdf": {
        "description": "A new unpaid invoice (INV-A4-NEW-0001, 1,250.00 USD) submitted for approval.",
        "reading": _invoice(
            "INV-A4-NEW-0001", "Northstar Cloud Systems LLC", "Redwood Learning Studio LLC", "1250.00",
            issue="2026-10-24",
        ),
    },
    "agent4_payment_failed_complaint.pdf": {
        "description": "The customer reports a 1,250.00 USD payment that the bank rejected.",
        "reading": {
            "document_type": "other",
            "document_id": "INV-DEMO-4",
            "currency": "USD",
            "customer": {"customer_name": "Redwood Learning Studio LLC"},
            "vendor": {"vendor_name": "Northstar Cloud Systems LLC"},
            "financial_information": {"currency": "USD", "total_amount": "1250.00"},
            "detected_problem": {
                "type": "payment_status_discrepancy",
                "primary_problem": "payment_failure",
                "description": "The customer's payment of 1,250.00 was rejected by the bank.",
                "source_evidence": "The bank rejected the transfer and the money never left our account.",
                "status": "reported",
            },
        },
    },
    "agent4_payment_inquiry_paid.pdf": {
        "description": "The customer asks whether their 1,250.00 USD payment for INV-A4-NEW-0001 arrived.",
        "reading": {
            "document_type": "other",
            "document_id": "INV-A4-NEW-0001",
            "currency": "USD",
            "customer": {"customer_name": "Redwood Learning Studio LLC"},
            "vendor": {"vendor_name": "Northstar Cloud Systems LLC"},
            "financial_information": {"currency": "USD", "amount_paid": "1250.00"},
            "detected_problem": {
                "type": "payment_status_discrepancy",
                "primary_problem": "bank_reconciliation",
                "description": "The customer says they paid and asks whether the payment arrived.",
                "source_evidence": "Please confirm whether my payment arrived.",
                "status": "reported",
            },
        },
    },
    "agent4_payment_inquiry_unpaid.pdf": {
        "description": "The customer asks about a payment for an invoice GhostLedger has never seen.",
        "reading": {
            "document_type": "other",
            "document_id": "INV-A4-UNKNOWN-9999",
            "currency": "USD",
            "customer": {"customer_name": "Redwood Learning Studio LLC"},
            "vendor": {"vendor_name": "Northstar Cloud Systems LLC"},
            "financial_information": {"currency": "USD", "amount_paid": "1250.00"},
            "detected_problem": {
                "type": "payment_status_discrepancy",
                "primary_problem": "bank_reconciliation",
                "description": "The customer says they paid and asks whether the payment arrived.",
                "source_evidence": "Please confirm whether my payment arrived.",
                "status": "reported",
            },
        },
    },
    "blue_harbor_overpayment.pdf": {
        "description": "The customer says they paid 3,200.00 USD against a 2,700.00 USD invoice.",
        "reading": {
            "document_type": "invoice",
            "document_id": "INV-2026-1042",
            "issue_date": "12 Jan 2026",
            "currency": "USD",
            "customer": {"customer_name": "Blue Harbor Technologies"},
            "vendor": {"vendor_name": "Harborline Office Supply"},
            "financial_information": {
                "currency": "USD", "subtotal": "2500.00", "tax_amount": "200.00", "total_amount": "2700.00",
                "amount_paid": "3200.00", "outstanding_balance": "500.00",
            },
            "detected_problem": {
                "type": "payment_status_discrepancy",
                "primary_problem": "bank_reconciliation",
                "description": "The customer says the invoice was already paid in full, and more.",
                "source_evidence": "Customer note: I have already paid this invoice in full.",
                "status": "reported",
            },
        },
    },
    "contradictory_financials.pdf": {
        "description": "An invoice whose subtotal, total and balance cannot all be right.",
        "reading": {
            "document_type": "invoice",
            "document_id": "INV-2026-0733",
            "issue_date": "2026-07-05",
            "due_date": "2026-06-20",
            "currency": "USD",
            "customer": {
                "customer_name": "Marcus Ibe", "company_name": "Kestrel Logistics",
                "email": "marcus.ibe@example.test",
            },
            "financial_information": {
                "currency": "USD", "subtotal": "900.00", "tax_amount": "72.00", "discount": "0.00",
                "total_amount": "1062.00", "amount_paid": "200.00", "outstanding_balance": "500.00",
                "payment_status": "Partially Paid",
            },
            "detected_problem": {"type": "none_identified", "primary_problem": "unknown"},
            "contradictions": ["The figures on this page do not agree with each other."],
        },
    },
    "corrupted.pdf": {
        "description": "A deliberately invalid PDF; intake must reject it safely.",
        "reading": None,
    },
    "duplicate_payment.pdf": {
        "description": "The customer believes they paid INV-2026-2058 (1,944.00 USD) twice.",
        "reading": {
            "document_type": "invoice",
            "document_id": "INV-2026-2058",
            "issue_date": "04 Feb 2026",
            "currency": "USD",
            "customer": {"customer_name": "Blue Harbor Technologies Inc."},
            "vendor": {"vendor_name": "Northstar Office Supplies LLC"},
            "financial_information": {
                "currency": "USD", "subtotal": "1800.00", "tax_amount": "144.00", "total_amount": "1944.00",
                "amount_paid": "3888.00",
            },
            "detected_problem": {
                "type": "payment_status_discrepancy",
                "primary_problem": "duplicate_payment",
                "description": "The customer thinks the invoice was paid twice and asks for a refund.",
                "source_evidence": "I think we have paid this invoice twice by mistake.",
                "status": "reported",
                "reported_payment_count": 2,
            },
        },
    },
    "missing_customer_information.pdf": {
        "description": "An invoice with no customer identity on it.",
        "reading": {
            "document_type": "invoice",
            "document_id": "INV-2026-0612",
            "issue_date": "2026-06-01",
            "currency": "USD",
            "customer": {"customer_name": None},
            "financial_information": {
                "currency": "USD", "subtotal": "1200.00", "tax_amount": "0.00", "total_amount": "1200.00",
                "amount_paid": "0.00", "outstanding_balance": "1200.00", "payment_status": "Unpaid",
            },
            "detected_problem": {"type": "none_identified", "primary_problem": "unknown"},
            "missing_information": ["customer name", "customer email"],
        },
    },
    "payment_dispute.pdf": {
        "description": "The customer says INV-2026-0441 was paid but they still got a reminder.",
        "reading": {
            "document_type": "other",
            "document_id": "INV-2026-0441",
            "currency": "USD",
            "customer": {
                "customer_name": "Avery Nguyen", "customer_id": "CUST-001",
                "company_name": "Aurora Studio 01", "email": "avery.nguyen1@example.test",
            },
            "financial_information": {
                "currency": "USD", "total_amount": "410.40", "amount_paid": "0.00",
                "outstanding_balance": "410.40", "payment_status": "Unpaid",
            },
            "detected_problem": {
                "type": "payment_status_discrepancy",
                "primary_problem": "bank_reconciliation",
                "description": "The customer says the invoice was paid by bank transfer but still shows unpaid.",
                "source_evidence": "I already paid invoice INV-2026-0441 on 2026-03-20 by bank transfer.",
                "status": "reported",
            },
        },
    },
    "refund_request.pdf": {
        "description": "A refund request for a returned order; not an invoice payment.",
        "reading": {
            "document_type": "refund_request",
            "document_id": "RF-2026-0078",
            "currency": "USD",
            "customer": {
                "customer_name": "Dana Whitfield", "company_name": "Loomer Craft Collective",
                "email": "dana.whitfield@example.test",
            },
            "financial_information": {"currency": "USD", "total_amount": "89.50"},
            "detected_problem": {
                "type": "refund_request",
                "primary_problem": "unknown",
                "description": "The customer asks for a full refund of a returned order.",
                "source_evidence": "I am requesting a full refund of 89.50 to the original card.",
                "status": "reported",
            },
        },
    },
    "unknown_document_type.pdf": {
        "description": "A field service report with no financial content.",
        "reading": {
            "document_type": "other",
            "document_id": "FSR-AA-11",
            "detected_problem": {"type": "none_identified", "primary_problem": "unknown"},
        },
    },
    "unseen_letter.pdf": {
        "description": "A supplier letter disputing a 2,340.00 GBP payment the customer says was made.",
        "reading": {
            "document_type": "other",
            "document_id": "ST-2026-88",
            "currency": "GBP",
            "customer": {"customer_name": "Ms Okafor"},
            "vendor": {"vendor_name": "Selby & Crane Accounts Desk"},
            "financial_information": {"currency": "GBP", "total_amount": "3120.00", "amount_paid": "2340.00"},
            "detected_problem": {
                "type": "payment_status_discrepancy",
                "primary_problem": "bank_reconciliation",
                "description": "The customer says 2,340.00 GBP was paid; the supplier cannot trace it.",
                "source_evidence": "We cannot trace that payment on our side",
                "status": "reported",
            },
        },
    },
}

# Pinned from one run of each fixture on a fresh database (scripted reading).
# route = the case intent; agents = steps that ran, in order; outcome = the
# customer_response outcome (None while a case still awaits approval).
EXPECTED: dict[str, dict[str, Any]] = {
    'normal_invoice.pdf': {'route': 'new_payment_request', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'agent_1', 'agent_5', 'human_review'], 'outcome': 'BLOCKED_DUPLICATE'},
    'Agent1_Test_Original_Unpaid_Invoice.pdf': {'route': 'new_payment_request', 'status': 'AWAITING_APPROVAL', 'agents': ['agent_3', 'agent_1', 'human_approval'], 'outcome': None},
    'agent4_new_invoice.pdf': {'route': 'new_payment_request', 'status': 'AWAITING_APPROVAL', 'agents': ['agent_3', 'agent_1', 'human_approval'], 'outcome': None},
    'agent4_payment_failed_complaint.pdf': {'route': 'payment_failure', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'agent_4', 'agent_5', 'human_review'], 'outcome': 'FAILURE_CHECKED'},
    'agent4_payment_inquiry_paid.pdf': {'route': 'payment_status_dispute', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'agent_2', 'agent_5', 'human_review'], 'outcome': 'PAYMENT_NOT_FOUND'},
    'agent4_payment_inquiry_unpaid.pdf': {'route': 'payment_status_dispute', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'agent_2', 'agent_5', 'human_review'], 'outcome': 'PAYMENT_NOT_FOUND'},
    'blue_harbor_overpayment.pdf': {'route': 'payment_status_dispute', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'agent_2', 'agent_5', 'human_review'], 'outcome': 'PAYMENT_NOT_FOUND'},
    'contradictory_financials.pdf': {'route': 'unknown', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'human_review', 'agent_5'], 'outcome': 'NEEDS_REVIEW'},
    'corrupted.pdf': {'route': 'rejected_at_intake', 'status': 'HTTP 400', 'agents': [], 'outcome': None},
    'duplicate_payment.pdf': {'route': 'duplicate_payment', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'agent_1', 'agent_5', 'human_review'], 'outcome': 'DUPLICATE_CHECKED'},
    'missing_customer_information.pdf': {'route': 'unknown', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'human_review', 'agent_5'], 'outcome': 'NEEDS_REVIEW'},
    'payment_dispute.pdf': {'route': 'bank_reconciliation', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'agent_2', 'agent_5', 'human_review'], 'outcome': 'PAYMENT_DETAILS_NEEDED'},
    'refund_request.pdf': {'route': 'unknown', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'human_review', 'agent_5'], 'outcome': 'NEEDS_REVIEW'},
    'unknown_document_type.pdf': {'route': 'unknown', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'human_review', 'agent_5'], 'outcome': 'NEEDS_REVIEW'},
    'unseen_letter.pdf': {'route': 'payment_status_dispute', 'status': 'REQUIRES_REVIEW', 'agents': ['agent_3', 'agent_2', 'agent_5', 'human_review'], 'outcome': 'PAYMENT_NOT_FOUND'},
}


def list_scenarios() -> list[dict[str, Any]]:
    present = {path.name for path in FIXTURE_DIR.glob("*.pdf")}
    return [
        {
            "name": name,
            "description": spec["description"],
            "available": name in present,
            "expected": EXPECTED.get(name),
        }
        for name, spec in SCENARIOS.items()
    ]


def run_scenario(app, name: str, *, admin, customer, mode: str = "scripted"):
    """Run one fixture through the real pipeline for ``customer``; return the case."""

    from agent3.service import Agent3Service

    from .schemas import AgentId

    spec = SCENARIOS.get(name)
    if spec is None:
        raise DemoError(f"Unknown scenario {name!r}.", 404)
    path = FIXTURE_DIR / name
    if not path.is_file():
        raise DemoError(f"The fixture {name} is not present on this server.", 404)
    if mode not in {"scripted", "live"}:
        raise DemoError("mode must be 'scripted' or 'live'.", 400)
    try:
        document = read_pdf_upload(name, path.read_bytes(), "application/pdf")
    except DocumentReadError as exc:
        raise DemoError(f"Agent 3 rejected the document at intake: {exc}", 400) from exc

    if mode == "live":
        extractor = app.state.service.extractor
        if not isinstance(extractor, LLMExtractor):
            raise DemoError("No live model is configured; use mode 'scripted'.", 409)
    else:
        extractor = ScriptedReading(spec["reading"])
    analysis = Agent3Service(app.state.service.repository, extractor).analyze(document)

    orchestrator = app.state.orchestrator
    case = orchestrator.open_case(analysis, filename=name, channel="demo")
    case.owner_account_id = customer.account_id
    case.add_audit(
        AgentId.orchestrator,
        "demo_run",
        f"Demo run of {name} by {admin.display_name} for {customer.display_name} "
        f"({'scripted reading; no model was called' if mode == 'scripted' else 'live model reading'}).",
        actor_account_id=admin.account_id,
    )
    orchestrator.repository.save_case(case)
    return orchestrator.route(case)
