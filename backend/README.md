# GhostLedger - five-agent autonomous finance workflow

Agent 3 is always the entry point. It reads a customer PDF, validates the
financial facts, classifies the customer's intent, and hands the case to the
central orchestrator, which dispatches one specialist and keeps an audit trail.

```
PDF -> Agent 3 (intake + validation) -> Orchestrator -> specialist agent
                                                          |
                                   Human approval (payments only) -> Agent 5
                                                                       | failure
                                                                    Agent 4
```

## The agents

| Agent | Module | Responsibility |
| --- | --- | --- |
| **Agent 1** | `workflow/agent1_duplicates.py` | Investigations of suspected duplicate invoices and duplicate payments. Compares invoice numbers, parties, dates and amounts; counts payment references; checks prior payments. Never refunds. |
| **Agent 2** | `workflow/agent2_reconciliation.py` | Reconciles reported payments against synthetic bank records by reference, then amount + counterparty. A payment is never marked verified without a matching settled transaction. |
| **Agent 3** | `agent3/` | Financial Intake & Verification. LLM extraction, `Decimal` validation, intent classification, routing recommendation. |
| **Agent 4** | `workflow/agent4_exceptions.py` | Diagnoses failed and pending payments, recommends resolution, escalates to humans. Never retries. |
| **Agent 5** | `workflow/agent5_execution.py` | Synthetic payment execution. Requires a matching human approval, blocks duplicates with a unique idempotency key, never retries a pending or failed attempt. |

All agents are independent modules. None calls another directly - the
orchestrator (`workflow/orchestrator.py`) coordinates every handoff using typed
`AgentReport` objects.

## Routing

| Intent | Primary agent |
| --- | --- |
| `new_payment_request` | Human approval, then Agent 5 |
| `duplicate_invoice` | Agent 1 |
| `duplicate_payment` | Agent 1 (Agent 2 supporting) |
| `bank_reconciliation` / `payment_status_dispute` | Agent 2 |
| `payment_failure` | Agent 4 |
| `unknown` | Human review |

The LLM classifies the intent; Python validates it. A new payment goes to human
review when financial consistency failed, required fields are missing, customer
details contradict stored records, or the document contains contradictions. An
*unverified payment* alone does not block a new payment request - an unpaid
invoice always has none, and the human approval step covers that risk.

## Safety properties

- Agent 5 will not execute without an approval record.
- The approval binds to invoice number, payee, amount and currency; any mismatch blocks execution.
- Payment attempts are keyed by `sha256(invoice|payee|amount|currency)` and that key is `UNIQUE` in SQLite, so the same obligation cannot be paid twice, even from a different case.
- A `PENDING` attempt blocks further execution until it is investigated.
- `FAILED` and `PENDING` are never recorded as completed transfers.
- Agent 4 never retries a payment. Agent 1 never issues a refund.
- Every state change is written to the case audit trail with actor and timestamps.

## Workflow API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/workflow/cases` | Upload a PDF; Agent 3 analyses and the orchestrator routes |
| `GET` | `/workflow/cases` | List recent cases |
| `GET` | `/workflow/cases/{id}` | Full case, reports, runs and audit |
| `POST` | `/workflow/cases/{id}/approve` | Record human approval, then Agent 5 executes |
| `POST` | `/workflow/cases/{id}/reject` | Reject a case awaiting approval |
| `POST` | `/workflow/cases/{id}/execute` | Re-attempt an approved case (idempotency still applies) |
| `GET` | `/workflow/bank-transactions` | Synthetic bank records used by Agent 2 |

The original `/agent3/analyze` and every earlier endpoint are unchanged.

## Case statuses

`RECEIVED -> ANALYZING -> VALIDATING -> ROUTED -> INVESTIGATING ->
AWAITING_APPROVAL -> APPROVED -> EXECUTING -> PENDING -> COMPLETED | FAILED |
REQUIRES_REVIEW`

## Workflow tests

`tests/test_workflow.py` covers every required scenario: valid new invoice;
duplicate invoice; duplicate payment (Agent 1 then Agent 2); bank mismatch;
failed payment to Agent 4; Agent 5 failure to Agent 4; missing information to
human review; successful payment recorded; duplicate execution blocked;
unapproved payment refused; approval/amount mismatch refused; pending payment not
retried; Agent 3 always first; audit trail present; existing Agent 3 contract
unchanged.

# GhostLedger — Agent 3: Customer Document Understanding

