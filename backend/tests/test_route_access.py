"""Routes that used to return case, payment or customer data without an account."""

from __future__ import annotations

from workflow.accounts import ROLE_CUSTOMER

from http_helpers import auth, invoice_text, make_admin, make_app, reply, signup, upload


def paid_case(app, client, customer, admin, invoice="INV-RA-1", amount="180.00"):
    case = upload(app, client, customer, invoice_text(invoice, amount), reply(invoice, amount)).json()
    response = client.post(f"/workflow/cases/{case['case_id']}/approve", json={}, headers=auth(admin))
    assert response.status_code == 200, response.text
    return response.json()


def test_execute_requires_the_owner_or_an_admin(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "execute")
    owner, _ = signup(client, "owner@example.test")
    other, _ = signup(client, "other@example.test")
    admin, _ = make_admin(app, client, "bank@example.test")
    case = paid_case(app, client, owner, admin)
    url = f"/workflow/cases/{case['case_id']}/execute"

    assert client.post(url, json={}).status_code == 401
    assert client.post(url, json={}, headers=auth(other)).status_code == 404
    rows_before = len(client.get("/bank/ledger", headers=auth(admin)).json()["transactions"])
    owner_view = client.post(url, json={}, headers=auth(owner))
    assert owner_view.status_code == 200
    # Idempotency still applies: re-inspecting never moves money again.
    assert owner_view.json()["last_payment_report"]["data"]["executed"] is False
    assert client.post(url, json={}, headers=auth(admin)).status_code == 200
    assert len(client.get("/bank/ledger", headers=auth(admin)).json()["transactions"]) == rows_before


def test_invoice_payments_are_scoped_to_the_owner(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "payments")
    owner, _ = signup(client, "owner@example.test")
    other, _ = signup(client, "other@example.test")
    admin, _ = make_admin(app, client, "bank@example.test")
    paid_case(app, client, owner, admin, invoice="INV-RA-2")
    url = "/workflow/invoices/INV-RA-2/payments"

    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth(owner)).json()["count"] == 1
    assert client.get(url, headers=auth(admin)).json()["count"] == 1
    stranger = client.get(url, headers=auth(other)).json()
    assert stranger["count"] == 0 and stranger["attempts"] == []


def test_reference_customers_and_saved_results_are_admin_only(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "refs")
    customer, _ = signup(client, "c@example.test")
    admin, _ = make_admin(app, client, "bank@example.test")

    for url in ("/customers", "/customers/CUST-001", "/agent3/results/INV-2026-0441"):
        assert client.get(url).status_code == 401, url
        assert client.get(url, headers=auth(customer)).status_code == 403, url
    assert client.get("/customers", headers=auth(admin)).status_code == 200


def test_synthetic_corpus_is_admin_only(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "synthetic")
    customer, _ = signup(client, "c@example.test")
    admin, _ = make_admin(app, client, "bank@example.test")

    for method, url in (
        ("get", "/agent3/synthetic/customers"),
        ("get", "/agent3/synthetic/documents"),
        ("post", "/agent3/synthetic/analyze/does-not-exist"),
        ("post", "/agent3/synthetic/evaluate"),
    ):
        assert getattr(client, method)(url).status_code == 401, url
        assert getattr(client, method)(url, headers=auth(customer)).status_code == 403, url
    assert client.get("/agent3/synthetic/customers", headers=auth(admin)).status_code == 200


def test_invoice_history_cannot_be_read_by_choosing_a_display_name(workspace_tmp_path):
    from test_agent1 import seed_invoice_history

    app, client = make_app(workspace_tmp_path, "history")
    seed_invoice_history(
        app.state.workflow_repository.path,
        [{"invoice_number": "INV-RA-OTHER", "vendor": "V", "customer": "Victim Corp",
          "amount": "99.00", "issue_date": "2026-01-01", "description": "x"}],
    )
    # Display names are self-chosen at signup.
    impostor, _ = signup(client, "impostor@example.test", "Victim Corp")

    response = client.get("/workflow/invoice-history", headers=auth(impostor))
    assert response.status_code == 200
    assert all(row["invoice_number"] != "INV-RA-OTHER" for row in response.json())


def test_signup_ignores_a_requested_role(workspace_tmp_path):
    app, client = make_app(workspace_tmp_path, "signup-role")

    for role in ("BANK_ADMIN", "DEMO_ADMIN"):
        response = client.post(
            "/auth/signup",
            json={"email": f"{role.lower()}@example.test", "password": "password123", "role": role},
        )
        assert response.status_code == 200
        assert response.json()["account"]["role"] == ROLE_CUSTOMER
        token = response.json()["token"]
        assert client.get("/bank/ledger", headers=auth(token)).status_code == 403
