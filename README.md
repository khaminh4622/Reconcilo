# GhostLedger

A multi-agent autonomous finance system. Agent 3 reads customer financial
documents, validates the numbers with deterministic Python, and routes the case
to the right specialist agent — with human approval required before any payment.

## The five agents

| Agent | Role |
| --- | --- |
| **Agent 1** | Duplicate Invoice Investigator — detects duplicate invoices and suspected duplicate payments |
| **Agent 2** | Bank Reconciliation Specialist — matches reported payments against bank records |
| **Agent 3** | Financial Intake & Verification — reads PDFs, validates financials, classifies intent, routes |
| **Agent 4** | Payment Exception Specialist — investigates failed or pending payments |
| **Agent 5** | Payment Execution & Monitoring — executes *approved* payments (synthetic in this build) |

Agent 3 is always the entry point. Agents never call each other directly; a
central orchestrator coordinates every handoff with typed structured data.

## Quick start

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # then add your LLM key
python -m uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>, upload a PDF from `backend/fixtures/pdfs/`, and
watch the workflow run.

## Configuration

Agent 3 supports any OpenAI-compatible endpoint. Set these in `backend/.env`:

```dotenv
LLM_API_KEY=your_key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

A local [Ollama](https://ollama.com) setup works too and needs no key — see
`backend/README.md`.

## Safety properties

- No payment executes without a recorded human approval bound to the exact
  invoice, payee, amount and currency.
- Idempotency keys are unique in the database, so the same invoice cannot be
  paid twice.
- Pending or failed payments are never automatically retried.
- Customer-reported payments are never treated as verified bank transactions.
- Every state change is written to an audit trail.

## Tests

```powershell
cd backend
python -m pytest -q          # 122 passing, 2 live tests skipped by default
```

## Documentation

See [`backend/README.md`](backend/README.md) for the full architecture, API
reference, routing table and validation rules.

## License

MIT