Agent 3 reads a customer PDF, sends its extracted text to a configured
OpenAI-compatible LLM, validates the returned claims independently in Python,
and stores a structured result.

It does **not** route work to other agents, and it does **not** make financial,
identity, payment, or refund decisions. A reported problem stays reported until
something outside this component confirms it.

## Workflow

```
PDF upload → PyMuPDF text extraction → LLM analysis → Python validation → structured JSON
```

Open `http://127.0.0.1:8000` to use the dashboard: upload a PDF, press **Analyze
with AI**, then inspect the extracted customer information, the financial
problem the model detected, the validation findings, the supporting excerpts
with page numbers, and the full JSON.

## Setup

From `ghostledger/backend` in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env    # then edit .env
python -m uvicorn main:app --reload
```

Configure any Chat Completions compatible endpoint in `.env`:

```dotenv
LLM_API_KEY=your_key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_RESPONSE_FORMAT=json_schema
LLM_TIMEOUT_SECONDS=60
```

- `LLM_RESPONSE_FORMAT=json_schema` (default) requests Structured Outputs.
- Use `json_object` only for a provider without Structured Outputs; the reply is
  still parsed and validated with Pydantic either way.

### Current configuration: xAI Grok (hosted, fast)

`.env` currently points at the xAI OpenAI-compatible API:

```dotenv
LLM_API_KEY=xai-...
LLM_BASE_URL=https://api.x.ai/v1
LLM_MODEL=grok-4.20-0309-non-reasoning
LLM_RESPONSE_FORMAT=json_object
LLM_TIMEOUT_SECONDS=180
```

A typical document is analysed in **4-6 seconds**. Model choice matters: with the
full Agent 3 prompt, `grok-4.20-0309-non-reasoning` returned correct customer
and vendor roles and `primary_problem: duplicate_payment`, while `grok-4.6`
(reasoning) also worked but took ~76 s and consumed ~5,900 reasoning tokens per
document.

### Running fully locally with Ollama (no API key, no cost)

This is the configuration currently in `.env`. Install
[Ollama](https://ollama.com/download), then pull a model:

```powershell
ollama pull qwen3:4b
```

and point `.env` at it:

```dotenv
LLM_API_KEY=ollama                  # placeholder; Ollama needs a non-empty key
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen3:4b
LLM_TIMEOUT_SECONDS=900
LLM_MAX_TOKENS=2048
```

When `LLM_BASE_URL` points at a local Ollama server, Agent 3 automatically uses
Ollama's **native `/api/chat` API** rather than the OpenAI-compatible `/v1`
endpoint. This is required, not cosmetic: `/v1` cannot disable reasoning for
thinking models such as qwen3, so the entire token budget is spent in an
internal `reasoning` field and `content` comes back empty. The native API
accepts `think: false` together with `format: "json"`, which returns usable JSON.

**Expect roughly 5 minutes per document.** CPU inference measured about 5
tokens/sec, and a full extraction needs ~1,400 output tokens. Raise
`LLM_TIMEOUT_SECONDS` for longer PDFs. A hosted provider is much faster if
latency matters.

### No silent fallback

If `LLM_API_KEY` or `LLM_MODEL` is missing, or the provider fails, the API
returns a result with:

- `analysis_source: "unavailable"`
- `validation.status: "extraction_failed"`
- `processing_errors` naming the actual failure
- every extracted field left `null`

Nothing is ever invented to fill the gap. `GET /health` reports whether a live
model is configured **before** you upload anything, and the UI shows the same
state in its header pill.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/agent3/analyze` | Multipart upload, one PDF in field `file` |
| `GET` | `/agent3/results/{document_id}` | Fetch a saved result by record or document ID |
| `GET` | `/customers` | List the fictional reference records used for matching |
| `GET` | `/customers/{customer_id}` | Fetch one reference record |
| `GET` | `/health` | Live-model state, model name, reference-customer count |
| `GET` | `/agent3/synthetic/*` | Development corpus and evaluation (not in the UI) |

The synthetic corpus and its evaluation endpoint remain available for
development. They never cause an unavailable live analysis to be presented as a
model result.

## Output

Abridged response for the reported overpayment case (a $2,700 invoice with a
reported $3,200 payment):

