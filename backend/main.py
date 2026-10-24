"""FastAPI entry point for the standalone GhostLedger Agent 3 MVP."""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent3.database import DocumentRepository
from agent3.document_reader import DocumentReadError, read_pdf_upload, read_upload
from agent3.evaluate_synthetic import evaluate_dataset
from agent3.extractor import LLMExtractor, build_extractor_from_environment
from agent3.schemas import (
    AnalysisResult,
    CustomerProfile,
)
from agent3.service import Agent3Service
from agent3.synthetic_data import create_synthetic_dataset, load_synthetic_dataset, reference_customers
from workflow.agent1_history import InvoiceHistoryRepository
from workflow.orchestrator import WorkflowError, WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import CaseSummary, WorkflowCase
from workflow.seed import synthetic_bank_seed


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))


def _dataset_directory() -> Path:
    return Path(os.getenv("SYNTHETIC_DATA_DIR", str(BASE_DIR / "synthetic_data")))


def _ensure_dataset(directory: Path) -> None:
    labels = directory / "ground_truth.json"
    if not labels.exists():
        create_synthetic_dataset(directory, count=500)


def create_app(service: Agent3Service | None = None, dataset_directory: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="GhostLedger Agent 3",
        version="0.1.0",
        description="LLM-powered customer PDF understanding with deterministic validation.",
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    dataset = dataset_directory or _dataset_directory()
    _ensure_dataset(dataset)
    app.state.dataset_directory = dataset
    app.state.service = service or Agent3Service(
        DocumentRepository(os.getenv("AGENT3_DB_PATH", str(BASE_DIR / "data" / "agent3.db"))),
        build_extractor_from_environment(),
    )
    # Customer-record matching needs reference data. Seeding is idempotent, so
    # existing development records are preserved across restarts.
    if os.getenv("AGENT3_SEED_CUSTOMERS", "1") != "0":
        app.state.service.repository.seed_customer_references(reference_customers())

    # The orchestrator shares Agent 3's database file, adding its own tables.
    workflow_repository = WorkflowRepository(
        os.getenv("AGENT3_DB_PATH", str(BASE_DIR / "data" / "agent3.db"))
    )
    if os.getenv("AGENT3_SEED_BANK", "1") != "0":
        workflow_repository.seed_bank_transactions(synthetic_bank_seed())
    app.state.workflow_repository = workflow_repository
    app.state.orchestrator = WorkflowOrchestrator(workflow_repository)

    def catalog() -> dict[str, Any]:
        return load_synthetic_dataset(app.state.dataset_directory)

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(BASE_DIR / "static" / "index.html")

    @app.get("/health")
    def health() -> dict[str, Any]:
        extractor = app.state.service.extractor
        client = getattr(extractor, "client", None)
        is_live = isinstance(extractor, LLMExtractor)
        # Hosted clients expose the SDK (and its base_url); Ollama exposes host.
        sdk_client = getattr(client, "client", None)
        base_url = getattr(client, "host", None) or (
            str(sdk_client.base_url) if sdk_client is not None else None
        )
        return {
            "status": "healthy",
            "service": "ghostledger-agent3",
            "analysis_mode": "live_llm" if is_live else "unavailable",
            "extractor": type(extractor).__name__,
            "llm_configured": is_live,
            "llm_model": getattr(client, "model", None),
            "llm_base_url": base_url,
            "reference_customers": app.state.service.repository.count_customers(),
            "detail": None if is_live else getattr(extractor, "reason", None),
        }

    @app.get("/customers", response_model=list[CustomerProfile])
    def list_customers() -> list[CustomerProfile]:
        """List the existing fictional reference records used for matching."""

        return app.state.service.repository.list_customers()

    @app.get("/customers/{customer_id}", response_model=CustomerProfile)
    def get_customer(customer_id: str) -> CustomerProfile:
        customer = app.state.service.repository.get_customer(customer_id)
        if not customer:
            raise HTTPException(status_code=404, detail="Customer profile was not found.")
        return customer

    @app.post("/agent3/analyze", response_model=AnalysisResult)
    async def analyze_upload(
        file: Annotated[UploadFile, File(description="Customer financial PDF document")],
    ) -> AnalysisResult:
        if not file.filename:
            raise HTTPException(status_code=400, detail="A filename is required.")
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="The upload exceeds the configured size limit.")
        try:
            document = read_pdf_upload(file.filename, content, file.content_type)
        except DocumentReadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return app.state.service.analyze(document)

    # ------------------------------------------------------------------
    # Multi-agent workflow API
    # ------------------------------------------------------------------
    async def _read_upload(file: UploadFile) -> Any:
        if not file.filename:
            raise HTTPException(status_code=400, detail="A filename is required.")
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="The upload exceeds the configured size limit.")
        try:
            return read_pdf_upload(file.filename, content, file.content_type)
        except DocumentReadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/workflow/cases", response_model=WorkflowCase, status_code=201)
    async def open_case(
        file: Annotated[UploadFile, File(description="Customer financial PDF document")],
    ) -> WorkflowCase:
        """Agent 3 reads the PDF, validates it, and the orchestrator routes the case."""

        document = await _read_upload(file)
        analysis = app.state.service.analyze(document)
        orchestrator: WorkflowOrchestrator = app.state.orchestrator
        case = orchestrator.open_case(analysis, filename=document.filename or "document.pdf")
        return orchestrator.route(case)

    @app.get("/workflow/cases", response_model=list[CaseSummary])
    def list_cases(limit: int = 50) -> list[CaseSummary]:
        return app.state.workflow_repository.list_cases(limit=limit)

    @app.get("/workflow/cases/{case_id}", response_model=WorkflowCase)
    def get_case(case_id: str) -> WorkflowCase:
        case = app.state.orchestrator.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        return case

    @app.post("/workflow/cases/{case_id}/approve", response_model=WorkflowCase)
    def approve_case(case_id: str, request: dict[str, Any]) -> WorkflowCase:
        """Record human approval and let Agent 5 execute the synthetic payment."""

        approved_by = str(request.get("approved_by") or "").strip()
        if not approved_by:
            raise HTTPException(status_code=400, detail="approved_by is required.")
        try:
            return app.state.orchestrator.approve(
                case_id,
                approved_by=approved_by,
                note=request.get("note"),
                simulate=str(request.get("simulate") or "SUCCESS"),
                failure_reason=request.get("failure_reason"),
            )
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/workflow/cases/{case_id}/reject", response_model=WorkflowCase)
    def reject_case(case_id: str, request: dict[str, Any]) -> WorkflowCase:
        rejected_by = str(request.get("rejected_by") or "").strip()
        if not rejected_by:
            raise HTTPException(status_code=400, detail="rejected_by is required.")
        try:
            return app.state.orchestrator.reject(case_id, rejected_by, request.get("reason"))
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/workflow/cases/{case_id}/investigation")
    def get_investigation(case_id: str) -> dict[str, Any]:
        """Agent 1's duplicate investigation for a case, if it has run."""

        case = app.state.orchestrator.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        report = next((r for r in reversed(case.reports) if r.agent.value == "agent_1"), None)
        if report is None:
            raise HTTPException(status_code=404, detail="Agent 1 has not investigated this case.")
        return {
            "case_id": case.case_id,
            "invoice_number": case.invoice_number,
            "vendor": case.vendor_name,
            "customer": case.customer_name,
            "status": report.data.get("status"),
            "summary": report.summary,
            "report": report.model_dump(mode="json"),
            "investigation": report.data.get("investigation"),
        }

    @app.get("/workflow/invoice-history")
    def invoice_history(customer: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Prior invoices Agent 1 compares against."""

        repository = InvoiceHistoryRepository(app.state.workflow_repository.path)
        records = repository.list_records(customer=customer, limit=limit)
        return [
            {
                "record_id": record.record_id,
                "invoice_number": record.invoice_number,
                "vendor": record.vendor,
                "customer": record.customer,
                "amount": str(record.amount) if record.amount is not None else None,
                "currency": record.currency,
                "issue_date": record.issue_date,
                "description": record.description,
            }
            for record in records
        ]

    @app.post("/workflow/cases/{case_id}/execute", response_model=WorkflowCase)
    def execute_case(case_id: str, request: dict[str, Any] | None = None) -> WorkflowCase:
        """Execute (or re-inspect) the approved synthetic payment for a case."""

        payload = request or {}
        try:
            return app.state.orchestrator.execute_payment(
                case_id,
                simulate=payload.get("simulate"),
                failure_reason=payload.get("failure_reason"),
            )
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/workflow/cases/{case_id}/receipt")
    def get_receipt(case_id: str) -> dict[str, Any]:
        """The verified synthetic payment receipt for a case, if settled."""

        case = app.state.orchestrator.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        receipt = case.receipt
        if receipt is None:
            raise HTTPException(status_code=404, detail="No receipt: payment has not settled.")
        return receipt.model_dump(mode="json")

    @app.get("/workflow/invoices/{invoice_number}/payments")
    def invoice_payments(invoice_number: str) -> dict[str, Any]:
        """Every payment attempt for an invoice, including failed and pending ones."""

        attempts = app.state.orchestrator.execution.store.list_for_invoice(invoice_number)
        return {
            "invoice_number": invoice_number,
            "count": len(attempts),
            "settled": [a.model_dump(mode="json") for a in attempts if a.status == "SUCCESS"],
            "pending": [a.model_dump(mode="json") for a in attempts if a.status == "PENDING"],
            "failed": [a.model_dump(mode="json") for a in attempts if a.status == "FAILED"],
            "attempts": [a.model_dump(mode="json") for a in attempts],
        }

    # ------------------------------------------------------------------
    # Bank statement import (Agent 2) - never routed through Agent 3
    # ------------------------------------------------------------------
    @app.post("/bank/import")
    async def import_bank_statement(
        file: Annotated[UploadFile, File(description="Synthetic bank statement (PDF or CSV)")],
        authorized_test_mode: bool = False,
        developer_key: str | None = None,
    ) -> dict[str, Any]:
        """Developer-only: import a simulated bank statement as test evidence.

        The customer workflow never calls this. Bank evidence is produced
        automatically by the payment simulator, so this endpoint exists only for
        developer testing and requires the configured developer key.
        """

        if not authorized_test_mode:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Bank import is a developer-only testing tool. Customers never upload bank "
                    "statements; GhostLedger creates bank records automatically."
                ),
            )
        expected_key = (os.getenv("GHOSTLEDGER_DEVELOPER_KEY") or "").strip()
        if not expected_key:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Developer bank import is disabled. Set GHOSTLEDGER_DEVELOPER_KEY to enable "
                    "this testing tool."
                ),
            )
        if (developer_key or "").strip() != expected_key:
            raise HTTPException(status_code=403, detail="Invalid developer key.")

        from workflow.bank_importer import (
            BankImportError,
            BankTransactionStore,
            document_hash,
            parse_csv,
            parse_pdf_text,
        )

        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="The statement is too large.")

        name = (file.filename or "statement").lower()
        try:
            if name.endswith(".csv") or (file.content_type or "").startswith("text/"):
                rows = parse_csv(content)
                source_type = "csv"
            else:
                from agent3.document_reader import read_pdf_upload

                document = read_pdf_upload(file.filename or "statement.pdf", content)
                rows = parse_pdf_text(document.text)
                source_type = "pdf"
        except BankImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"Could not read the statement: {exc}") from exc

        store = BankTransactionStore(app.state.workflow_repository.path)
        result = store.import_rows(
            rows,
            filename=file.filename,
            source_type=source_type,
            digest=document_hash(content),
        )
        result["source_type"] = source_type
        result["simulated"] = True
        result["transactions"] = store.list_transactions()
        return result

    # ------------------------------------------------------------------
    # Developer-only failure simulator (Agent 4 testing)
    # ------------------------------------------------------------------
    def _require_developer(developer_key: str | None = None) -> None:
        """Allow the failure simulator only on a local development machine.

        No separate password is needed: the guard is "this request came from
        localhost". Set GHOSTLEDGER_DISABLE_SIMULATOR=1 to switch it off.
        """

        if (os.getenv("GHOSTLEDGER_DISABLE_SIMULATOR") or "").strip().lower() in {"1", "true", "yes"}:
            raise HTTPException(
                status_code=403,
                detail="The failure simulator is disabled on this deployment.",
            )

    def _is_local_request(request: Request) -> bool:
        host = (request.client.host if request.client else "") or ""
        return host in {"127.0.0.1", "::1", "localhost", "testclient"}

    @app.get("/developer/failure-scenarios")
    def list_failure_scenarios(request: Request) -> dict[str, Any]:
        _require_developer()
        if not _is_local_request(request):
            raise HTTPException(status_code=403, detail="Developer tools are local-only.")
        from workflow.failure_simulator import failure_scenarios

        return {"simulated": True, "scenarios": failure_scenarios()}

    @app.post("/developer/simulate-failure")
    def simulate_failure(
        request: Request,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a synthetic failed payment so Agent 4 can be tested."""

        _require_developer()
        if not _is_local_request(request):
            raise HTTPException(status_code=403, detail="Developer tools are local-only.")
        from decimal import Decimal

        from workflow.failure_simulator import FailureSimulationError, FailureSimulator

        payload = payload or {}
        try:
            result = FailureSimulator(app.state.workflow_repository.path).simulate(
                invoice_number=payload.get("invoice_number"),
                amount=Decimal(str(payload.get("amount") or "0")),
                currency=payload.get("currency") or "USD",
                payee=payload.get("payee"),
                customer_name=payload.get("customer_name"),
                failure_code=payload.get("failure_code") or payload.get("scenario"),
            )
        except FailureSimulationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid amount: {exc}") from exc
        return {"simulated": True, "disclaimer": "SIMULATED failure. No real money moved.", **result}

    @app.get("/bank/ledger")
    def mock_bank_ledger(
        search: str | None = None,
        status: str | None = None,
        invoice: str | None = None,
    ) -> dict[str, Any]:
        """The Mock Bank Ledger: every transaction the simulator created."""

        from workflow.mock_bank import MockBank

        bank = MockBank(app.state.workflow_repository.path)
        rows = bank.ledger()
        if invoice:
            rows = [r for r in rows if (r.get("invoice_number") or "").casefold() == invoice.casefold()]
        if status:
            wanted = status.upper()
            rows = [r for r in rows if (r.get("status") or "").upper() == wanted]
        if search:
            needle = search.casefold()
            rows = [
                r for r in rows
                if needle in " ".join(
                    str(r.get(f) or "") for f in (
                        "entry_id", "payment_reference", "invoice_number",
                        "customer_name", "vendor_name", "description", "scenario",
                    )
                ).casefold()
            ]
        balances = bank.balances()
        return {
            "simulated": True,
            "disclaimer": "SIMULATED - NO REAL MONEY. All transactions are fictional.",
            "count": len(rows),
            "balances": {k: f"{v:.2f}" for k, v in balances.items()},
            "transactions": rows,
        }

    @app.get("/bank/ledger/{entry_id}")
    def mock_bank_entry(entry_id: str) -> dict[str, Any]:
        """One bank transaction with its complete settlement history."""

        from workflow.mock_bank import MockBank

        bank = MockBank(app.state.workflow_repository.path)
        entry = bank.entry(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Bank transaction not found.")
        history = bank.history_for_reference(entry["payment_reference"]) if entry["payment_reference"] else []
        instruction = None
        if entry.get("instruction_id"):
            for row in history:
                instruction = row.get("instruction_id")
                break
        return {
            "simulated": True,
            "disclaimer": "SIMULATED - NO REAL MONEY.",
            "transaction": entry,
            "settlement_history": history,
            "history_count": len(history),
        }

    @app.get("/bank/transactions")
    def list_bank_transactions(include_outgoing: bool = False) -> dict[str, Any]:
        from workflow.bank_importer import BankTransactionStore

        store = BankTransactionStore(app.state.workflow_repository.path)
        transactions = store.list_transactions(include_outgoing=include_outgoing)
        return {
            "count": len(transactions),
            "simulated": True,
            "transactions": transactions,
            "imports": store.import_history(),
        }

    @app.post("/bank/reconcile")
    def reconcile_bank() -> dict[str, Any]:
        """Compare expected payments against imported bank evidence."""

        from workflow.bank_reconciliation import ReconciliationService

        report = ReconciliationService(app.state.workflow_repository.path).reconcile()
        return report.model_dump(mode="json")

    @app.get("/workflow/agents")
    def list_agents() -> list[dict[str, str]]:
        from workflow.schemas import AGENT_LABELS

        return [{"agent": agent.value, "name": label} for agent, label in AGENT_LABELS.items()]

    @app.get("/workflow/bank-transactions")
    def list_bank_transactions(limit: int = 100) -> list[dict[str, Any]]:
        return [
            txn.model_dump(mode="json")
            for txn in app.state.workflow_repository.list_bank_transactions(limit=limit)
        ]

    @app.get("/agent3/results/{document_id}", response_model=AnalysisResult)
    def get_result(document_id: str) -> AnalysisResult:
        outcome = app.state.service.repository.get(document_id)
        if not outcome:
            raise HTTPException(status_code=404, detail="No processed document was found.")
        return outcome

    @app.get("/agent3/synthetic/customers")
    def synthetic_customers() -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for record in catalog()["records"]:
            group = groups.setdefault(
                record["customer_id"],
                {"customer_id": record["customer_id"], "customer_name": record["customer_name"], "document_count": 0},
            )
            group["document_count"] += 1
        return sorted(groups.values(), key=lambda item: item["customer_id"])

    @app.get("/agent3/synthetic/documents")
    def synthetic_documents(customer_id: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "synthetic_id": record["synthetic_id"],
                "filename": record["filename"],
                "customer_id": record["customer_id"],
                "document_type": record["document"]["document_type"],
                "anomalies": record["anomalies"],
            }
            for record in catalog()["records"]
            if customer_id is None or record["customer_id"] == customer_id
        ]

    @app.post("/agent3/synthetic/analyze/{synthetic_id}", response_model=AnalysisResult)
    def analyze_synthetic(synthetic_id: str) -> AnalysisResult:
        record = next((item for item in catalog()["records"] if item["synthetic_id"] == synthetic_id), None)
        if not record:
            raise HTTPException(status_code=404, detail="Synthetic document was not found.")
        path = app.state.dataset_directory / "documents" / record["filename"]
        try:
            document = read_upload(record["filename"], path.read_bytes(), "text/plain")
        except (OSError, DocumentReadError) as exc:
            raise HTTPException(status_code=500, detail="The synthetic document could not be loaded.") from exc
        return app.state.service.analyze(document)

    @app.post("/agent3/synthetic/evaluate")
    def evaluate_synthetic_documents(request: Request) -> dict[str, Any]:
        report = evaluate_dataset(request.app.state.dataset_directory)
        report_path = request.app.state.dataset_directory / "evaluation_report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    return app


app = create_app()
