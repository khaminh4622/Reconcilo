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
from workflow.agent1_history import InvoiceHistoryRepository, normalize_invoice_number, normalize_name
from workflow.orchestrator import WorkflowError, WorkflowOrchestrator
from workflow.repository import WorkflowRepository
from workflow.schemas import AgentId, CaseSummary, WorkflowCase, WorkflowCaseView
from workflow.seed import synthetic_bank_seed


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "20000"))


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
    # When a service is injected (tests, alternate deployments) its database is
    # authoritative, so accounts and cases always live in the same file.
    database_path = os.getenv("AGENT3_DB_PATH", str(BASE_DIR / "data" / "agent3.db"))
    try:
        service_db = app.state.service.repository.path
        if service_db:
            database_path = str(service_db)
    except AttributeError:
        pass
    workflow_repository = WorkflowRepository(database_path)
    if os.getenv("AGENT3_SEED_BANK", "1") != "0":
        workflow_repository.seed_bank_transactions(synthetic_bank_seed())
    app.state.workflow_repository = workflow_repository
    app.state.orchestrator = WorkflowOrchestrator(workflow_repository)

    # The simulated bank's accounts, payment requests and audit log. It shares
    # the same SQLite file as everything else and is the only component that
    # moves a simulated balance.
    from workflow.bank_admin_api import install_bank_admin_routes
    from workflow.mock_bank_accounts import MockBankStore

    app.state.bank_store = MockBankStore(workflow_repository.path)
    app.state.orchestrator.bind_bank(app.state.bank_store)
    # Agent 4 support conversations use the application's configured LLM for
    # prose only.  Payment and bank facts are gathered separately through
    # case-scoped, read-only tools.
    from workflow.agent4_support import Agent4SupportAssistant

    app.state.agent4_support = Agent4SupportAssistant(
        workflow_repository, app.state.service.extractor
    )

    # Accounts, sessions and the tenancy helpers. Ownership lives in the same
    # database as everything else, so history survives restarts.
    from workflow.accounts import ensure_bootstrap_admin
    from workflow.auth_api import install_auth
    from workflow.tenancy import migrate_tenancy

    # Tables are created lazily by their owners, so make sure every owned table
    # exists before attempting to add ownership columns to it.
    from workflow.mock_bank import MockBank as _MockBank

    _MockBank(workflow_repository.path)
    migrate_tenancy(workflow_repository.path, take_backup=True)
    accounts = install_auth(app, workflow_repository.path)
    ensure_bootstrap_admin(accounts, workflow_repository.path)

    # Preserve historical uploads, but detach any old case that was linked to
    # a different customer's bank request by the pre-tenancy invoice lookup.
    # This is a data repair, not a deletion: the original request and every
    # settlement ledger row remain immutable.
    from workflow.bank_admin import repair_cross_customer_payment_links

    app.state.payment_link_repair = repair_cross_customer_payment_links(app)

    # Local demos: create the four seeded logins so a fresh clone can sign in.
    # Opt-in, idempotent, and refused when GHOSTLEDGER_ENV=production.
    if os.getenv("GHOSTLEDGER_DEMO_ACCOUNTS", "0") == "1":
        from tools.seed_demo_accounts import LOCAL_DEFAULT_PASSWORD, SeedRefused, seed

        try:
            seed(workflow_repository.path, os.getenv("DEMO_ACCOUNT_PASSWORD") or LOCAL_DEFAULT_PASSWORD)
        except SeedRefused as exc:
            print(f"Demo accounts not created: {exc}")

    # The Orion / Blue Harbor demo case and its simulated funding. Idempotent.
    # Opt-in with GHOSTLEDGER_SEED_DEMO_CASES=1 AND demo logins enabled, so a
    # bare test run or a production deployment never seeds demo rows.
    if (
        os.getenv("GHOSTLEDGER_DEMO_ACCOUNTS", "0") == "1"
        and os.getenv("GHOSTLEDGER_SEED_DEMO_CASES", "0") == "1"
    ):
        from workflow.demo_bank_seed import seed_demo_bank

        app.state.demo_seed = seed_demo_bank(app)
    else:
        app.state.demo_seed = {"skipped_reason": "demo accounts or demo cases disabled"}

    from workflow.admin_api import install_admin_routes
    from workflow.agent_bank_api import install_agent_bank_routes
    from workflow.bank_admin_api import install_bank_admin_routes

    install_admin_routes(app)
    install_bank_admin_routes(app)
    install_agent_bank_routes(app)

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

    # ------------------------------------------------------------------
    # Accounts: sign up, sign in, sign out, who am I
    # ------------------------------------------------------------------
    @app.post("/auth/signup")
    def signup(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create a brand-new account with no history of any kind."""

        from workflow.accounts import AuthError

        body = payload or {}
        try:
            # Public signup always creates a CUSTOMER. Any "role" in the body is
            # ignored on purpose; admin accounts come from the seed tool.
            account = app.state.accounts.sign_up(
                email=body.get("email"),
                password=body.get("password"),
                display_name=body.get("display_name"),
            )
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        _account, token = app.state.accounts.sign_in(
            email=account.email, password=body.get("password")
        )
        return {
            "account": account.public(),
            "token": token,
            "payment_history": [],
            "cases": [],
            "message": "No payment history yet. Upload your first invoice to get started.",
        }

    @app.post("/auth/signin")
    def signin(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Sign in and return the account's own history. Never resets it."""

        from workflow.accounts import AuthError

        body = payload or {}
        try:
            account, token = app.state.accounts.sign_in(
                email=body.get("email"), password=body.get("password")
            )
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        history = _account_history(account.account_id, account.role)
        return {
            "account": account.public(),
            "token": token,
            **history,
            "message": (
                f"Welcome back. {len(history['payment_history'])} payment(s) on record."
                if history["payment_history"]
                else "No payment history yet. Upload your first invoice to get started."
            ),
        }

    @app.post("/auth/signout")
    def signout(request: Request) -> dict[str, Any]:
        resolver = request.app.state.account_context
        token = None
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        token = token or request.headers.get("x-account-token") or request.query_params.get("token")
        if token:
            app.state.accounts.sign_out(token)
        return {"signed_out": True}

    @app.get("/auth/me")
    def whoami(request: Request) -> dict[str, Any]:
        from workflow.auth_api import current_account

        account = current_account(request)
        if account is None:
            raise HTTPException(status_code=401, detail="Not signed in.")
        history = _account_history(account.account_id, account.role)
        return {"account": account.public(), **history}

    def _account_history(account_id: str, role: str) -> dict[str, Any]:
        """Everything this account owns, read from the database."""

        from workflow.accounts import ADMIN_ROLES

        orchestrator = app.state.orchestrator
        cases = orchestrator.repository.list_cases(limit=200)
        is_admin = role in ADMIN_ROLES
        owned_cases = [
            c for c in cases
            if is_admin or getattr(c, "owner_account_id", None) == account_id
        ]

        # Payments come from the simulator's attempt store, which is the
        # authoritative record of money movement for a case.
        payments: list[dict[str, Any]] = []
        seen: set[str] = set()
        for case in owned_cases:
            for attempt in orchestrator.simulator.store.list_for_invoice(case.invoice_number):
                if attempt.attempt_id in seen:
                    continue
                seen.add(attempt.attempt_id)
                payments.append(
                    {
                        "attempt_id": attempt.attempt_id,
                        "invoice_number": attempt.invoice_number,
                        "amount": str(attempt.amount) if attempt.amount is not None else None,
                        "currency": attempt.currency,
                        "status": attempt.status,
                        "verified": attempt.verified,
                        "reference": attempt.provider_reference,
                        "settlement_reference": attempt.settlement_reference,
                        "settled_at": attempt.settled_at,
                        "case_id": attempt.case_id,
                    }
                )

        # Summaries carry no receipt, so read the owned cases in full.
        owned_ids = {c.case_id for c in owned_cases}
        receipts = []
        for case in orchestrator.repository.list_full_cases(limit=200):
            if case.case_id in owned_ids and case.receipt is not None:
                receipts.append(
                    {
                        "receipt_id": case.receipt.receipt_id,
                        "case_id": case.case_id,
                        "invoice_number": case.receipt.invoice_number,
                        "amount": str(case.receipt.amount) if case.receipt.amount else None,
                        "currency": case.receipt.currency,
                        "settled_at": case.receipt.settled_at,
                    }
                )
        return {
            "payment_history": payments,
            "cases": [
                {
                    "case_id": c.case_id,
                    "invoice_number": c.invoice_number,
                    "status": c.status.value,
                    "intent": c.intent.value,
                    "amount": str(c.amount) if c.amount is not None else None,
                    "currency": c.currency,
                    "created_at": c.created_at,
                    "outcome": c.outcome,
                    "vendor_name": c.vendor_name,
                }
                for c in owned_cases
            ],
            "receipts": receipts,
            # The customer's OWN payment requests and simulated account, read
            # from the bank. Never the ledger, and never anyone else's rows.
            "bank_payments": _own_bank_payments(account_id, role, owned_cases),
            "bank_account": _own_bank_account(account_id, role),
        }

    def _own_bank_payments(account_id: str, role: str, owned_cases) -> list[dict[str, Any]]:
        """Payment requests for this account only, or every request for admins."""

        from workflow.accounts import ADMIN_ROLES

        store = app.state.bank_store
        if role in ADMIN_ROLES:
            rows = store.payment_requests(limit=200)
        else:
            rows = store.payment_requests(customer_id=account_id, limit=200)
        titles = {c.case_id: c for c in owned_cases}
        return [
            {
                "payment_id": row["payment_id"],
                "case_id": row["case_id"],
                "invoice_number": row["invoice_id"],
                "payee": row["payee"],
                "amount": row["amount"],
                "currency": row["currency"],
                "status": row["status"],
                "settlement_reference": row["settlement_reference"],
                "failure_code": row["failure_code"],
                "failure_reason": row["failure_reason"],
                "created_at": row["created_at"],
                "processed_at": row["processed_at"],
                "case_status": (titles.get(row["case_id"]).status.value if titles.get(row["case_id"]) else None),
            }
            for row in rows
        ]

    def _own_bank_account(account_id: str, role: str) -> dict[str, Any] | None:
        """The signed-in customer's simulated account, if they were onboarded."""

        from workflow.accounts import ADMIN_ROLES

        if role in ADMIN_ROLES:
            return None
        account = app.state.bank_store.account_for_owner(account_id)
        if account is None:
            return None
        return {
            "account_id": account["account_id"],
            "account_number_masked": account["account_number_masked"],
            "currency": account["currency"],
            "balance": account["balance"],
            "status": account["status"],
            "simulated": True,
        }

    @app.get("/customers", response_model=list[CustomerProfile])
    def list_customers(request: Request) -> list[CustomerProfile]:
        """List the fictional reference records used for matching. Admins only."""

        from workflow.auth_api import require_admin

        require_admin(request)
        return app.state.service.repository.list_customers()

    @app.get("/customers/{customer_id}", response_model=CustomerProfile)
    def get_customer(customer_id: str, request: Request) -> CustomerProfile:
        from workflow.auth_api import require_admin

        require_admin(request)
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
        http_request: Request,
        file: Annotated[UploadFile, File(description="Customer financial PDF document")],
    ) -> WorkflowCase:
        """Agent 3 reads the PDF, validates it, and the orchestrator routes the case.

        The case is owned by the signed-in account, so its history stays with
        that account forever.
        """

        from workflow.auth_api import require_customer

        account = require_customer(http_request)
        document = await _read_upload(file)
        analysis = app.state.service.analyze(document)
        return _open_and_route(
            analysis, account, filename=document.filename or "document.pdf", channel="upload"
        )

    @app.get("/workflow/cases", response_model=list[CaseSummary])
    def list_cases(request: Request, limit: int = 50) -> list[CaseSummary]:
        """Only this account's cases. Admins may pass all=true."""

        from workflow.auth_api import current_account

        account = current_account(request)
        cases = app.state.workflow_repository.list_cases(limit=limit)
        if account is None:
            return []
        if account.is_admin:
            return cases
        return [
            c for c in cases
            if getattr(c, "owner_account_id", None) == account.account_id
        ]

    @app.get("/workflow/invoices")
    def list_canonical_invoices(request: Request) -> list[dict[str, Any]]:
        """One customer-facing row per payable invoice, with upload history.

        A document upload is a case, but a customer's invoice is a business
        record.  Grouping only inside the authenticated owner's records keeps
        re-uploads visible without turning them into duplicate dashboard rows
        or accidentally combining another customer's same-number invoice.
        """

        from workflow.auth_api import require_signed_in_customer

        account = require_signed_in_customer(request)
        owned_cases = [
            case for case in app.state.workflow_repository.list_full_cases()
            if case.owner_account_id == account.account_id
        ]
        requests_by_case = {
            row.get("case_id"): row
            for row in app.state.bank_store.payment_requests(customer_id=account.account_id, limit=1000)
            if row.get("case_id")
        }

        groups: dict[tuple[str, str, str, str], list[Any]] = {}
        for case in owned_cases:
            invoice = normalize_invoice_number(case.invoice_number)
            # A document without an invoice number is not safe to merge with
            # another incomplete document.  Its own case id makes it unique.
            if not invoice:
                invoice = f"CASE:{case.case_id}"
            key = (
                account.account_id,
                invoice,
                normalize_name(case.vendor_name),
                (case.currency or "").strip().upper(),
            )
            groups.setdefault(key, []).append(case)

        def request_rank(case) -> tuple[int, str]:
            bank = requests_by_case.get(case.case_id) or {}
            status = bank.get("status")
            # The request that owns money movement is the canonical invoice
            # state.  A later duplicate upload must not hide that state.
            rank = {
                "PENDING_APPROVAL": 4,
                "APPROVED": 4,
                "PROCESSING": 4,
                "UNCERTAIN": 4,
                "SETTLED": 3,
                "FAILED": 2,
                "REJECTED": 2,
            }.get(status, 1 if case.status.value == "AWAITING_APPROVAL" else 0)
            return rank, case.created_at

        rows: list[dict[str, Any]] = []
        for identity, cases in groups.items():
            ordered = sorted(cases, key=lambda c: c.created_at)
            primary = max(ordered, key=request_rank)
            bank = requests_by_case.get(primary.case_id)
            uploads = [
                {
                    "case_id": case.case_id,
                    "created_at": case.created_at,
                    "updated_at": case.updated_at,
                    "status": case.status.value,
                    "source_channel": case.source_channel,
                    "filename": case.documents[0].filename if case.documents else None,
                    "bank_status": (requests_by_case.get(case.case_id) or {}).get("status"),
                }
                for case in ordered
            ]
            rows.append(
                {
                    "canonical_id": "|".join(identity),
                    "case_id": primary.case_id,
                    "invoice_number": primary.invoice_number,
                    "vendor_name": primary.vendor_name,
                    "customer_name": primary.customer_name,
                    "amount": str(primary.amount) if primary.amount is not None else None,
                    "currency": primary.currency,
                    "status": bank.get("status") if bank else primary.status.value,
                    "case_status": primary.status.value,
                    "bank_status": bank.get("status") if bank else None,
                    "created_at": ordered[0].created_at,
                    "updated_at": max(case.updated_at for case in ordered),
                    "upload_count": len(ordered),
                    "uploads": uploads,
                }
            )
        return sorted(rows, key=lambda row: row["updated_at"], reverse=True)

    @app.post("/workflow/cases/message", response_model=WorkflowCase, status_code=201)
    def open_message_case(http_request: Request, payload: dict[str, Any] | None = None) -> WorkflowCase:
        """A customer describes a problem in words instead of uploading a PDF.

        The text runs through the same Agent 3 analysis, case opening, ownership
        stamping and routing as an upload. A message without enough to act on
        goes to human review saying what is missing.
        """

        from agent3.document_reader import read_plain_text
        from workflow.auth_api import require_signed_in_customer

        account = require_signed_in_customer(http_request)
        message = (payload or {}).get("message")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="Write a message describing what you need.")
        if len(message) > MAX_MESSAGE_CHARS:
            raise HTTPException(
                status_code=413, detail=f"Messages are limited to {MAX_MESSAGE_CHARS:,} characters."
            )
        try:
            document = read_plain_text(message)
        except DocumentReadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _open_and_route(
            app.state.service.analyze(document), account,
            filename="customer message", channel="message", source_text=document.text,
        )

    def _open_and_route(analysis, account, *, filename: str, channel: str, source_text=None) -> WorkflowCase:
        """Open a case from Agent 3's analysis, stamp the owner, then route it."""

        orchestrator: WorkflowOrchestrator = app.state.orchestrator
        case = orchestrator.open_case(analysis, filename=filename, channel=channel, source_text=source_text)
        # Stamp ownership before routing, so every later write inherits it.
        case.owner_account_id = account.account_id
        orchestrator.repository.save_case(case)
        return orchestrator.route(case)

    @app.get("/workflow/approvals")
    def approvals_queue(request: Request) -> dict[str, Any]:
        """Cases awaiting a bank-side decision, each with Agent 1's result. Admins only."""

        from workflow.approvals import approval_queue
        from workflow.auth_api import require_admin

        require_admin(request)
        items = approval_queue(app.state.workflow_repository, app.state.accounts)
        return {"count": len(items), "approvals": items}

    @app.get("/workflow/cases/{case_id}", response_model=WorkflowCaseView)
    def get_case(case_id: str, request: Request) -> WorkflowCaseView:
        from workflow.approvals import approval_policy
        from workflow.auth_api import assert_owner, require_customer

        account = require_customer(request)
        case = app.state.orchestrator.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        assert_owner(account, getattr(case, "owner_account_id", None))
        from workflow.repository import summarise_for_human

        why, problems, next_action = summarise_for_human(case)
        return WorkflowCaseView.model_validate(
            {
                **case.model_dump(),
                "approval_policy": approval_policy(case, app.state.accounts),
                "why": why or None,
                "problems": problems,
                "next_action": next_action or None,
            }
        )

    def _agent4_chat_case(case_id: str, request: Request):
        """Resolve a support-chat case with the same ownership rules as its detail page."""

        from workflow.auth_api import assert_owner, require_customer

        account = require_customer(request)
        case = app.state.orchestrator.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        assert_owner(account, getattr(case, "owner_account_id", None))
        return case, account

    @app.get("/workflow/cases/{case_id}/agent4/conversation")
    def agent4_conversation(case_id: str, request: Request) -> dict[str, Any]:
        """Persistent, case-scoped Agent 4 support history and verified status."""

        case, account = _agent4_chat_case(case_id, request)
        return app.state.agent4_support.conversation(case, account=account)

    @app.post("/workflow/cases/{case_id}/agent4/conversation")
    def send_agent4_message(
        case_id: str, request: Request, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Store one customer/staff message and return Agent 4's grounded reply."""

        from workflow.agent4_support import MAX_SUPPORT_MESSAGE_CHARS

        case, account = _agent4_chat_case(case_id, request)
        body = payload or {}
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="Write a message for Agent 4.")
        if len(message) > MAX_SUPPORT_MESSAGE_CHARS:
            raise HTTPException(
                status_code=413,
                detail=f"Messages are limited to {MAX_SUPPORT_MESSAGE_CHARS:,} characters.",
            )
        action_id = body.get("action_id")
        if action_id is not None and not isinstance(action_id, str):
            raise HTTPException(status_code=400, detail="The suggested action is not valid.")
        try:
            reply = app.state.agent4_support.reply(
                case,
                account_id=account.account_id,
                actor="customer" if account.role == "CUSTOMER" else "staff",
                message=message,
                action_id=action_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # The assistant already saved the case when it escalated. Record the
        # conversation turn for audit, then save the refreshed case.
        case.add_audit(
            AgentId.agent_4,
            "support_chat_reply",
            "Agent 4 supplied a case-scoped support response; no bank action was taken.",
            actor_account_id=account.account_id,
        )
        app.state.workflow_repository.save_case(case)
        return {
            "case_id": case.case_id,
            **reply,
            "conversation": app.state.agent4_support.conversation(case, account=account),
        }

    @app.post("/workflow/cases/{case_id}/agent4/escalate")
    def escalate_agent4_case(
        case_id: str, request: Request, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Move an unresolved support case to human review.

        This changes only the case's review status. It cannot approve, execute,
        retry, or settle a payment, and it does not touch the Mock Bank.
        """

        case, account = _agent4_chat_case(case_id, request)
        reason = (payload or {}).get("reason")
        if reason is not None and not isinstance(reason, str):
            raise HTTPException(status_code=400, detail="The escalation reason is not valid.")
        result = app.state.agent4_support.escalate(
            case, account_id=account.account_id, reason=reason
        )
        return {
            "case_id": case.case_id,
            **result,
            "conversation": app.state.agent4_support.conversation(case, account=account),
        }

    @app.post("/workflow/cases/{case_id}/agent4/request-payment", status_code=201)
    def agent4_request_payment(case_id: str, request: Request) -> dict[str, Any]:
        """Prepare a *new* verified payment request from an existing invoice.

        This route only starts Agent 3's verified intake and Agent 1's duplicate
        check. It does not approve, process, or settle a payment. The existing
        Mock Bank identity guard remains the final protection against duplicate
        live or settled requests.
        """

        from workflow.agent4_support import payment_intake_analysis
        from workflow.auth_api import assert_owner, require_signed_in_customer

        account = require_signed_in_customer(request)
        case = app.state.orchestrator.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        assert_owner(account, getattr(case, "owner_account_id", None))
        if not all((case.invoice_number, case.vendor_name, case.amount, case.currency)):
            raise HTTPException(
                status_code=422,
                detail="This case does not contain the invoice number, vendor, amount and currency needed for a new request.",
            )

        evidence = app.state.agent4_support.evidence(case)
        bank_status = ((evidence.get("bank") or {}).get("status") or "").upper()
        if evidence.get("settlement_verified") or bank_status == "SETTLED":
            raise HTTPException(status_code=409, detail="This invoice already has a verified settled payment.")
        if bank_status in {"PENDING_APPROVAL", "APPROVED", "PROCESSING", "UNCERTAIN"}:
            raise HTTPException(
                status_code=409,
                detail="An existing payment is still active or uncertain, so GhostLedger will not create another request.",
            )
        # The Mock Bank's own identity guard is checked before a new case is
        # created, so a second click cannot add a parallel live instruction.
        store = app.state.bank_store
        identity = {
            "customer_id": case.owner_account_id,
            "payee": case.vendor_name,
            "currency": case.currency,
        }
        settled = store.settled_request_for_invoice(case.invoice_number, **identity)
        if settled is not None:
            raise HTTPException(status_code=409, detail="This invoice already has a verified settled payment.")
        live = store.live_request_for_invoice(case.invoice_number, **identity)
        if live is not None:
            raise HTTPException(
                status_code=409,
                detail="An existing payment request is still active or uncertain, so GhostLedger will not create another request.",
            )
        if evidence.get("bank_error") or bank_status == "UNAVAILABLE":
            raise HTTPException(
                status_code=503,
                detail="Bank evidence is unavailable, so a new payment request cannot be prepared safely.",
            )
        if not evidence.get("can_request_payment"):
            raise HTTPException(
                status_code=409,
                detail=evidence.get("reason_not_requestable")
                or "This case is not eligible for a new payment request.",
            )

        intake_message = (
            f"Please pay invoice {case.invoice_number} from {case.vendor_name} for "
            f"{case.amount} {case.currency}."
        )
        # Agent 3's verified facts from the original case are reused; there is
        # no new document to read, so no second model extraction is needed.
        analysis = payment_intake_analysis(case)
        new_case = _open_and_route(
            analysis,
            account,
            filename="Agent 4 payment support request",
            channel="message",
            source_text=intake_message,
        )
        new_case.add_audit(
            AgentId.agent_3,
            "verified_invoice_facts_reused",
            (
                f"Agent 4 prepared this request from the invoice, vendor, amount and currency "
                f"Agent 3 verified on case {case.case_id}. Agent 1 must still clear duplicates."
            ),
            actor_account_id=account.account_id,
        )

        request_row = store.payment_request(getattr(new_case, "payment_request_id", None))
        if new_case.status.value == "AWAITING_APPROVAL" and request_row is not None:
            explanation = (
                f"I prepared a new verified payment request for {case.invoice_number}. "
                f"Agent 1 checked for duplicates and the Mock Bank now holds request "
                f"{request_row['payment_id']} in PENDING_APPROVAL. No money has moved. "
                "A bank administrator must approve or decline it. After approval, Agent 5 "
                "submits the simulated payment, Agent 2 reconciles the bank entries, and "
                "Agent 5 confirms the result only when both sides settle."
            )
        elif new_case.status.value == "AWAITING_APPROVAL":
            explanation = (
                f"I started a new verification for {case.invoice_number}, but no Mock Bank "
                "request could be opened (the customer may not have a simulated account). "
                "No money has moved; a person must review the case."
            )
        else:
            explanation = (
                f"I started a new verification for {case.invoice_number}, but it did not reach "
                f"bank approval. The case is {new_case.status.value.replace('_', ' ').lower()}"
                + (f": {new_case.outcome}" if new_case.outcome else ".")
                + " No money has moved and nothing was approved."
            )

        app.state.agent4_support.record_payment_intake(
            case,
            account_id=account.account_id,
            result=explanation,
            customer_message="Please prepare a new payment request for this invoice.",
        )
        case.add_audit(
            AgentId.agent_4,
            "support_payment_intake_started",
            (
                f"Customer asked Agent 4 to prepare a new verification case {new_case.case_id}; "
                "no approval was granted and no money moved."
            ),
            actor_account_id=account.account_id,
        )
        app.state.workflow_repository.save_case(case)
        app.state.workflow_repository.save_case(new_case)
        return {
            "case": new_case.model_dump(mode="json"),
            "source_case_id": case.case_id,
            "payment_request": (
                {
                    "payment_id": request_row["payment_id"],
                    "status": request_row["status"],
                    "amount": request_row["amount"],
                    "currency": request_row["currency"],
                    "invoice_number": request_row["invoice_id"],
                }
                if request_row
                else None
            ),
            "message": explanation,
            "conversation": app.state.agent4_support.conversation(case, account=account),
        }

    def _case_for_decision(case_id: str, http_request: Request):
        """The case and the bank-side account allowed to decide on it."""

        from workflow.auth_api import assert_can_approve, assert_owner, require_customer

        account = require_customer(http_request)
        existing = app.state.orchestrator.get(case_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        # Ownership first: a customer probing another account's case gets 404.
        assert_owner(account, getattr(existing, "owner_account_id", None))
        assert_can_approve(account, getattr(existing, "owner_account_id", None))
        return existing, account

    @app.post("/workflow/cases/{case_id}/approve", response_model=WorkflowCase)
    def approve_case(case_id: str, http_request: Request, request: dict[str, Any] | None = None) -> WorkflowCase:
        """Record a bank-side approval and run the synthetic payment.

        The approver is the signed-in account; any ``approved_by`` in the body
        is ignored.
        """

        _existing, account = _case_for_decision(case_id, http_request)
        body = request or {}
        try:
            return app.state.orchestrator.approve(
                case_id,
                approved_by=f"{account.display_name} ({account.account_id})",
                approver_account_id=account.account_id,
                note=body.get("note"),
                simulate=str(body.get("simulate") or "SUCCESS"),
                failure_reason=body.get("failure_reason"),
                failure_code=body.get("failure_code"),
                # Two-step demos pass process=false and then submit the
                # authorised request from the Bank admin page.
                process=bool(body.get("process", True)),
            )
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/workflow/cases/{case_id}/reject", response_model=WorkflowCase)
    def reject_case(case_id: str, http_request: Request, request: dict[str, Any] | None = None) -> WorkflowCase:
        """Record a bank-side rejection. The rejector is the signed-in account."""

        _existing, account = _case_for_decision(case_id, http_request)
        body = request or {}
        try:
            return app.state.orchestrator.reject(
                case_id,
                f"{account.display_name} ({account.account_id})",
                body.get("reason"),
                rejector_account_id=account.account_id,
            )
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/workflow/cases/{case_id}/investigation")
    def get_investigation(case_id: str, request: Request) -> dict[str, Any]:
        """Agent 1's duplicate investigation for a case, if it has run."""

        from workflow.auth_api import assert_owner, require_customer

        account = require_customer(request)
        case = app.state.orchestrator.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        assert_owner(account, getattr(case, "owner_account_id", None))
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
    def invoice_history(request: Request, customer: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Prior invoices Agent 1 compares against, for this account only."""

        from workflow.auth_api import require_customer

        account = require_customer(request)
        repository = InvoiceHistoryRepository(app.state.workflow_repository.path)
        if account.is_admin:
            records = repository.list_records(customer=customer, limit=limit)
        else:
            # A customer sees only records for invoices on cases they own. The
            # display name is self-chosen at signup, so it cannot be the key.
            from workflow.agent1_history import normalize_invoice_number

            owned = {
                normalize_invoice_number(c.invoice_number)
                for c in app.state.workflow_repository.list_cases(limit=1000)
                if c.owner_account_id == account.account_id and c.invoice_number
            }
            records = [
                r for r in repository.list_records(limit=10_000)
                if normalize_invoice_number(r.invoice_number) in owned
            ][:limit]
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
    def execute_case(
        case_id: str, http_request: Request, request: dict[str, Any] | None = None
    ) -> WorkflowCase:
        """Execute (or re-inspect) the approved synthetic payment for a case.

        Only the case owner or an administrator. Idempotency and the no-retry
        rules still apply inside the orchestrator.
        """

        from workflow.auth_api import assert_owner, require_customer

        account = require_customer(http_request)
        existing = app.state.orchestrator.get(case_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        assert_owner(account, getattr(existing, "owner_account_id", None))
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
    def get_receipt(case_id: str, request: Request) -> dict[str, Any]:
        """The verified synthetic payment receipt for a case, if settled."""

        from workflow.auth_api import assert_owner, require_customer

        account = require_customer(request)
        case = app.state.orchestrator.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        assert_owner(account, getattr(case, "owner_account_id", None))
        receipt = case.receipt
        if receipt is None:
            raise HTTPException(status_code=404, detail="No receipt: payment has not settled.")
        return receipt.model_dump(mode="json")

    @app.get("/workflow/invoices/{invoice_number}/payments")
    def invoice_payments(invoice_number: str, request: Request) -> dict[str, Any]:
        """Every payment attempt for an invoice, including failed and pending ones.

        Administrators see every attempt. A customer sees only attempts made for
        cases they own, so an invoice number alone reveals nothing.
        """

        from workflow.auth_api import require_customer

        account = require_customer(request)
        attempts = app.state.orchestrator.execution.store.list_for_invoice(invoice_number)
        if not account.is_admin:
            orchestrator = app.state.orchestrator
            owned: dict[str, bool] = {}

            def is_owned(case_id: str | None) -> bool:
                if not case_id:
                    return False
                if case_id not in owned:
                    case = orchestrator.get(case_id)
                    owned[case_id] = bool(case and case.owner_account_id == account.account_id)
                return owned[case_id]

            attempts = [a for a in attempts if is_owned(a.case_id)]
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
        request: Request,
        search: str | None = None,
        status: str | None = None,
        invoice: str | None = None,
    ) -> dict[str, Any]:
        """The Mock Bank Ledger. Administrators only.

        Customers never see the raw ledger; their own payments appear under
        /auth/me in payment_history.
        """

        from workflow.auth_api import require_admin
        from workflow.mock_bank import MockBank

        require_admin(request)
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
    def mock_bank_entry(entry_id: str, request: Request) -> dict[str, Any]:
        """One bank transaction with its complete settlement history. Admins only."""

        from workflow.auth_api import require_admin
        from workflow.mock_bank import MockBank

        require_admin(request)

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
    def list_imported_transactions(request: Request, include_outgoing: bool = False) -> dict[str, Any]:
        """Imported statement rows. Administrators only."""

        from workflow.auth_api import require_admin
        from workflow.bank_importer import BankTransactionStore

        require_admin(request)
        store = BankTransactionStore(app.state.workflow_repository.path)
        transactions = store.list_transactions(include_outgoing=include_outgoing)
        return {
            "count": len(transactions),
            "simulated": True,
            "transactions": transactions,
            "imports": store.import_history(),
        }

    @app.post("/bank/reconcile")
    def reconcile_bank(request: Request, case_id: str | None = None) -> dict[str, Any]:
        """Compare expected payments against imported bank evidence. Admins only.

        With ``case_id`` the report is scoped to that one case, so inspecting a
        payment does not re-reconcile every customer's invoice.
        """

        from workflow.auth_api import require_admin
        from workflow.bank_reconciliation import ReconciliationService

        require_admin(request)
        service = ReconciliationService(app.state.workflow_repository.path)
        if case_id:
            case = app.state.orchestrator.get(case_id)
            if case is None:
                raise HTTPException(status_code=404, detail="Case not found.")
            request_row = app.state.bank_store.payment_request(
                getattr(case, "payment_request_id", None)
            )
            recommendation = app.state.orchestrator.reconciliation.reconcile_payment(case)
            return {
                "simulated": True,
                "case_id": case.case_id,
                "invoice_number": case.invoice_number,
                "payment_request_id": (request_row or {}).get("payment_id"),
                "bank_status": (request_row or {}).get("status"),
                "recommendation": recommendation,
            }

        report = service.reconcile()
        return report.model_dump(mode="json")

    @app.get("/workflow/agents")
    def list_agents() -> list[dict[str, str]]:
        from workflow.schemas import AGENT_LABELS

        return [{"agent": agent.value, "name": label} for agent, label in AGENT_LABELS.items()]

    @app.get("/workflow/bank-transactions")
    def list_bank_transactions(request: Request, limit: int = 100) -> list[dict[str, Any]]:
        """Legacy bank rows. Administrators only."""

        from workflow.auth_api import require_admin

        require_admin(request)
        return [
            txn.model_dump(mode="json")
            for txn in app.state.workflow_repository.list_bank_transactions(limit=limit)
        ]

    @app.get("/agent3/results/{document_id}", response_model=AnalysisResult)
    def get_result(document_id: str, request: Request) -> AnalysisResult:
        """A saved analysis. Admins only: the key is a guessable invoice number and
        saved analyses carry no owner; customers read theirs through their cases."""

        from workflow.auth_api import require_admin

        require_admin(request)
        outcome = app.state.service.repository.get(document_id)
        if not outcome:
            raise HTTPException(status_code=404, detail="No processed document was found.")
        return outcome

    @app.get("/agent3/synthetic/customers")
    def synthetic_customers(request: Request) -> list[dict[str, Any]]:
        """Development corpus. Admins only."""

        from workflow.auth_api import require_admin

        require_admin(request)
        groups: dict[str, dict[str, Any]] = {}
        for record in catalog()["records"]:
            group = groups.setdefault(
                record["customer_id"],
                {"customer_id": record["customer_id"], "customer_name": record["customer_name"], "document_count": 0},
            )
            group["document_count"] += 1
        return sorted(groups.values(), key=lambda item: item["customer_id"])

    @app.get("/agent3/synthetic/documents")
    def synthetic_documents(request: Request, customer_id: str | None = None) -> list[dict[str, Any]]:
        from workflow.auth_api import require_admin

        require_admin(request)
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
    def analyze_synthetic(synthetic_id: str, request: Request) -> AnalysisResult:
        from workflow.auth_api import require_admin

        require_admin(request)
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
        from workflow.auth_api import require_admin

        require_admin(request)
        report = evaluate_dataset(request.app.state.dataset_directory)
        report_path = request.app.state.dataset_directory / "evaluation_report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    return app


app = create_app()