```json
{
  "customer_information": { "customer_name": "Blue Harbor Technologies" },
  "vendor_information": { "vendor_name": null },
  "document_information": {
    "document_type": "invoice",
    "document_number": "INV-2026-1042",
    "issue_date": "12 Jan 2026",
    "due_date": "11 Feb 2026",
    "currency": "USD"
  },
  "financial_information": {
    "currency": "USD",
    "subtotal": "2500.00",
    "tax_amount": "200.00",
    "total_amount": "2700.00",
    "amount_paid": "3200.00",
    "outstanding_balance": "500.00",
    "payment_claim": {
      "claimed_amount": "3200.00",
      "status": "reported",
      "requires_reconciliation": true,
      "note": "The customer reports having paid; no payment artefact supports the claim."
    }
  },
  "detected_problem": {
    "type": "payment_status_discrepancy",
    "description": "The customer says the invoice was already paid.",
    "source_evidence": "I have already paid this invoice in full.",
    "page": 1,
    "status": "reported"
  },
  "validation_findings": [
    { "code": "subtotal_tax_total", "label": "Subtotal + tax = invoice total",
      "status": "passed", "explanation": "Subtotal 2500.00 plus tax 200.00 equals the stated total 2700.00." },
    { "code": "overpayment_detected", "label": "Overpayment check",
      "status": "failed", "explanation": "The reported payment 3200.00 exceeds the invoice total 2700.00 by 500.00.",
      "variance": "500.00" },
    { "code": "payment_verification", "label": "Payment verification",
      "status": "unverified", "explanation": "The payment of 3200.00 is reported … it must be reconciled." }
  ],
  "validation": {
    "financial_consistency": "failed",
    "customer_verification": "unverified",
    "payment_provenance": "reported",
    "requires_reconciliation": true,
    "calculated": { "potential_overpayment": "500.00" }
  },
  "routing": {
    "recommended_agent": "agent_2",
    "agent_name": "Agent 2 (Bank Reconciliation Specialist)",
    "reason": "Agent 2 should reconcile the money trail because a potential overpayment of 500.00 USD; the reported payment of 3200.00 is reported and not verified.",
    "blocking": true
  },
  "reasoning": ["…one line per step Agent 3 took…"],
  "missing_information": [],
  "requires_review": true
}
```

Amounts serialize as **strings**, never floats. Full responses also carry
`source` (filename, page count, hash), `evidence`, `contradictions`, and
`llm_call` — the audit trail showing whether a real request happened:

```json
"llm_call": {
  "invoked": true,
  "provider_response_id": "…",
  "model": "qwen3:4b",
  "usage": { "prompt_tokens": 1574, "completion_tokens": 1431, "total_tokens": 3005 }
}
```

`reasoning` explains every step in plain language, so the routing decision is
auditable rather than a bare label.

## What the LLM does, and what Python does

**The model** reads the document and reports document type, customer details,
references, financial figures, the reported financial problem, contradictions,
missing fields, and which claims need verification. The prompt is written to
make it *read* rather than pattern-match: a payment dispute inside an invoice is
`payment_status_discrepancy`, not `none_identified`.

**Python decides the truth of the arithmetic, the payment status, and the
identity.** The model is never the source of truth for any of them:

- **Financial consistency** — `subtotal − discount + tax = total`,
  `total − payments = outstanding`, each line item's `quantity × unit_price`,
  and currency-precision-aware tolerance. All money is `Decimal`, never float.
- **Overpayment detection** — when the reported payment exceeds the invoice
  total, the difference is reported as `potential_overpayment`.
- **Payment provenance** — a payment the document mentions is a *claim*. It is
  `reported` (customer assertion), `document_supported` (a receipt or
  remittance advice is referenced), or `verified` (matched to an independent
  source). Agent 3 has no independent source, so nothing is `verified` here and
  every claimed payment requires reconciliation before it may clear a balance.
- **Date consistency** — recognizable calendar dates in any common format, and
  due date not before issue date.
- **Required fields** — per document type. A missing total or currency is a
  `FAILED` financial result, never a pass.
- **Customer verification** — exact matching against stored reference records
  only, by customer ID or by name plus company, or by email. No fuzzy matching.
- **Duplicates** — by file hash and by document metadata.
- **Routing** — Agent 2 for anything needing a money trail, Agent 3 for
  unresolved verification, or no further agent when nothing is outstanding.

### How field recovery works

A grounded model value always wins. When the model returns `null` for a money
field or the currency, the value is recovered deterministically from the
document's own labelled line (`_AMOUNT_LABELS` in `extractor.py`), and every
recovery is recorded in `processing_errors`. Recovery understands alternative
labels — "Net Amount", "GST", "Grand Total", "Total Payable", "Remittance",
"Balance Owing" — and prefers the payable amount over a pre-discount total.

