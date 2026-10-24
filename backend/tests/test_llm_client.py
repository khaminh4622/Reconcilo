"""Verify the OpenAI-compatible client contract without contacting a provider.

The client is exercised against a local HTTP server speaking the Chat
Completions protocol. This proves the request shape (structured output, the
document as untrusted data) and the response handling (response ID and token
usage) that the live integration test asserts against a real model.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent3.extractor import ExtractionError, LLMExtractor, OpenAICompatibleClient
from agent3.schemas import AnalysisSource, ProblemType


REPLY = {
    "document_type": "other",
    "document_id": "INV-2026-0441",
    "customer": {
        "customer_name": "Avery Nguyen",
        "customer_id": "CUST-001",
        "company_name": None,
        "email": None,
        "address": None,
    },
    "financial_information": {
        "currency": "USD",
        "subtotal": None,
        "tax_amount": None,
        "discount": None,
        "total_amount": "410.40",
        "amount_paid": "0.00",
        "outstanding_balance": "410.40",
        "payment_status": "Unpaid",
    },
    "detected_problem": {
        "type": "payment_status_discrepancy",
        "description": "Customer says the invoice was already paid.",
        "source_evidence": "I already paid invoice INV-2026-0441 on 2026-03-20 by bank transfer.",
        "status": "reported",
    },
}

DOCUMENT = (
    "[Page 1]\nAvery Nguyen (CUST-001)\n"
    "I already paid invoice INV-2026-0441 on 2026-03-20 by bank transfer.\n"
    "Outstanding Balance: 410.40\n"
)


class _Handler(BaseHTTPRequestHandler):
    captured: dict = {}
    status = 200
    body: dict | None = None

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0))
        type(self).captured = json.loads(self.rfile.read(length) or b"{}")
        payload = self.body if self.body is not None else {
            "id": "chatcmpl-local-1",
            "object": "chat.completion",
            "created": 0,
            "model": "local-stub",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(REPLY)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 900,
                "completion_tokens": 120,
                "total_tokens": 1020,
                # Real providers include nested detail objects.
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        }
        encoded = json.dumps(payload).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):  # keep test output quiet
        return


@pytest.fixture
def server():
    _Handler.captured = {}
    _Handler.status = 200
    _Handler.body = None
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def client(base_url: str, mode: str = "json_schema") -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        api_key="local-test-key",
        model="local-stub",
        base_url=base_url,
        response_format_mode=mode,
        timeout=10,
        max_retries=0,
    )


def test_request_uses_structured_output_and_wraps_document_as_data(server):
    result = LLMExtractor(client(server)).extract(DOCUMENT)

    request = _Handler.captured
    assert request["model"] == "local-stub"
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    user_message = request["messages"][1]["content"]
    assert "DOCUMENT START" in user_message and "DOCUMENT END" in user_message
    assert "INV-2026-0441" in user_message

    assert result.analysis_source is AnalysisSource.live_llm
    assert result.detected_problem.type is ProblemType.payment_status_discrepancy


def test_response_id_and_flat_token_usage_are_recorded(server):
    extractor = LLMExtractor(client(server))
    result = extractor.extract(DOCUMENT)

    assert extractor.client.last_response_id == "chatcmpl-local-1"
    assert result.llm_call.invoked is True
    assert result.llm_call.provider_response_id == "chatcmpl-local-1"
    assert result.llm_call.model == "local-stub"
    # Nested provider detail objects must not leak into the flat summary.
    assert result.llm_call.usage == {
        "prompt_tokens": 900,
        "completion_tokens": 120,
        "total_tokens": 1020,
    }


def test_json_object_mode_is_supported(server):
    extractor = LLMExtractor(client(server, mode="json_object"))
    result = extractor.extract(DOCUMENT)

    assert _Handler.captured["response_format"] == {"type": "json_object"}
    assert result.analysis_source is AnalysisSource.live_llm


def test_unsupported_response_format_is_rejected_before_calling(server):
    with pytest.raises(ExtractionError):
        client(server, mode="xml").complete("system", "user")
    assert _Handler.captured == {}


def test_provider_http_failure_is_reported_without_leaking_document_text(server):
    _Handler.status = 503
    _Handler.body = {"error": {"message": "SECRET-INTERNAL-DETAIL: upstream exploded"}}

    with pytest.raises(ExtractionError) as error:
        client(server).complete("system", DOCUMENT)

    message = str(error.value)
    assert "503" in message
    assert "SECRET-INTERNAL-DETAIL" not in message
    assert "INV-2026-0441" not in message


def test_unreachable_provider_is_an_explicit_error():
    unreachable = OpenAICompatibleClient(
        api_key="k", model="m", base_url="http://127.0.0.1:1/v1", timeout=2, max_retries=0
    )
    with pytest.raises(ExtractionError) as error:
        unreachable.complete("system", DOCUMENT)
    assert "could not be reached" in str(error.value)


def test_fenced_json_with_preamble_is_still_parsed():
    from agent3.extractor import _json_from_content

    payload = _json_from_content('Here is the result:\n```json\n{"document_type": "invoice"}\n```')
    assert payload == {"document_type": "invoice"}
    assert _json_from_content('Sure: {"a": 1} hope that helps') == {"a": 1}
    with pytest.raises(ExtractionError):
        _json_from_content("no json here at all")
