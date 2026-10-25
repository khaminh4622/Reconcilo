# GhostLedger frontend

Vite + React + TypeScript + Tailwind. Talks to the backend through Vite's proxy
(`/auth`, `/workflow`, `/admin`, `/demo`, `/bank`, `/customers`, `/developer`, `/health`
→ `http://127.0.0.1:8000`).

```powershell
# backend (from backend/, venv active)
python -m tools.seed_demo_accounts
python -m uvicorn main:app --host 127.0.0.1 --port 8000

# frontend (from frontend/)
npm install
npm run dev          # http://127.0.0.1:5173
npm run typecheck
npm test
```

Demo logins (local only): `customer.a@`, `customer.b@`, `bank.admin@`, `demo.admin@ghostledger.test`,
password `ghostledger-demo`. The sign-in page has a "Demo accounts" helper, and `#/demo-script`
lists three runs to show.