Model-supplied numbers are matched against every figure printed in the
document, compared *by value* rather than by string. This lets a correct
`2700.00` from the model match a printed `$2,700.00` instead of being discarded
as unsupported. Printed placeholders such as `-`, `N/A`, or "Not provided" are
treated as missing rather than becoming a customer identity. Evidence excerpts
must be exact quotes, tolerant only of line wrapping; the page number is
recomputed from the PDF, never taken on trust.

### Routing

The customer's **primary reported problem** selects the first specialized
agent. Overpayment amounts and failed arithmetic raise priority and add a
secondary Agent 2 step, but they never displace the primary agent.

| Primary problem | First agent |
| --- | --- |
| `duplicate_invoice` | Agent 1 (Duplicate Invoice Investigator) |
| `duplicate_payment` | Agent 1 (Duplicate Invoice Investigator) |
| `bank_reconciliation` | Agent 2 (Bank Reconciliation Specialist) |
| `payment_failure` | Agent 4 (Payment Failure Specialist) |
| `payment_request` | Human approval, then Agent 5 |
| `unknown` | Human review / clarification |

Agent 1 receives Agent 2 as a **secondary** investigation when bank-record
verification is still needed, rather than being replaced by it. A duplicate is
always reported as alleged, never confirmed.

The model classifies `primary_problem`; Python validates it. An explicit
`unknown` from the model is respected and escalates to human review. Only when
the model supplies no classification at all does Agent 3 infer one from the
document's wording, and the result is then marked `overridden: true`.

### Financial status values

| Status | Meaning |
| --- | --- |
| `passed` | Arithmetic reconciles and no payment claim is outstanding |
| `failed` | A numeric conflict, overpayment, or missing required field |
| `unverified` | Arithmetic reconciles but rests on an unconfirmed payment |
| `not_available` | The document carries no financial figures |
| `requires_review` | Review needed; used for extraction failures |

Each entry in `validation_findings` carries its own
`passed` / `failed` / `unverified` / `not_applicable` status with a plain-language
explanation, expected value, reported value, and variance.

## PDF handling

PyMuPDF extracts text page by page and inserts `[Page N]` markers so evidence
citations can be traced. Handled explicitly:

- **Multi-page** — all pages extracted and cited by page.
- **Empty / image-only / scanned** — a clear needs-OCR error (HTTP 400). No OCR
  or vision model is configured by default.
- **Corrupted** — a safe read error, never a crash.
- **Missing information** — reported in `missing_information` and
  `validation.missing_fields`, never filled in.
- **Oversized text** — extracted text beyond `MAX_TEXT_CHARS` (default
  1,000,000) is rejected with a clear error rather than sent as a huge prompt.

## Tests

```powershell
python -m pytest -q
```

120 tests collected: 118 run by default, and the 2 live-provider tests are skipped
unless explicitly enabled. The suite mocks only the remote LLM boundary and
covers the scenarios in `tests/test_scenarios.py` against generated fictional
PDFs in `fixtures/pdfs/`:

| Fixture | Scenario |
| --- | --- |
| `normal_invoice.pdf` | Normal, internally consistent invoice |
| `payment_dispute.pdf` | Customer says a paid invoice still shows unpaid (2 pages) |
| `refund_request.pdf` | Refund request |
| `missing_customer_information.pdf` | No customer identifying fields |
| `contradictory_financials.pdf` | Totals that cannot all be correct |
| `unknown_document_type.pdf` | Unfamiliar document with no financial content |
| `unseen_letter.pdf` | Unfamiliar letter wording and layout |
| `corrupted.pdf` | Deliberately invalid PDF |

Also covered: provider HTTP failures, unreachable providers, malformed JSON,
prompt-injection attempts, placeholder customer values, hallucinated figures
being discarded, and the exact JSON contract.

Regenerate the fictional PDFs at any time:

```powershell
python tools/generate_fixture_pdfs.py
```

`tests/test_llm_client.py` runs the real OpenAI SDK against a local HTTP server
speaking the Chat Completions protocol, so the request shape (structured output,
document framed as data) and response handling (response ID, token usage) are
verified without contacting a provider.

### Live integration test

The only test that contacts a real provider, in
`tests/test_live_llm_integration.py`:

```powershell
$env:RUN_LIVE_LLM_TEST = "1"
python -m pytest -m integration -v
```

It requires `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`, and asserts a
provider-assigned response ID plus non-zero token usage — proving a genuine
model request was made rather than a canned reply. It is skipped by default so
the normal suite sends no data and incurs no usage.
