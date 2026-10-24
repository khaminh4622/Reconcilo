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

## Where does the API key go?

**Not on GitHub.** GitHub only stores code; it cannot run the app, so there is
no settings page for secrets there. The key goes into a file on **your own
machine**: `backend/.env`.

That file is gitignored on purpose, so your key is never published. The
repository ships `backend/.env.example` instead - a template listing every
variable. The flow is:

```
backend/.env.example  (in the repo, no secrets)
        |
        |  Copy-Item .env.example .env
        v
backend/.env          (on your machine, gitignored - put your key here)
```

### Steps

1. Copy the template:
   ```powershell
   cd backend
   Copy-Item .env.example .env
   ```
2. Open `.env` in a text editor and paste your key into `LLM_API_KEY`.
   `.env.example` lists three ready-made provider blocks; keep **one** uncommented
   and delete or comment the others.
3. Restart the server. The dashboard header turns green and names the model.

If no key is configured, the app still starts - but every analysis returns
`analysis_source: "unavailable"` with a clear explanation rather than inventing
a result. That is intentional.

## Configuration

Agent 3 supports any OpenAI-compatible endpoint. The three supported setups:

**xAI Grok**
```dotenv
LLM_API_KEY=xai-...
LLM_BASE_URL=https://api.x.ai/v1
LLM_MODEL=grok-4.20-0309-non-reasoning
LLM_RESPONSE_FORMAT=json_object
```

**OpenAI**
```dotenv
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_RESPONSE_FORMAT=json_schema
```

**Local Ollama (free, no key, slower)**
```dotenv
LLM_API_KEY=ollama
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen3:4b
LLM_TIMEOUT_SECONDS=900
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
